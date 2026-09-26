@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 未找到项目Python环境，请运行常用入口中的维护工具：安装依赖。
  exit /b 1
)
".venv\Scripts\python.exe" -m %*
exit /b %errorlevel%
