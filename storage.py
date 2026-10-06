# -*- coding: utf-8 -*-
"""SQLite 存储与聚合查询

news 表在写入时就冗余好 hour('2026-10-06 17') / day 列，聚合查询不依赖
SQLite 时间函数版本，直接按字符串分组。
"""

import json
import sqlite3
import threading
import time
from datetime import datetime

DB_PATH = None  # 由 init_db 指定
_local = threading.local()


def init_db(path):
    global DB_PATH
    DB_PATH = path
    conn = _conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS news(
        uid       TEXT PRIMARY KEY,
        source    TEXT NOT NULL,
        title     TEXT NOT NULL,
        summary   TEXT DEFAULT '',
        url       TEXT DEFAULT '',
        ts        INTEGER NOT NULL,
        day       TEXT NOT NULL,
        hour      TEXT NOT NULL,
        stocks    TEXT DEFAULT '',
        media     TEXT DEFAULT '',
        score     REAL DEFAULT 0,
        label     INTEGER DEFAULT 0,
        pos_words TEXT DEFAULT '',
        neg_words TEXT DEFAULT '',
        keywords  TEXT DEFAULT '[]'
    );
    CREATE INDEX IF NOT EXISTS idx_news_hour   ON news(hour);
    CREATE INDEX IF NOT EXISTS idx_news_ts     ON news(ts);
    CREATE INDEX IF NOT EXISTS idx_news_label  ON news(label);
    CREATE INDEX IF NOT EXISTS idx_news_source ON news(source);
    CREATE TABLE IF NOT EXISTS sectors_meta(
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        updated_at INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS stock_sector(
        stock_code  TEXT NOT NULL,
        sector_code TEXT NOT NULL,
        stock_name  TEXT DEFAULT '',
        PRIMARY KEY (stock_code, sector_code)
    );
    CREATE TABLE IF NOT EXISTS news_sector(
        uid         TEXT NOT NULL,
        sector_code TEXT NOT NULL,
        PRIMARY KEY (uid, sector_code)
    );
    CREATE INDEX IF NOT EXISTS idx_ns_sector ON news_sector(sector_code);
    """)
    # 历史库升级：stock_sector 补 stock_name 列
    try:
        conn.execute("ALTER TABLE stock_sector ADD COLUMN stock_name TEXT DEFAULT ''")
    except sqlite3.Error:
        pass
    conn.commit()


def _conn():
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


def _s(v):
    """字段统一转字符串：防歂数据源把标量字段换成对象/列表导致整条静默丢弃"""
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return ",".join(_s(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def upsert_news(items):
    """items 需已带 score/label/pos_words/neg_words/keywords 字段（analyzer 输出）
    返回新增条数。uid 冲突时忽略（去重）。
    """
    conn = _conn()
    n = 0
    for it in items:
        dt = datetime.fromtimestamp(it["ts"])
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO news
                   (uid, source, title, summary, url, ts, day, hour, stocks, media,
                    score, label, pos_words, neg_words, keywords)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (it["uid"], it["source"], it["title"], _s(it.get("summary", "")),
                 _s(it.get("url", "")), it["ts"], dt.strftime("%Y-%m-%d"),
                 dt.strftime("%Y-%m-%d %H"), _s(it.get("stocks", "")),
                 _s(it.get("media", "")), it.get("score", 0), it.get("label", 0),
                 ",".join(it.get("pos_words", [])),
                 ",".join(it.get("neg_words", [])),
                 json.dumps(it.get("keywords", []), ensure_ascii=False)))
            n += cur.rowcount if cur.rowcount > 0 else 0
            if cur.rowcount > 0 and it.get("sectors"):
                conn.executemany(
                    "INSERT OR IGNORE INTO news_sector(uid, sector_code) VALUES (?,?)",
                    [(it["uid"], s) for s in it["sectors"]])
        except sqlite3.Error:
            continue
    conn.commit()
    return n


# ---------------------------------------------------------------- 板块
def save_sector_meta(sectors, stock_rows):
    """保存板块元数据与成分股明细（stock_rows: [(code, name, sector)]）"""
    conn = _conn()
    now = int(time.time())
    with conn:
        conn.execute("DELETE FROM sectors_meta")
        conn.executemany("INSERT OR REPLACE INTO sectors_meta(code,name,updated_at) VALUES (?,?,?)",
                         [(s["code"], s["name"], now) for s in sectors])
        conn.execute("DELETE FROM stock_sector")
        conn.executemany("INSERT OR REPLACE INTO stock_sector(stock_code,sector_code,stock_name) VALUES (?,?,?)",
                         stock_rows)


def sector_meta_age():
    row = _conn().execute("SELECT MIN(updated_at) t FROM sectors_meta").fetchone()
    return (time.time() - row["t"]) if row and row["t"] else 1e9


def sector_count():
    return _conn().execute("SELECT COUNT(*) c FROM sectors_meta").fetchone()["c"]


def stock_sector_map():
    """{个股代码: 板块代码}"""
    rows = _conn().execute("SELECT stock_code, sector_code FROM stock_sector").fetchall()
    m = {}
    for r in rows:
        m.setdefault(r["stock_code"], r["sector_code"])
    return m


def stock_name_map():
    """{股票简称: 板块代码}（用于标题个股名匹配）"""
    rows = _conn().execute(
        "SELECT stock_name, sector_code FROM stock_sector WHERE stock_name != ''").fetchall()
    m = {}
    for r in rows:
        name = (r["stock_name"] or "").strip()
        if len(name) >= 2 and "*" not in name:
            m.setdefault(name, r["sector_code"])
    return m


def sector_names():
    rows = _conn().execute("SELECT code,name FROM sectors_meta").fetchall()
    return {r["code"]: r["name"] for r in rows}


def link_news_sectors(uid, sector_codes):
    with _conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO news_sector(uid,sector_code) VALUES (?,?)",
                         [(uid, s) for s in sector_codes])


def sector_ranking(hours_back=24, min_n=3, limit=40):
    """板块情绪排行：按 (|指数|, 新闻量) 综合排序"""
    rows = _conn().execute(
        """SELECT ns.sector_code, COUNT(*) n, AVG(nd.score) avg_score,
                  SUM(CASE WHEN nd.label=1 THEN 1 ELSE 0 END) pos_cnt,
                  SUM(CASE WHEN nd.label=-1 THEN 1 ELSE 0 END) neg_cnt
           FROM news_sector ns JOIN news nd ON nd.uid = ns.uid
           WHERE nd.hour >= datetime('now','localtime',?)
           GROUP BY ns.sector_code HAVING n >= ?""",
        ("-%d hours" % hours_back, min_n)).fetchall()
    names = sector_names()
    out = [{
        "code": r["sector_code"], "name": names.get(r["sector_code"], r["sector_code"]),
        "n": r["n"], "index": round((r["avg_score"] or 0) * 100, 2),
        "pos_cnt": r["pos_cnt"], "neg_cnt": r["neg_cnt"],
    } for r in rows]
    out.sort(key=lambda x: (abs(x["index"]), x["n"]), reverse=True)
    return out[:limit]


def sector_timeline(sector_code, hours_back=72):
    rows = _conn().execute(
        """SELECT nd.hour, COUNT(*) n, AVG(nd.score) avg_score
           FROM news_sector ns JOIN news nd ON nd.uid = ns.uid
           WHERE ns.sector_code = ? AND nd.hour >= datetime('now','localtime',?)
           GROUP BY nd.hour ORDER BY nd.hour""",
        (sector_code, "-%d hours" % hours_back)).fetchall()
    return [{
        "hour": r["hour"], "n": r["n"],
        "index": round((r["avg_score"] or 0) * 100, 2),
    } for r in rows]


def count_all():
    return _conn().execute("SELECT COUNT(*) c FROM news").fetchone()["c"]


def _ewma(values, span):
    """指数移动平均；空桶(None)沿用上一值，保持曲线连续"""
    alpha = 2.0 / (span + 1.0)
    out, prev = [], None
    for v in values:
        x = prev if v is None else v
        prev = x if prev is None else alpha * x + (1 - alpha) * prev
        out.append(round(prev, 2))
    return out


def hourly_sentiment(hours_back=168):
    """按小时聚合情绪（含 EWMA 快/慢动量线）。
    返回按 hour 升序的列表：
    {hour, n, index(均分×100), pos_cnt, neg_cnt, neu_cnt, ewma_fast, ewma_slow}
    """
    rows = _conn().execute(
        """SELECT hour, COUNT(*) n, AVG(score) avg_score, SUM(label) label_sum,
                  SUM(CASE WHEN label=1 THEN 1 ELSE 0 END) pos_cnt,
                  SUM(CASE WHEN label=-1 THEN 1 ELSE 0 END) neg_cnt
           FROM news GROUP BY hour HAVING hour >= datetime('now', 'localtime', ?)
           ORDER BY hour""",
        ("-%d hours" % hours_back,)).fetchall()
    out = []
    for r in rows:
        n = r["n"]
        out.append({
            "hour": r["hour"],
            "n": n,
            "index": round((r["avg_score"] or 0) * 100, 2),
            "pos_cnt": r["pos_cnt"], "neg_cnt": r["neg_cnt"],
            "neu_cnt": n - r["pos_cnt"] - r["neg_cnt"],
        })
    idx = [h["index"] for h in out]
    fast, slow = _ewma(idx, 6), _ewma(idx, 24)
    for h, f, s in zip(out, fast, slow):
        h["ewma_fast"], h["ewma_slow"] = f, s
    return out


def daily_sentiment(days_back=30):
    rows = _conn().execute(
        """SELECT day, COUNT(*) n, AVG(score) avg_score,
                  SUM(CASE WHEN label=1 THEN 1 ELSE 0 END) pos_cnt,
                  SUM(CASE WHEN label=-1 THEN 1 ELSE 0 END) neg_cnt
           FROM news GROUP BY day HAVING day >= date('now', 'localtime', ?)
           ORDER BY day""",
        ("-%d days" % days_back,)).fetchall()
    return [{
        "day": r["day"], "n": r["n"],
        "index": round((r["avg_score"] or 0) * 100, 2),
        "pos_cnt": r["pos_cnt"], "neg_cnt": r["neg_cnt"],
    } for r in rows]


def source_sentiment(hours_back=24):
    rows = _conn().execute(
        """SELECT source, COUNT(*) n, AVG(score) avg_score,
                  SUM(CASE WHEN label=1 THEN 1 ELSE 0 END) pos_cnt,
                  SUM(CASE WHEN label=-1 THEN 1 ELSE 0 END) neg_cnt
           FROM news WHERE hour >= datetime('now', 'localtime', ?)
           GROUP BY source ORDER BY n DESC""",
        ("-%d hours" % hours_back,)).fetchall()
    return [{
        "source": r["source"], "n": r["n"],
        "index": round((r["avg_score"] or 0) * 100, 2),
        "pos_cnt": r["pos_cnt"], "neg_cnt": r["neg_cnt"],
    } for r in rows]


def query_news(hours_back=24, label=None, limit=50, offset=0, order="ts DESC"):
    cond = "hour >= datetime('now', 'localtime', ?)"
    args = ["-%d hours" % hours_back]
    if label in (1, -1, 0):
        cond += " AND label=?"
        args.append(label)
    order = order if order in ("ts DESC", "ts ASC", "ABS(score) DESC") else "ts DESC"
    rows = _conn().execute(
        """SELECT uid, source, title, summary, url, ts, hour, stocks, media,
                  score, label, pos_words, neg_words
           FROM news WHERE %s ORDER BY %s LIMIT ? OFFSET ?""" % (cond, order),
        args + [limit, offset]).fetchall()
    out = []
    for r in rows:
        out.append({
            "uid": r["uid"], "source": r["source"], "title": r["title"],
            "summary": r["summary"], "url": r["url"], "ts": r["ts"],
            "dt": datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M"),
            "stocks": r["stocks"], "media": r["media"],
            "score": r["score"], "label": r["label"],
            "pos_words": [w for w in (r["pos_words"] or "").split(",") if w],
            "neg_words": [w for w in (r["neg_words"] or "").split(",") if w],
        })
    return out


def top_news(label, hours_back=24, limit=10):
    """|score| 最高的正面/负面新闻"""
    sign = 1 if label > 0 else -1
    rows = _conn().execute(
        """SELECT uid, source, title, url, ts, score, label
           FROM news WHERE label=? AND hour >= datetime('now', 'localtime', ?)
           ORDER BY ABS(score) DESC, ts DESC LIMIT ?""",
        (sign, "-%d hours" % hours_back, limit)).fetchall()
    return [{
        "title": r["title"], "source": r["source"], "url": r["url"],
        "dt": datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M"),
        "score": r["score"],
    } for r in rows]


def hot_keywords(hours_back=24, topk=30):
    """热词榜：keywords 列(JSON数组)在 Python 侧展开计数"""
    rows = _conn().execute(
        "SELECT keywords FROM news WHERE hour >= datetime('now', 'localtime', ?)",
        ("-%d hours" % hours_back,)).fetchall()
    from collections import Counter
    counter = Counter()
    for r in rows:
        try:
            for w in json.loads(r["keywords"] or "[]"):
                counter[w] += 1
        except Exception:
            continue
    return [{"word": w, "count": c} for w, c in counter.most_common(topk)]


def overview_stat():
    row = _conn().execute(
        """SELECT COUNT(*) n, AVG(score) avg_score,
                  SUM(CASE WHEN label=1 THEN 1 ELSE 0 END) pos_cnt,
                  SUM(CASE WHEN label=-1 THEN 1 ELSE 0 END) neg_cnt,
                  MIN(ts) min_ts, MAX(ts) max_ts
           FROM news WHERE day = date('now', 'localtime')""").fetchone()
    n_today = row["n"] or 0
    return {
        "today_count": n_today,
        "today_avg_score": round((row["avg_score"] or 0) * 100, 2),
        "today_pos": row["pos_cnt"] or 0,
        "today_neg": row["neg_cnt"] or 0,
    }


if __name__ == "__main__":
    import os
    import time as _t
    init_db(os.path.join(os.path.dirname(__file__), "test.db"))
    from analyzer import score_text, extract_keywords
    item = {
        "uid": "t1", "source": "sina724", "title": "测试新闻",
        "summary": "", "url": "", "ts": int(_t.time()),
    }
    s = score_text(item["title"], item["summary"])
    kw = extract_keywords(item["title"], item["summary"])
    item.update(s, keywords=kw)
    print("inserted:", upsert_news([item]))
    print("count:", count_all())
    print("hourly:", hourly_sentiment(2))
    _local.conn.close()
    _local.conn = None
    os.remove(os.path.join(os.path.dirname(__file__), "test.db"))
