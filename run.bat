@echo off
cd /d "%~dp0"
echo "创建虚拟环境"
python -m venv .venv >nul 2>&1
echo "安装依赖"
.venv\Scripts\python -m pip install -r requirements.txt >nul 2>&1
echo "安装完成"
.venv\Scripts\python tool2path.py >nul 2>&1
