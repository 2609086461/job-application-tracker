@echo off
chcp 65001 >nul
call "%~dp0..\看板程序\run-tool.bat" tools.check_update
pause
