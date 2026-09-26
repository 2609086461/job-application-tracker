@echo off
chcp 65001 >nul
setlocal
echo 请先核对：%~dp0..\业务数据\待更新\飞书匹配结果_最新.txt
set "CONFIRM="
set /p "CONFIRM=输入 YES 确认操作："
if /i not "%CONFIRM%"=="YES" (
  echo 已取消。
  pause
  exit /b 0
)
call "%~dp0..\看板程序\run-tool.bat" tools.feishu.apply_feishu_mail_updates --confirm
pause
