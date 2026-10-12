# Тесты и линтер (PowerShell): ./scripts/test.ps1 [аргументы pytest]
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
uv run ruff check src tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run pytest @args
exit $LASTEXITCODE
