@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\..\看板程序\tools\stop_dashboard.ps1"
pause
