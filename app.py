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
from fastapi.staticfiles import StaticFiles

import fetchers
import storage
from analyzer import score_text, extract_keywords

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "data.db")
REFRESH_SECONDS = 120          # 增量抓取间隔
BACKFILL_DAYS = 7              # 回填深度
SECTOR_META_TTL = 7 * 86400    # 板块元数据刷新周期

# 板块映射（启动后由后台线程填充）
STOCK_SECTOR_MAP = {}      # 个股代码 -> 板块代码
STOCK_NAME_MAP = {}        # 股票简称 -> 板块代码（标题匹配用）
SECTOR_NAME_MATCH = []     # [(板块核心名, 板块代码)] 按名长降序

# 热点概念词 -> 新浪行业名（行业分类偏传统，用别名补热点覆盖）
CONCEPT_ALIASES = {
    "人工智能": "电子信息", "芯片": "电子信息", "半导体": "电子信息", "算力": "电子信息",
    "光模块": "电子信息", "集成电路": "电子信息", "晶圆": "电子信息", "消费电子": "电子信息",
    "软件": "电子信息", "互联网": "传媒娱乐", "游戏": "传媒娱乐", "影视": "传媒娱乐",
    "短剧": "传媒娱乐", "直播": "传媒娱乐", "传媒": "传媒娱乐",
    "银行": "金融行业", "券商": "金融行业", "保险": "金融行业", "证券": "金融行业",
    "基金": "金融行业", "期货": "金融行业",
    "地产": "房地产", "楼市": "房地产", "房企": "房地产", "房价": "房地产",
    "白酒": "酿酒行业", "啤酒": "酿酒行业", "食品饮料": "食品行业",
    "新能源车": "汽车制造", "电动车": "汽车制造", "车企": "汽车制造", "整车": "汽车制造",
    "汽车": "汽车制造", "智能驾驶": "汽车制造", "自动驾驶": "汽车制造",
    "光伏": "电力行业", "风电": "电力行业", "核电": "电力行业", "绿电": "电力行业",
    "电力": "电力行业", "电网": "电器行业",
    "煤炭": "煤炭行业", "动力煤": "煤炭行业",
    "原油": "石油行业", "油气": "石油行业", "炼化": "石油行业", "石油": "石油行业",
    "钢铁": "钢铁行业", "钢材": "钢铁行业", "铁矿石": "钢铁行业",
    "有色": "有色金属", "稀土": "有色金属", "黄金": "有色金属", "白银": "有色金属",
    "锂矿": "有色金属", "铜价": "有色金属",
    "医药": "生物制药", "创新药": "生物制药", "疫苗": "生物制药", "中药": "生物制药",
    "医疗": "医疗器械", "CXO": "生物制药",
    "军工": "飞机制造", "国防": "飞机制造", "航天": "飞机制造", "低空经济": "飞机制造",
    "机器人": "机械行业", "工程机械": "机械行业", "机床": "机械行业",
    "水泥": "水泥行业", "建材": "建筑建材", "基建": "建筑建材", "建筑": "建筑建材",
    "化工": "化工行业", "磷化工": "化工行业",
    "种业": "农林牧渔", "粮食": "农林牧渔", "养殖": "农林牧渔", "农业": "农林牧渔",
    "旅游": "酒店旅游", "酒店": "酒店旅游", "免税": "酒店旅游", "出行": "酒店旅游",
    "家电": "家电行业", "空调": "家电行业",
    "环保": "环保行业", "碳中和": "环保行业",
    "物流": "交通运输", "航运": "交通运输", "港口": "交通运输", "快递": "交通运输",
    "航空": "交通运输", "高铁": "交通运输",
    "外贸": "物资外贸", "出口": "物资外贸", "进口": "物资外贸",
    "玻璃": "玻璃行业", "纺织": "纺织行业", "造纸": "造纸行业",
}
# 核心名过于泛化、不做标题匹配的板块
_SECTOR_MATCH_BLACKLIST = {"综合", "其它行业", "次新股", "开发区"}

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


# ---------------------------------------------------------------- 板块关联
def _norm_stock_code(raw):
    """'sz002460'/'sh600519'/'002460' -> '002460'；非6位A股码返回 None"""
    raw = (raw or "").strip().lower()
    digits = raw[-6:] if len(raw) >= 6 else ""
    return digits if digits.isdigit() and len(digits) == 6 else None


def _resolve_sectors(item):
    """一条新闻关联的板块：个股代码反查 / 显式板块码 / 标题匹配个股简称与板块核心名"""
    codes = set()
    valid_codes = {code for _, code in SECTOR_NAME_MATCH}
    for s in (item.get("stocks") or "").split(","):
        s = s.strip()
        if not s:
            continue
        if s.upper().startswith("BK") and s.upper() in valid_codes:
            codes.add(s.upper())
        else:
            norm = _norm_stock_code(s)
            if norm and STOCK_SECTOR_MAP:
                sec = STOCK_SECTOR_MAP.get(norm)
                if sec:
                    codes.add(sec)
    title = item.get("title", "")
    if title:
        for name, code in SECTOR_NAME_MATCH:
            if len(codes) >= 4:   # 单条新闻最多挂4个板块，防泛化
                break
            if len(name) >= 2 and name in title:
                codes.add(code)
        for name, code in STOCK_NAME_MAP.items():
            if len(codes) >= 4:
                break
            if len(name) >= 3 and name in title:   # 简称>=3字才做标题匹配，降误伤
                codes.add(code)
    return list(codes)


