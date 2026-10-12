#!/usr/bin/env sh
# Тесты и линтер (Linux/macOS): ./scripts/test.sh [аргументы pytest]
set -e
cd "$(dirname "$0")/.."
uv run ruff check src tests
exec uv run pytest "$@"
