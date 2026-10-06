# -*- coding: utf-8 -*-
"""全市场新闻情绪指标 — FastAPI 服务

启动:  python app.py        (默认 http://127.0.0.1:8000)
流程:  启动后台线程回填历史(新浪7×24全量+新浪滚动近7天+东财要闻前5页)
       → 每2分钟增量抓取5源 → 词典情绪打分 → SQLite → 聚合API
"""

import os
import threading
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import fetchers
import storage
from analyzer import score_text, extract_keywords

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "data.db")
REFRESH_SECONDS = 120          # 增量抓取间隔
BACKFILL_DAYS = 7              # 回填深度

@asynccontextmanager
async def lifespan(_app):
    storage.init_db(DB_FILE)
    threading.Thread(target=_backfill, daemon=True).start()
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    yield


app = FastAPI(title="全市场新闻情绪指标", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

state = {
    "backfill_running": False,
    "backfill_pages_done": 0,
    "backfill_pages_total": 0,
    "last_fetch_time": None,
    "last_fetch_added": 0,
    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
}


# ---------------------------------------------------------------- 抓取管道
def process_and_save(items):
    for it in items:
        s = score_text(it["title"], it.get("summary", ""))
        it.update(s)
        it["keywords"] = extract_keywords(it["title"], it.get("summary", ""), topk=6)
    return storage.upsert_news(items)


def incremental_fetch():
    """抓5源最新一页并入库，返回新增条数"""
    added = 0
    for fn in fetchers.FETCHERS_INCREMENTAL:
        try:
            added += process_and_save(fn())
        except Exception:
            traceback.print_exc()
    return added


def _backfill():
    # 已有足量历史（如服务重启）则跳过，仅靠增量任务补最新
    if storage.count_all() > 300:
        state["backfill_running"] = False
        return
    state["backfill_running"] = True
    cutoff = time.time() - BACKFILL_DAYS * 86400
    try:
        # 新浪7×24 全量翻页（历史约900条）
        for page in range(1, 50):
            items = fetchers.fetch_sina724(page, 100)
            if not items:
                break
            process_and_save(items)
            state["backfill_pages_done"] += 1
            if min(i["ts"] for i in items) < cutoff:
                break
            time.sleep(0.4)
        # 新浪滚动新闻 近7天（每页50条，最多60页）
        for page in range(1, 61):
            items = fetchers.fetch_sinaroll(page, 50)
            if not items:
                break
            process_and_save(items)
            state["backfill_pages_done"] += 1
            if min(i["ts"] for i in items) < cutoff:
                break
            time.sleep(0.4)
        # 东财要闻 前5页
        for page in range(1, 6):
            items = fetchers.fetch_emnews(page, 20)
            if not items:
                break
            process_and_save(items)
            state["backfill_pages_done"] += 1
            time.sleep(0.4)
        # 东财快讯 + 华尔街见闻 最新一批
        process_and_save(fetchers.fetch_emfast(50))
        process_and_save(fetchers.fetch_wscn(50))
    except Exception:
        traceback.print_exc()
    finally:
        state["backfill_running"] = False
        state["last_fetch_added"] = storage.count_all()


def _scheduler_loop():
    while True:
        try:
            added = incremental_fetch()
            state["last_fetch_time"] = datetime.now().strftime("%H:%M:%S")
            state["last_fetch_added"] = added
        except Exception:
            traceback.print_exc()
        time.sleep(REFRESH_SECONDS)


def _zone_of(index):
    if index >= 40:
        return "极度贪婪", "#e03131"
    if index >= 15:
        return "贪婪", "#f76707"
    if index > -15:
        return "中性", "#748ffc"
    if index > -40:
        return "恐慌", "#2f9e44"
    return "极度恐慌", "#0b7285"


@app.get("/api/overview")
def api_overview():
    spot = fetchers.fetch_index_spot()
    hs = [h for h in storage.hourly_sentiment(6) if h["n"] >= 3]
    cur_index = hs[-1]["index"] if hs else 0.0
    zone, color = _zone_of(cur_index)
    stat = storage.overview_stat()
    return {
        "index_value": cur_index,
        "zone": zone, "zone_color": color,
        "spot": spot,
        "stat": stat,
        "pos_neg_ratio": round(stat["today_pos"] / max(stat["today_neg"], 1), 2),
        "backfill": {
            "running": state["backfill_running"],
            "pages_done": state["backfill_pages_done"],
            "total_news": storage.count_all(),
        },
        "last_fetch_time": state["last_fetch_time"],
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "started_at": state["started_at"],
    }


@app.get("/api/timeline")
def api_timeline(hours: int = 168):
    hours = max(1, min(hours, 24 * 30))
    hourly = storage.hourly_sentiment(hours)
    kline = fetchers.fetch_index_kline("1.000001", 60, days=max(3, hours // 24 + 2))
    daily = storage.daily_sentiment(30)
    return {"hourly": hourly, "kline": kline, "daily": daily}


@app.get("/api/news")
def api_news(hours: int = 24, label: int = None, limit: int = 50, offset: int = 0):
    limit = max(1, min(limit, 200))
    rows = storage.query_news(hours, label, limit, offset)
    return {"total": len(rows), "items": rows}


@app.get("/api/top")
def api_top(label: int = 1, hours: int = 24):
    return {"items": storage.top_news(1 if label >= 0 else -1, hours, 10)}


@app.get("/api/keywords")
def api_keywords(hours: int = 24):
    return {"items": storage.hot_keywords(hours, 30)}


@app.get("/api/sources")
def api_sources(hours: int = 24):
    rows = storage.source_sentiment(hours)
    for r in rows:
        r["name"] = fetchers.SOURCE_NAMES.get(r["source"], r["source"])
    return {"items": rows}


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
