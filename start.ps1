param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    Write-Output '首次启动，正在安装独立运行环境…'
    & (Join-Path $PSScriptRoot 'setup.ps1')
}
& '.venv\Scripts\python.exe' run.py --open --port $Port

