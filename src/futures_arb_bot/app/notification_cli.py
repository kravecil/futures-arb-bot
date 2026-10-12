"""
Служебные режимы настройки уведомлений: --notify-test и --notify-chat-id.
Работают без подключения бирж — только MAX Bot API и config/notifications.yaml.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from rich import box
from rich.table import Table

from futures_arb_bot.app.bootstrap import Services
from futures_arb_bot.app.cli import CliOptions
from futures_arb_bot.app.console import console, esc
from futures_arb_bot.core.cancellation import CancellationToken, OperationCanceledError
from futures_arb_bot.core.domain.options import NotificationOptions
from futures_arb_bot.core.engine import formatting
from futures_arb_bot.core.engine.notification_formatter import test_message
from futures_arb_bot.core.errors import message_of
from futures_arb_bot.infrastructure.config import YamlConfigProvider
from futures_arb_bot.infrastructure.max_messenger import MaxMessengerClient

WATCH_SECONDS = 90
"""Сколько секунд слушаем /updates в режиме поиска chat_id."""


def requested(cli: CliOptions) -> bool:
    """Запрошен ли один из служебных режимов."""
    return cli.notify_test or cli.notify_chat_id


async def run(cli: CliOptions, ct: CancellationToken) -> int:
    """Проверить токен, а дальше — тестовая отправка либо поиск chat_id. Возвращает код выхода."""
    services = Services(cli)
    try:
        config = services.config
        client = services.max_client
        notifications = config.current.notifications

        _print_state(notifications)

        console.print(f"[cyan]→[/] проверяю токен: GET {esc(notifications.api_base_url)}/me")
        verify = await client.verify(ct)
        if not verify.success:
            console.print(f"[red]✗ токен не принят:[/] {esc(verify.detail)}")
            console.print(
                "[bright_black]Создайте бота в диалоге с @MasterBot и запишите токен в notifications.bot_token "
                "или в .env: ARB_NOTIFICATIONS__BOT_TOKEN.[/]"
            )
            return 2

        console.print(f"[green]✓[/] {esc(verify.detail)}")

        if cli.notify_chat_id:
            return await _watch_chat_id(client, ct)
        return await _send_test(client, config, ct)
    finally:
        await services.aclose()


async def _send_test(client: MaxMessengerClient, config: YamlConfigProvider, ct: CancellationToken) -> int:
    """Отправить проверочное сообщение в чат из конфигурации."""
    notifications = config.current.notifications
    if not notifications.admin_chat_id.strip():
        console.print("[red]✗ Notifications:AdminChatId пуст.[/] Сначала выполните: --notify-chat-id")
        return 2

    if not notifications.enabled:
        console.print("[yellow]![/] Notifications:Enabled = false — сообщение отправляю вручную, сигналы робота при этом не шлются.")

    text = test_message(datetime.now(UTC), config.current.general.network_mode)
    console.print(f"[cyan]→[/] отправляю тестовое сообщение в чат {esc(notifications.admin_chat_id)}…")

    delivery = await client.send(text, ct)
    if delivery.is_delivered:
        console.print("[green]✓ сообщение доставлено.[/] Задайте Notifications:Enabled = true — и сигналы начнут приходить.")
        return 0

    console.print(f"[red]✗ доставка не удалась:[/] {esc(delivery.error or 'неизвестная ошибка')}")
    if notifications.use_markdown:
        console.print("[bright_black]400 — возможно, чат не принимает разметку: отключите UseMarkdown. 401/403 — токен, 404 — AdminChatId.[/]")
    else:
        console.print("[bright_black]401/403 — проверьте BotToken, 404 — AdminChatId (и что вы написали боту первым).[/]")
    return 2


async def _watch_chat_id(client: MaxMessengerClient, ct: CancellationToken) -> int:
    """Слушать входящие апдейты и показывать chat_id диалогов, где боту уже написали."""
    console.print("[yellow]Отправьте боту любое сообщение в Max[/] — прочитаю chat_id этого диалога. Ctrl+C — выход.")

    deadline = time.monotonic() + WATCH_SECONDS
    known: set[str] = set()
    marker: str | None = None

    while time.monotonic() < deadline and not ct.is_cancellation_requested:
        try:
            page = await client.poll_updates(marker, ct)
        except OperationCanceledError:
            break
        except Exception as ex:
            console.print(f"[red]✗ не удалось прочитать /updates:[/] {esc(message_of(ex))}")
            console.print("[bright_black]Бот не может начать диалог сам: убедитесь, что токен ваш и вы уже написали боту.[/]")
            return 2

        marker = page.marker or marker
        for hit in page.chats:
            if hit.chat_id in known:
                continue
            known.add(hit.chat_id)
            note = hit.update_type if not (hit.text and hit.text.strip()) else f"{hit.update_type}: {_trim(hit.text)}"
            console.print(f"[green]✓ chat_id[/] {esc(hit.chat_id)} [bright_black]{esc(note or 'update')}[/]")

    if not known:
        console.print("[yellow]Сообщений не увидено.[/] Напишите боту и повторите: --notify-chat-id")
        return 1

    console.print("[bright_black]Скопируйте нужный chat_id в notifications.admin_chat_id и включите enabled = true.[/]")
    return 0


def _trim(text: str) -> str:
    return text if len(text) <= 60 else text[:60] + "…"


def _print_state(notifications: NotificationOptions) -> None:
    """Показать, что сейчас настроено в разделе notifications (без раскрытия токена)."""
    table = Table(box=box.ROUNDED, show_header=False)
    table.add_column("Параметр")
    table.add_column("Значение")

    min_spread = (
        formatting.pct(notifications.min_spread_percent) if notifications.min_spread_percent is not None else "наследует Arbitrage:MinSpreadPercentUp"
    )
    table.add_row("Enabled", _describe(notifications.enabled))
    table.add_row("BotToken", _mask(notifications.bot_token))
    table.add_row("AdminChatId", "[red]не задан[/]" if not notifications.admin_chat_id.strip() else esc(notifications.admin_chat_id))
    table.add_row("ApiBaseUrl", esc(notifications.api_base_url))
    table.add_row("Endpoint", str(notifications.endpoint))
    table.add_row("MinSpreadPercent", min_spread)
    table.add_row("UseMarkdown / Silent", f"{_describe(notifications.use_markdown)} / {_describe(notifications.silent)}")
    table.add_row("Cooldown / MaxPerMinute", f"{notifications.signal_cooldown_seconds} c / {notifications.max_per_minute} сообщ.")
    table.add_row(
        "Retries / Circuit", f"{notifications.max_retries} / {notifications.failure_circuit_limit}→{notifications.circuit_break_minutes} мин"
    )

    console.print("[cyan]Уведомления (config/notifications.yaml → notifications)[/]")
    console.print(table)
    console.print()


def _describe(value: bool) -> str:
    return "[green]да[/]" if value else "[bright_black]нет[/]"


def _mask(token: str) -> str:
    if not token.strip():
        return "[red]не задан[/]"
    if len(token) <= 8:
        return "[yellow]задан (короткий)[/]"
    return esc(f"{token[:4]}…{token[-4:]} ({len(token)} симв.)")
