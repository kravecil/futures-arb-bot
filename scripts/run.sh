#!/usr/bin/env sh
# Запуск FuturesArbBot (Linux/macOS). Аргументы передаются роботу: ./scripts/run.sh --once
set -e
cd "$(dirname "$0")/.."
command -v uv >/dev/null 2>&1 || { echo "Не найден менеджер пакетов uv: https://docs.astral.sh/uv/" >&2; exit 1; }
exec uv run --no-dev futures-arb-bot "$@"
