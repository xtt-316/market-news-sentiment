#!/usr/bin/env bash
# 一键启动：装依赖 -> 开浏览器 -> 起服务
cd "$(dirname "$0")"

echo "[1/3] 检查依赖..."
python -c "import fastapi, uvicorn, requests, apscheduler, jieba" 2>/dev/null || \
    pip install -r requirements.txt

echo "[2/3] 启动服务 http://127.0.0.1:8000 ..."
(sleep 3 && (xdg-open http://127.0.0.1:8000 2>/dev/null || open http://127.0.0.1:8000 2>/dev/null)) &

echo "[3/3] Ctrl+C 停止服务"
python app.py
