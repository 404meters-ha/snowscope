@echo off
rem celestial-snow 一键启动（等价于 IDEA 运行配置 celestial-snow :8100）
cd /d %~dp0
.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8100
