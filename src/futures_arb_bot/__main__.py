"""Точка входа: ``uv run futures-arb-bot`` / ``python -m futures_arb_bot``."""

from __future__ import annotations

import asyncio
import os
import signal
import sys

from futures_arb_bot.app import bootstrap, notification_cli
from futures_arb_bot.app.cli import CliError, CliOptions, print_help
from futures_arb_bot.app.console import err_console, esc
from futures_arb_bot.app.runner import AppRunner
from futures_arb_bot.core.cancellation import CancellationToken, OperationCanceledError
from futures_arb_bot.core.errors import message_of
from futures_arb_bot.infrastructure.config import ConfigError


class _Interrupts:
    """Первый Ctrl+C — корректная остановка с отчётом, повторный — принудительное завершение."""

    def __init__(self, loop: asyncio.AbstractEventLoop, token: CancellationToken) -> None:
        self._loop = loop
        self._token = token
        self.interrupted = False

    def install(self) -> None:
        signal.signal(signal.SIGINT, self._handle)
        if hasattr(signal, "SIGBREAK"):  # Ctrl+Break в консоли Windows
            signal.signal(signal.SIGBREAK, self._handle)

    def _handle(self, signum: int, frame: object) -> None:
        if self.interrupted:
            os._exit(130)  # повторный Ctrl+C — принудительное завершение

        self.interrupted = True
        sys.stderr.write("\n^C  Останавливаюсь… повторный Ctrl+C — принудительно.\n")
        sys.stderr.flush()
        try:
            self._loop.call_soon_threadsafe(self._token.cancel)
        except RuntimeError:
            pass  # цикл уже остановлен


async def _main_async(cli: CliOptions) -> int:
    token = CancellationToken()
    _Interrupts(asyncio.get_running_loop(), token).install()

    # служебные режимы проверки уведомлений: биржи не подключаются, только MAX Bot API
    if notification_cli.requested(cli):
        return await notification_cli.run(cli, token)

    app = AppRunner(bootstrap.build(cli))
    try:
        try:
            exit_code = await app.run(token)
        except OperationCanceledError:
            err_console.print("Остановлено пользователем.")
            exit_code = 130
        except Exception as ex:
            err_console.print(f"Фатальная ошибка: {esc(message_of(ex))}")
            exit_code = 1

        return exit_code
    finally:
        await app.aclose()


def main(argv: list[str] | None = None) -> int:
    if os.name == "nt":
        # UTF-8 в консоли Windows: кириллица и псевдографика без «кракозябр»
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
            except Exception:
                pass

    try:
        cli = CliOptions.parse(sys.argv[1:] if argv is None else argv)
    except CliError as ex:
        err_console.print(f"[red]{esc(ex)}[/]")
        return 2

    if cli.show_help:
        print_help()
        return 0

    try:
        return asyncio.run(_main_async(cli))
    except ConfigError as ex:
        err_console.print(f"[red]Ошибка конфигурации:[/] {esc(ex)}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
