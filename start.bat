@echo off
chcp 65001 >nul
title 全市场新闻情绪指标
cd /d "%~dp0"

echo [1/3] 检查依赖...
python -c "import fastapi, uvicorn, requests, apscheduler, jieba" 2>nul
if errorlevel 1 (
    echo 正在安装依赖...
    pip install -r requirements.txt
)

echo [2/3] 启动服务 http://127.0.0.1:8000 ...
start "" http://127.0.0.1:8000

echo [3/3] 后台运行中，关闭本窗口即停止服务。
python app.py
pause
