# -*- coding: utf-8 -*-
"""SQLite 存储与聚合查询

news 表在写入时就冗余好 hour('2026-10-06 17') / day 列，聚合查询不依赖
SQLite 时间函数版本，直接按字符串分组。
"""

import json
import sqlite3
import threading
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
    """)
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
        except sqlite3.Error:
            continue
    conn.commit()
    return n


def count_all():
    return _conn().execute("SELECT COUNT(*) c FROM news").fetchone()["c"]


def hourly_sentiment(hours_back=168):
    """按小时聚合情绪。返回按 hour 升序的列表：
    {hour, n, index(均分×100), pos_cnt, neg_cnt, neu_cnt, avg_score}
    """
    cutoff = datetime.now().strftime("%Y-%m-%d %H")
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
