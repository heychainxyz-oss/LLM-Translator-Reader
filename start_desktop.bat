@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 德语智读 · Desktop

if not exist ".venv\Scripts\python.exe" (
  echo 正在创建虚拟环境…
  py -3.13 -m venv .venv 2>nul
  if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv 2>nul
  if not exist ".venv\Scripts\python.exe" python -m venv .venv 2>nul
)

if not exist ".venv\Scripts\python.exe" (
  echo 无法创建虚拟环境，请先安装 Python 3.10+。
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -c "import webview" >nul 2>nul
if errorlevel 1 (
  echo 正在安装桌面窗口依赖 pywebview（首次需要联网）…
  ".venv\Scripts\python.exe" -m pip install -r requirements-desktop.txt
  if errorlevel 1 (
    echo 安装失败。你仍然可以双击 start.bat 使用浏览器模式。
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" desktop.py
if errorlevel 1 (
  echo.
  echo 独立窗口启动失败，已尝试回退。可改用 start.bat。
  pause
)
