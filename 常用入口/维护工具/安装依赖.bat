@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0..\..\看板程序"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto :fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
echo 依赖安装完成。
pause
exit /b 0
:fail
echo 安装失败，请检查上方错误信息。
pause
exit /b 1
