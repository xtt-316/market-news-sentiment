# -*- coding: utf-8 -*-
"""5个新闻源 + 指数行情抓取器（全部为计划阶段实测可用的公开JSON接口）

统一输出 NewsItem dict：
  uid / source / source_name / title / summary / url / ts(unix秒) / stocks / media
"""

import hashlib
import json
import re
import time
from datetime import datetime

import requests

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_TAG_RE = re.compile(r"<[^>]+>")

SOURCE_NAMES = {
    "sina724": "新浪7×24",
    "sinaroll": "新浪财经",
    "emfast": "东财快讯",
    "emnews": "东财要闻",
    "wscn": "华尔街见闻",
}


def _get(url, referer=None, timeout=10):
    h = dict(HEADERS)
    if referer:
        h["Referer"] = referer
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.json()


def _clean(text):
    return _TAG_RE.sub("", text or "").strip()


def _ts_from_str(s):
    """'2026-10-06 17:00:58' -> unix秒（本地时区即北京时间）"""
    try:
        return int(datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        return int(time.time())


def _uid(source, key, title=""):
    if not key:
        key = hashlib.md5(title.encode("utf-8")).hexdigest()
    return "%s:%s" % (source, key)


# ---------------------------------------------------------------- 新浪7×24
def fetch_sina724(page=1, page_size=100):
    """新浪财经7×24直播，可翻页（历史约900条）"""
    url = ("https://zhibo.sina.com.cn/api/zhibo/feed?callback="
           "&page=%d&page_size=%d&zhibo_id=152&tag_id=0&dire=f&dpc=1" % (page, page_size))
    try:
        data = _get(url, referer="https://finance.sina.com.cn/7x24/")
        items = data["result"]["data"]["feed"]["list"] or []
    except Exception:
        return []
    out = []
    for it in items:
        text = _clean(it.get("rich_text", ""))
        if not text:
            continue
        stocks = ""
        try:
            ext = json.loads(it.get("ext", "") or "{}")
            stocks = ",".join(s.get("symbol", "") for s in ext.get("stocks", []) if s.get("symbol"))
        except Exception:
            pass
        # tag 字段可能是字符串或 [{id,name}] 列表，统一转字符串
        media = it.get("tag", "")
        if isinstance(media, list):
            media = ",".join(x.get("name", "") for x in media if isinstance(x, dict))
        out.append({
            "uid": _uid("sina724", it.get("id")),
            "source": "sina724",
            "source_name": SOURCE_NAMES["sina724"],
            "title": text[:80],
            "summary": text[80:400] if len(text) > 80 else "",
            "url": it.get("docurl") or "https://finance.sina.com.cn/7x24/",
            "ts": _ts_from_str(it.get("create_time", "")),
            "stocks": stocks,
            "media": media or "",
        })
    return out


# ---------------------------------------------------------------- 新浪滚动新闻
def fetch_sinaroll(page=1, num=50):
    """新浪财经滚动新闻（历史海量，回填主力）"""
    url = ("https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516"
           "&num=%d&page=%d" % (num, page))
    try:
        data = _get(url, referer="https://finance.sina.com.cn/roll/")
        items = data.get("result", {}).get("data") or []
    except Exception:
        return []
    out = []
    for it in items:
        title = _clean(it.get("title", ""))
        if not title:
            continue
        intro = _clean(it.get("intro", "") or it.get("summary", ""))
        ts = int(it.get("ctime") or 0) or int(time.time())
        out.append({
            "uid": _uid("sinaroll", it.get("docid"), title),
            "source": "sinaroll",
            "source_name": SOURCE_NAMES["sinaroll"],
            "title": title,
            "summary": intro[:400],
            "url": it.get("url") or "",
            "ts": ts,
            "stocks": "",
            "media": it.get("media_name", ""),
        })
    return out


# ---------------------------------------------------------------- 东财7×24快讯
def fetch_emfast(page_size=50):
    """东方财富7×24快讯（含关联股票/板块）"""
    url = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
           "?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=%d&req_trace=1" % page_size)
    try:
        data = _get(url, referer="https://kuaixun.eastmoney.com/")
        items = data["data"]["fastNewsList"] or []
    except Exception:
        return []
    out = []
    for it in items:
        title = _clean(it.get("title", ""))
        if not title:
            continue
        stocks = ""
        try:
            stocks = ",".join(s.split(".")[-1] for s in (it.get("stockList") or []) if s)
        except Exception:
            pass
        out.append({
            "uid": _uid("emfast", it.get("code"), title),
            "source": "emfast",
            "source_name": SOURCE_NAMES["emfast"],
            "title": title,
            "summary": _clean(it.get("summary", ""))[:400],
            "url": "https://kuaixun.eastmoney.com/",
            "ts": _ts_from_str(it.get("showTime", "")),
            "stocks": stocks,
            "media": "",
        })
    return out


# ---------------------------------------------------------------- 东财要闻
def fetch_emnews(page_index=1, page_size=20):
    """东方财富要闻栏目（深度新闻，可翻页）"""
    url = ("https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
           "?client=web&biz=web_news_col&column=350&order=1&needInteractData=0"
           "&page_index=%d&page_size=%d&req_trace=1" % (page_index, page_size))
    try:
        data = _get(url, referer="https://finance.eastmoney.com/")
        items = data["data"]["list"] or []
    except Exception:
        return []
    out = []
    for it in items:
        title = _clean(it.get("title", ""))
        if not title:
            continue
        out.append({
            "uid": _uid("emnews", it.get("code"), title),
            "source": "emnews",
            "source_name": SOURCE_NAMES["emnews"],
            "title": title,
            "summary": _clean(it.get("summary", ""))[:400],
            "url": it.get("uniqueUrl") or it.get("url") or "",
            "ts": _ts_from_str(it.get("showTime", "")),
            "stocks": "",
            "media": it.get("mediaName", ""),
        })
    return out


