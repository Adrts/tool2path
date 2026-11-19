@echo off
cd /d "%~dp0"
echo "启动前准备"
python -m venv .venv >nul 2>&1
.venv\Scripts\python -m pip install -r requirements.txt >nul 2>&1
echo "启动工具"
.venv\Scripts\python tool2path.py >nul 2>&1
