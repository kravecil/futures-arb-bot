@echo off
rem Запуск FuturesArbBot (Windows). Двойной клик — мониторинг с дашбордом.
rem Аргументы передаются роботу: run.cmd --once / --no-ui / --notify-test ...
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
where uv >nul 2>nul || (
  echo Не найден менеджер пакетов uv. Установите: https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)
uv run --no-dev futures-arb-bot %*
exit /b %ERRORLEVEL%
