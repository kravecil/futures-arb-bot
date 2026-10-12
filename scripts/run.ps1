# Запуск FuturesArbBot (PowerShell). Аргументы передаются роботу:
#   ./scripts/run.ps1 --once
#   ./scripts/run.ps1 --no-ui
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location (Join-Path $PSScriptRoot '..')
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error 'Не найден менеджер пакетов uv. Установите: https://docs.astral.sh/uv/getting-started/installation/'
    exit 1
}
uv run --no-dev futures-arb-bot @args
exit $LASTEXITCODE
