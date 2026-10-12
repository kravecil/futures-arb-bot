"""Аргументы командной строки."""

from __future__ import annotations

from dataclasses import dataclass, replace

from rich import box
from rich.table import Table

from futures_arb_bot.app.console import console, figlet


class CliError(Exception):
    """Ошибка разбора аргументов."""


@dataclass(frozen=True, slots=True)
class CliOptions:
    config_dir: str | None = None
    once: bool = False
    """Одно сканирование, отчёт и выход."""
    no_ui: bool = False
    """Без Live-дашборда: события печатаются потоком в консоль."""
    show_help: bool = False
    notify_test: bool = False
    """Проверить токен MAX и прислать тестовое сообщение (биржи не подключаются)."""
    notify_chat_id: bool = False
    """Показать chat_id администратора: ждёт входящих сообщений (биржи не подключаются)."""

    @staticmethod
    def parse(args: list[str]) -> CliOptions:
        options = CliOptions()
        i = 0
        while i < len(args):
            arg = args[i]
            match arg:
                case "--once":
                    options = replace(options, once=True)
                case "--no-ui":
                    options = replace(options, no_ui=True)
                case "--help" | "-h":
                    return replace(options, show_help=True)
                case "--config-dir" | "-c":
                    if i + 1 >= len(args):
                        raise CliError("Для --config-dir нужно указать путь.")
                    i += 1
                    options = replace(options, config_dir=args[i])
                case "--notify-test":
                    options = replace(options, notify_test=True)
                case "--notify-chat-id":
                    options = replace(options, notify_chat_id=True)
                case _:
                    raise CliError(f"Неизвестный аргумент: {arg} (см. --help)")
            i += 1
        return options


def print_help() -> None:
    console.print(figlet("FuturesArbBot"))
    console.print("[cyan]Межбиржевой арбитражный робот (фьючерсы, CCXT)[/]")
    console.print()
    table = Table(box=box.ROUNDED, show_header=False)
    table.add_column("Аргумент")
    table.add_column("Описание")
    table.add_row("[cyan]--once[/]", "выполнить одно сканирование, показать сводку и выйти")
    table.add_row("[cyan]--no-ui[/]", "без Live-дашборда; события печатаются обычным потоком")
    table.add_row("[cyan]--config-dir <путь>[/]", "каталог конфигурации (по умолчанию: ./config)")
    table.add_row("[cyan]--notify-test[/]", "проверить токен MAX и прислать тестовое сообщение в чат администратора")
    table.add_row("[cyan]--notify-chat-id[/]", "показать chat_id: отправьте боту любое сообщение, робот прочитает его из /updates")
    table.add_row("[cyan]--help[/]", "показать справку")
    console.print(table)
    console.print("[bright_black]Примеры:[/] uv run futures-arb-bot --once")
    console.print("       uv run futures-arb-bot --config-dir ./config")
    console.print("       uv run futures-arb-bot --notify-chat-id")
    console.print()
    console.print(
        "[bright_black]Уведомления настраиваются в config/notifications.yaml (раздел notifications); "
        "токен безопаснее задавать в .env: ARB_NOTIFICATIONS__BOT_TOKEN.[/]"
    )
    console.print()
    console.print("[yellow]Ctrl+C[/] — корректная остановка с подробной статистикой сеанса.")
