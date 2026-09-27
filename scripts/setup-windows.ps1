param(
  [switch]$SkipTests,
  [switch]$StartDashboard
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$appRoot = Join-Path $repoRoot "看板程序"
$venvPython = Join-Path $appRoot ".venv\Scripts\python.exe"

$python = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python.exe -ErrorAction SilentlyContinue }
if (-not $python) { throw "Python 3.11 or newer is required." }

Write-Host "[1/5] Creating isolated Python environment..."
if (-not (Test-Path -LiteralPath $venvPython)) {
  if ($python.Name -ieq "py.exe") { & $python.Source -3 -m venv (Join-Path $appRoot ".venv") }
  else { & $python.Source -m venv (Join-Path $appRoot ".venv") }
  if ($LASTEXITCODE -ne 0) { throw "Could not create .venv." }
}

Write-Host "[2/5] Installing dependencies..."
& $venvPython -m pip install -r (Join-Path $appRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

Write-Host "[3/5] Preparing private configuration and data directories..."
$envPath = Join-Path $appRoot ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
  Copy-Item -LiteralPath (Join-Path $appRoot ".env.example") -Destination $envPath
}
foreach ($relative in @(
  "业务数据\公司投递", "业务数据\同步记录", "业务数据\待更新",
  "业务数据\邮件导出", "历史备份\飞书数据快照"
)) {
  New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot $relative) | Out-Null
}

Write-Host "[4/5] Running offline verification..."
Push-Location $appRoot
try {
  if (-not $SkipTests) {
    & $venvPython -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "Offline tests failed." }
  }
  & $venvPython -m tools.doctor
  if ($LASTEXITCODE -ne 0) { throw "Installation doctor found a blocking problem." }
}
finally { Pop-Location }

Write-Host "[5/5] Installation is ready."
if ($StartDashboard) {
  Start-Process -FilePath $venvPython -ArgumentList @("-m", "tools.dashboard") -WorkingDirectory $appRoot
  Write-Host "Dashboard is starting at http://localhost:8765"
}
else {
  Write-Host "Run 常用入口\启动看板.bat when you are ready."
}
Write-Host "Feishu and QQ mail remain disabled until you choose to configure them."
