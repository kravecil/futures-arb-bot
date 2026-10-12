#!/usr/bin/env bash
# Установка/обновление FuturesArbBot на Linux-сервере (Debian/Ubuntu, RHEL-подобные).
#
#   curl -fsSL https://raw.githubusercontent.com/kravecil/futures-arb-bot/main/scripts/setup-server.sh | bash
#
# Что делает:
#   1) ставит git, curl, tmux и uv (uv сам скачает Python 3.14);
#   2) прежний каталог ~/futures-arb-bot без git (сборка версии на C#) переносит в ~/futures-arb-bot.old-<дата>;
#   3) клонирует репозиторий (или обновляет существующий клон) и ставит зависимости;
#   4) создаёт .env: переносит ключи бирж и токен MAX из старых config/*.json, если они там были;
#   5) регистрирует службу systemd futures-arb-bot (не запускает её).
# Повторный запуск безопасен: обновляет код и зависимости, .env не трогает.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/kravecil/futures-arb-bot.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-$HOME/futures-arb-bot}"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

log "Системные пакеты"
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq git curl ca-certificates tmux >/dev/null
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q git curl ca-certificates tmux
elif command -v yum >/dev/null 2>&1; then
    yum install -y -q git curl ca-certificates tmux
else
    echo "Не найден apt/dnf/yum — установите git, curl и tmux вручную." >&2
fi

log "uv"
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv --version

OLD_DIR=""
if [ -d "$APP_DIR" ] && [ ! -d "$APP_DIR/.git" ]; then
    OLD_DIR="$APP_DIR.old-$(date +%Y%m%d-%H%M%S)"
    log "Прежняя версия перенесена в $OLD_DIR"
    mv "$APP_DIR" "$OLD_DIR"
fi

if [ -d "$APP_DIR/.git" ]; then
    log "Обновление кода ($BRANCH)"
    git -C "$APP_DIR" fetch -q origin "$BRANCH"
    git -C "$APP_DIR" checkout -q "$BRANCH"
    git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
else
    log "Клонирование $REPO_URL"
    git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
log "Python 3.14 и зависимости"
uv sync --locked --no-dev

if [ ! -f .env ]; then
    log "Создание .env"
    cp .env.example .env
    chmod 600 .env
    if [ -n "$OLD_DIR" ]; then
        # ключи из старых exchanges.json / notifications.json (версия на C#) → .env
        uv run --no-dev python - "$OLD_DIR" <<'PY'
import json, pathlib, re, sys

old = pathlib.Path(sys.argv[1])
env_path = pathlib.Path(".env")
values: dict[str, str] = {}

def load(name):
    for path in sorted(old.rglob(name)):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
    return None

exchanges = load("exchanges.json") or {}
for item in (exchanges.get("Exchanges") or {}).get("Items") or []:
    ident = re.sub(r"[^A-Z0-9]", "_", str(item.get("Id", "")).upper())
    for field, suffix in (("ApiKey", "API_KEY"), ("Secret", "SECRET"), ("Password", "PASSWORD")):
        if ident and item.get(field):
            values[f"ARB_{ident}_{suffix}"] = str(item[field])

notifications = (load("notifications.json") or {}).get("Notifications") or {}
if notifications.get("BotToken"):
    values["ARB_NOTIFICATIONS__BOT_TOKEN"] = str(notifications["BotToken"])
if notifications.get("AdminChatId"):
    values["ARB_NOTIFICATIONS__ADMIN_CHAT_ID"] = str(notifications["AdminChatId"])

lines = env_path.read_text(encoding="utf-8").splitlines()
done = set()
for i, line in enumerate(lines):
    key = line.lstrip("# ").split("=", 1)[0].strip()
    if key in values and key not in done:
        lines[i] = f"{key}={values[key]}"
        done.add(key)
lines += [f"{k}={v}" for k, v in values.items() if k not in done]
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"перенесено значений из старой конфигурации: {len(values)}")
PY
    fi
else
    log ".env уже есть — не трогаю"
fi

if command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ] && [ "$(id -u)" = "0" ]; then
    log "Служба systemd futures-arb-bot"
    cat >/etc/systemd/system/futures-arb-bot.service <<UNIT
[Unit]
Description=FuturesArbBot — межбиржевой арбитражный робот
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$HOME/.local/bin/uv run --no-dev futures-arb-bot --no-ui
Environment=PYTHONUNBUFFERED=1
Environment=COLUMNS=160
# SIGINT = Ctrl+C: корректная остановка с закрытием позиций и итоговым отчётом
KillSignal=SIGINT
TimeoutStopSec=120
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
fi

log "Готово"
cat <<TXT
Каталог: $APP_DIR
1) Проверьте ключи:            nano $APP_DIR/.env
2) Проверьте настройки:        nano $APP_DIR/config/appsettings.yaml
3) Пробный запуск (один скан): cd $APP_DIR && uv run futures-arb-bot --once

Постоянная работа с дашбордом — в tmux (отключиться: Ctrl+B, затем D; вернуться: tmux attach -t arb):
   tmux new -s arb "cd $APP_DIR && uv run futures-arb-bot"

Или службой (текстовый журнал, автоперезапуск, запуск при загрузке сервера):
   systemctl enable --now futures-arb-bot
   journalctl -u futures-arb-bot -f        # журнал
   systemctl stop futures-arb-bot          # остановка с закрытием позиций и отчётом

Обновление робота: повторите эту же команду установки.
TXT