# ---------------------------------------------------------------- 华尔街见闻
def fetch_wscn(limit=30):
    """华尔街见闻全球快讯"""
    url = ("https://api-one.wallstcn.com/apiv1/content/lives"
           "?channel=global-channel&limit=%d" % limit)
    try:
        data = _get(url, referer="https://wallstreetcn.com/live/global")
        items = data["data"]["items"] or []
    except Exception:
        return []
    out = []
    for it in items:
        title = _clean(it.get("title", ""))
        content = _clean(it.get("content_text", ""))
        if not title and not content:
            continue
        if not title:
            title = content[:80]
        out.append({
            "uid": _uid("wscn", it.get("id")),
            "source": "wscn",
            "source_name": SOURCE_NAMES["wscn"],
            "title": title[:80],
            "summary": content[:400],
            "url": "https://wallstreetcn.com/live/global",
            "ts": int(it.get("display_time") or 0) or int(time.time()),
            "stocks": "",
            "media": "",
        })
    return out


# ---------------------------------------------------------------- 指数行情
INDEX_SECIDS = "1.000001,0.399001,0.399006,0.399005"  # 上证/深成/创业板/中小100


def fetch_index_spot():
    """上证/深成/创业板实时行情（东财优先，失败切腾讯源）"""
    out = _fetch_index_spot_em()
    if out:
        return out
    return _fetch_index_spot_qq()


def _fetch_index_spot_em():
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2"
           "&secids=%s&fields=f2,f3,f4,f12,f14" % INDEX_SECIDS)
    try:
        data = _get(url, referer="https://quote.eastmoney.com/")
        out = {}
        for it in (data.get("data") or {}).get("diff") or []:
            out[it.get("f12")] = {
                "code": it.get("f12"), "name": it.get("f14"),
                "price": it.get("f2"), "pct": it.get("f3"), "chg": it.get("f4"),
            }
        return out
    except Exception:
        return {}


def _fetch_index_spot_qq():
    name_map = {"sh000001": "上证指数", "sz399001": "深证成指",
                "sz399006": "创业板指", "sz399005": "中小100"}
    url = "https://qt.gtimg.cn/q=" + ",".join(name_map)
    try:
        h = dict(HEADERS)
        h["Referer"] = "https://gu.qq.com/"
        r = requests.get(url, headers=h, timeout=10)
        r.encoding = "gbk"
        out = {}
        for line in r.text.split(";"):
            if "=" not in line:
                continue
            body = line.split("=", 1)[1].strip('"')
            f = body.split("~")
            if len(f) < 33:
                continue
            code = f[2]
            out[code] = {
                "code": code, "name": f[1],
                "price": float(f[3]), "pct": float(f[32]), "chg": float(f[31]),
            }
        return out
    except Exception:
        return {}


def fetch_index_kline(secid="1.000001", klt=60, days=10):
    """指数60分钟K线（用于情绪曲线叠加对照）。腾讯源优先，东财备用。"""
    out = _fetch_index_kline_qq(klt, days)
    if out:
        return out
    return _fetch_index_kline_em(secid, klt, days)


def _fetch_index_kline_qq(klt=60, days=10):
    qq_code = "sh000001"
    url = ("https://ifzq.gtimg.cn/appstock/app/kline/mkline"
           "?param=%s,m60,,%d" % (qq_code, min(days * 4, 320)))
    try:
        h = dict(HEADERS)
        h["Referer"] = "https://gu.qq.com/"
        r = requests.get(url, headers=h, timeout=10)
        rows = r.json()["data"][qq_code]["m60"] or []
        out = []
        for k in rows:
            t = k[0]  # "202609301500"
            dt = datetime.strptime(t[:12], "%Y%m%d%H%M")
            # 仅保留整点（10:30/11:30/14:00等60分钟锚点也保留，取小时桶）
            out.append({"dt": dt.strftime("%Y-%m-%d %H"),
                        "ts": int(dt.timestamp()), "close": float(k[2])})
        # 同一小时桶取最后一条
        dedup = {}
        for o in out:
            dedup[o["dt"]] = o
        return [dedup[k] for k in sorted(dedup)]
    except Exception:
        return []


def _fetch_index_kline_em(secid="1.000001", klt=60, days=10):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3&fields2=f51,f53&klt=%d&fqt=1&end=20500101"
           "&lmt=%d" % (secid, klt, days * 4))
    try:
        data = _get(url, referer="https://quote.eastmoney.com/")
        klines = (data.get("data") or {}).get("klines") or []
        out = []
        for k in klines:
            parts = k.split(",")
            # 时间"2026-10-06 10:30" -> unix秒；仅保留整点
            dt = datetime.strptime(parts[0][:16], "%Y-%m-%d %H:%M")
            out.append({"dt": parts[0][:13], "ts": int(dt.timestamp()), "close": float(parts[1])})
        return out
    except Exception:
        return []


FETCHERS_INCREMENTAL = [
    lambda: fetch_sina724(1, 50),
    lambda: fetch_sinaroll(1, 50),
    fetch_emfast,
    lambda: fetch_emnews(1, 20),
    fetch_wscn,
]

if __name__ == "__main__":
    for fn in (fetch_sina724, fetch_sinaroll, fetch_emfast, fetch_emnews, fetch_wscn):
        try:
            items = fn()
            print("%-10s %3d条  最新: %s" % (items[0]["source"] if items else "-",
                                             len(items),
                                             items[0]["title"][:30] if items else "-"))
        except Exception as e:
            print("FAIL", getattr(fn, "__name__", fn), e)
    print("指数:", fetch_index_spot().get("000001"))
