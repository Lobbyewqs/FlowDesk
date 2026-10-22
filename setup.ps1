$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskTemp = Join-Path $PSScriptRoot '.tmp'
New-Item -ItemType Directory -Path $taskTemp -Force | Out-Null
$env:TEMP = $taskTemp
$env:TMP = $taskTemp
$env:PYTHONUTF8 = '1'
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败，请检查 Python 安装。' }
}
& '.venv\Scripts\python.exe' -m ensurepip --upgrade
if ($LASTEXITCODE -ne 0) { throw '初始化 pip 失败。' }
& '.venv\Scripts\python.exe' -m pip install -r requirements.lock.txt
if ($LASTEXITCODE -ne 0) { throw '安装依赖失败，请检查网络后重试。' }
Write-Output 'FlowDesk 已安装。运行 .\start.ps1 或双击 启动FlowDesk.cmd。'

