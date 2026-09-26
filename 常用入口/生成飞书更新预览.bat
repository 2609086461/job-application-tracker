@echo off
chcp 65001 >nul
call "%~dp0..\看板程序\run-tool.bat" tools.feishu.match_feishu_mail_preview
pause