def _load_sector_meta(force=False):
    """加载/刷新板块元数据（SQLite 缓存 7 天），填充全局映射"""
    global STOCK_SECTOR_MAP, STOCK_NAME_MAP, SECTOR_NAME_MATCH
    if force or storage.sector_count() == 0 or storage.sector_meta_age() > SECTOR_META_TTL:
        sectors = fetchers.fetch_sector_list()
        if sectors:
            stock_rows = []
            for s in sectors:
                for st in fetchers.fetch_sector_stocks(s["code"]):
                    stock_rows.append((st["code"], s["code"], st.get("name", "")))
                time.sleep(0.15)
            storage.save_sector_meta(sectors, stock_rows)
    names = storage.sector_names()
    STOCK_SECTOR_MAP = storage.stock_sector_map()
    STOCK_NAME_MAP = storage.stock_name_map()
    # 标题匹配词表：行业核心名（去"行业"后缀）+ 热点概念别名，黑名单除外
    core = {}
    for code, name in names.items():
        key = name[:-2] if name.endswith("行业") and len(name) > 4 else name
        if key not in _SECTOR_MATCH_BLACKLIST:
            core[key] = code
    code_by_name = {name: code for code, name in names.items()}
    for alias, target in CONCEPT_ALIASES.items():
        code = code_by_name.get(target)
        if code and alias not in core:
            core[alias] = code
    SECTOR_NAME_MATCH = sorted(core.items(), key=lambda x: -len(x[0]))


# ---------------------------------------------------------------- 抓取管道
def process_and_save(items):
    for it in items:
        s = score_text(it["title"], it.get("summary", ""))
        it.update(s)
        it["keywords"] = extract_keywords(it["title"], it.get("summary", ""), topk=6)
        if it.get("sectors") is None:
            it["sectors"] = _resolve_sectors(it)
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
    _load_sector_meta()   # 回填前先备好板块映射，存量新闻可正确关联
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
    first = True
    while True:
        try:
            if first:
                first = False
                # 增量抓取前先确保板块映射就绪（缓存命中时几乎瞬时）
                _load_sector_meta()
            added = incremental_fetch()
            state["last_fetch_time"] = datetime.now().strftime("%H:%M:%S")
            state["last_fetch_added"] = added
            if storage.sector_meta_age() > SECTOR_META_TTL:
                _load_sector_meta(force=True)
        except Exception:
            traceback.print_exc()
        time.sleep(REFRESH_SECONDS)


def _pearson(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs) ** 0.5
    vy = sum((y - my) ** 2 for y in ys) ** 0.5
    return cov / (vx * vy) if vx > 0 and vy > 0 else None


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

    # 情绪×上证 相关性：对齐最近有K线的时间点，取其前24个情绪小时桶
    corr = None
    try:
        kline = fetchers.fetch_index_kline("1.000001", 60, days=10)
        if kline:
            kmap = {k["dt"]: k["close"] for k in kline}
            last_k = kline[-1]["dt"]
            hourly = [h for h in storage.hourly_sentiment(14 * 24) if h["hour"] <= last_k][-72:]
            pairs = [(h["index"], kmap[h["hour"]]) for h in hourly if h["hour"] in kmap]
            if len(pairs) >= 5:
                corr = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
    except Exception:
        pass

    return {
        "index_value": cur_index,
        "zone": zone, "zone_color": color,
        "spot": spot,
        "stat": stat,
        "pos_neg_ratio": round(stat["today_pos"] / max(stat["today_neg"], 1), 2),
        "corr_sh_24h": round(corr, 3) if corr is not None else None,
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


@app.get("/api/sectors")
def api_sectors(hours: int = 24, min_n: int = 3):
    return {"items": storage.sector_ranking(hours, max(1, min_n))}


@app.get("/api/sectors/{code}/timeline")
def api_sector_timeline(code: str, hours: int = 72):
    hours = max(1, min(hours, 24 * 14))
    names = storage.sector_names()
    # 兼容 BK 大写与 new_ 小写两种板块码体系
    code = code if code in names else code.upper()
    return {"code": code, "name": names.get(code, code),
            "items": storage.sector_timeline(code, hours)}


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


# 开发辅助：PDF速览页与其截图素材（/overview 预览）
app.mount("/assets", StaticFiles(directory=os.path.join(BASE_DIR, "assets")), name="assets")


@app.get("/overview")
def overview_page():
    return FileResponse(os.path.join(BASE_DIR, "overview.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
