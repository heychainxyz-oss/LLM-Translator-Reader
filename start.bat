@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 德语智读 · German AI Reader

if not exist ".venv\Scripts\python.exe" goto create
".venv\Scripts\python.exe" -c "import fastapi" >nul 2>nul
if errorlevel 1 goto install
goto run

:create
echo [1/2] 未检测到虚拟环境，正在创建 .venv …
py -3.13 -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" python -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo 无法创建虚拟环境。请先安装 Python 3.10 或更高版本：
  echo https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

:install
echo [2/2] 正在安装运行依赖，请稍候…
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo 依赖安装失败，请检查网络后重试。
  pause
  exit /b 1
)

:run
echo.
echo 正在启动本地服务，浏览器将自动打开 http://127.0.0.1:8000
echo 手机与电脑连接同一 Wi-Fi 后，可通过设置中的局域网地址访问。
echo 按 Ctrl+C 停止服务。
echo.
set READER_OPEN_BROWSER=1
".venv\Scripts\python.exe" main.py
echo.
pause
