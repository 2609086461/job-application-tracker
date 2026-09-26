@echo off
chcp 65001 >nul
call "%~dp0..\看板程序\run-tool.bat" tools.mail.sync_and_analyze
pause
