# -*- coding: utf-8 -*-
"""一次性迁移：为存量新闻建立板块关联（news_sector 表）

用法: python migrate_sectors.py
"""

import os
import time

import storage
import fetchers
from app import _load_sector_meta, _resolve_sectors, DB_FILE


def main():
    storage.init_db(DB_FILE)
    print("加载板块元数据（首次需拉取全部板块成分股，约1~3分钟）...")
    t0 = time.time()
    _load_sector_meta(force=True)
    print("板块: %d 个，个股映射: %d 条，耗时 %.0fs" % (
        storage.sector_count(), len(storage.stock_sector_map()), time.time() - t0))

    rows = storage._conn().execute(
        "SELECT uid, stocks, title, source FROM news").fetchall()
    print("存量新闻:", len(rows), "条")
    linked = 0
    batch = []
    for i, r in enumerate(rows):
        codes = _resolve_sectors({"stocks": r["stocks"], "title": r["title"]})
        if codes:
            batch.append((r["uid"], codes))
            linked += 1
        if len(batch) >= 500:
            _flush(batch)
            batch = []
    _flush(batch)
    print("已关联板块的新闻: %d 条 (%.1f%%)" % (linked, 100.0 * linked / max(len(rows), 1)))


def _flush(batch):
    for uid, codes in batch:
        storage.link_news_sectors(uid, codes)


if __name__ == "__main__":
    main()
