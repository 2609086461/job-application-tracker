$ErrorActionPreference = 'Stop'
$app = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$processes = @(Get-CimInstance Win32_Process)
$roots = @($processes | Where-Object {
    $_.Name -in @('python.exe','pythonw.exe') -and
    $_.ExecutablePath -like ($app + '\.venv\*') -and
    $_.CommandLine -match '(-m tools.dashboard|-m uvicorn app.main:app)'
})
$ids = @($roots | Select-Object -ExpandProperty ProcessId)
do {
    $children = @($processes | Where-Object { $_.ParentProcessId -in $ids -and $_.ProcessId -notin $ids })
    $ids += @($children | Select-Object -ExpandProperty ProcessId)
} while ($children.Count)
[array]::Reverse($ids)
foreach ($processId in $ids) { Stop-Process -Id $processId -ErrorAction SilentlyContinue }
Write-Output "Tracker stopped ($($ids.Count) processes)."
