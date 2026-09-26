@echo off
chcp 65001 >nul
call "%~dp0..\..\看板程序\run-tool.bat" tools.mail.read_qq_mail --days 7 --limit 100 --save-auth
pause
