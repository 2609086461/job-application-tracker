@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\setup-windows.ps1" -StartDashboard
pause
