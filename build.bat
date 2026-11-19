@echo off
cd /d "%~dp0"
echo "打包前准备"
python -m venv .venv >nul 2>&1
.venv\Scripts\python -m pip install -r requirements.txt >nul 2>&1
echo "开始打包"
.venv\Scripts\python.exe -m PyInstaller -y -w -D tool2path.py >nul 2>&1
echo "打包完成"
