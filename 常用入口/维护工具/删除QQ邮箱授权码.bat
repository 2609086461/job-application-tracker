@echo off
chcp 65001 >nul
setlocal
echo 删除本机保存的QQ邮箱授权码后，下次同步需要重新填写。
set "CONFIRM="
set /p "CONFIRM=输入 YES 确认操作："
if /i not "%CONFIRM%"=="YES" (
  echo 已取消。
  pause
  exit /b 0
)
call "%~dp0..\..\看板程序\run-tool.bat" tools.mail.read_qq_mail --forget-auth
pause
