"""
Live-дашборд на rich: шапка состояния, таблица лучших возможностей, журнал событий
и строка статистики. Обновляется 2–3 раза в секунду.
"""

from __future__ import annotations

from collections.abc import Callable

from rich import box
from rich.console import RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from futures_arb_bot.app.console import console, esc
from futures_arb_bot.app.ui.views import opportunities_table
from futures_arb_bot.core.abstractions import ConfigProvider
from futures_arb_bot.core.cancellation import CancellationToken, OperationCanceledError, delay
from futures_arb_bot.core.domain.enums import AppLogLevel, NetworkMode
from futures_arb_bot.core.domain.options import UiOptions
from futures_arb_bot.core.domain.statistics import DashboardSnapshot
from futures_arb_bot.core.engine import formatting
from futures_arb_bot.core.engine.event_log import InMemoryEventLog
from futures_arb_bot.core.engine.formatting import GREY, SILVER, WHITE, level_color
from futures_arb_bot.core.errors import message_of

_MODES = {
    NetworkMode.DRY_RUN: "[yellow]DRY-RUN[/]",
    NetworkMode.TESTNET: "[blue]TESTNET[/]",
    NetworkMode.LIVE: "[bold red]LIVE[/]",
}


def log_line(time_text: str, level: AppLogLevel, message: str) -> str:
    return f"[dim]{time_text}[/] [{level_color(level)}]{esc(message)}[/]"


class Dashboard:
    def __init__(self, log: InMemoryEventLog, config: ConfigProvider) -> None:
        self._log = log
        self._config = config

    async def run(self, ct: CancellationToken, snapshot: Callable[[], DashboardSnapshot | None]) -> None:
        ui = self._config.current.general.console_ui

        root = Layout(name="root")
        root.split_column(Layout(name="header", size=6), Layout(name="body"), Layout(name="footer", size=3))
        root["body"].split_row(Layout(name="opportunities"), Layout(name="log", size=52))

        try:
            with Live(root, console=console, auto_refresh=False, transient=True, redirect_stdout=True, redirect_stderr=True) as live:
                while not ct.is_cancellation_requested:
                    self._render(root, snapshot(), ui)
                    live.refresh()
                    try:
                        await delay(ui.refresh_ms / 1000, ct)
                    except OperationCanceledError:
                        break

                # финальный кадр перед остановкой
                self._render(root, snapshot(), ui)
                live.refresh()
        except OperationCanceledError:
            raise
        except Exception as ex:
            self._log.warning(f"Дашборд недоступен ({message_of(ex)}); продолжаю в текстовом режиме.")

    def _render(self, root: Layout, snapshot: DashboardSnapshot | None, ui: UiOptions) -> None:
        root["header"].update(self._header(snapshot))
        root["opportunities"].update(opportunities_table(snapshot, self._config.current.arbitrage))
        root["log"].update(self._log_panel(ui.log_lines))
        root["footer"].update(self._footer(snapshot))

    def _header(self, snapshot: DashboardSnapshot | None) -> RenderableType:
        options = self._config.current
        mode = _MODES.get(options.general.network_mode, "?")

        grid = Table.grid(padding=(0, 2))
        for _ in range(6):
            grid.add_column()

        if snapshot is None:
            grid.add_row("[bold cyan]FUTURES ARB BOT[/]", f"[{GREY}]ожидание данных…[/]", "", "", "", "")
            return grid

        pnl = formatting.usd(snapshot.realized_pnl_usd)
        if snapshot.realized_pnl_usd > 0:
            pnl_markup = f"[green]{pnl}[/]"
        elif snapshot.realized_pnl_usd < 0:
            pnl_markup = f"[red]{pnl}[/]"
        else:
            pnl_markup = pnl

        best = "—" if snapshot.best_symbol is None else f"{formatting.pct(snapshot.best_net_ever_percent)} ({esc(snapshot.best_symbol)})"
        trading = "[green]вкл[/]" if snapshot.trading_enabled else f"[{SILVER}]выкл[/]"

        grid.add_row(
            "[bold cyan]FUTURES ARB BOT[/]",
            f"режим {mode}",
            f"биржи: [{WHITE}]{snapshot.online_exchanges}[/]",
            f"пары: [{WHITE}]{snapshot.tracked_symbols}[/]",
            f"позиции: [{WHITE}]{snapshot.open_positions}[/]",
            f"PnL: {pnl_markup}{f' [{GREY}](симуляция)[/]' if snapshot.pnl_simulated else ''}",
        )
        grid.add_row(
            f"[{GREY}]обновлено[/] {snapshot.updated_at.astimezone():%H:%M:%S}",
            f"[{GREY}]тиков[/] {snapshot.scan_ticks}",
            f"[{GREY}]сигналов[/] {snapshot.opportunities}",
            f"[{GREY}]ошибок[/] {snapshot.errors}",
            f"[{GREY}]лучший нетто[/] {best}",
            f"[{GREY}]торговля[/] {trading}",
        )
        return grid

    def _log_panel(self, lines: int) -> RenderableType:
        # последние события без отладочных подробностей (иначе панель тонет в служебных строках)
        events = self._log.latest(lines, min_level=AppLogLevel.INFO)
        if not events:
            text = f"[{GREY}]журнал пуст[/]"
        else:
            text = "\n".join(log_line(f"{e.time.astimezone():%H:%M:%S}", e.level, e.message) for e in events)

        return Panel(Text.from_markup(text), box=box.ROUNDED, border_style=GREY, title="[cyan]Журнал[/]", title_align="left")

    def _footer(self, snapshot: DashboardSnapshot | None) -> RenderableType:
        if snapshot is None:
            return Text.from_markup(f"[{GREY}]подключение к биржам…[/]")

        summary = formatting.join(
            " ",
            [
                "[cyan]FuturesArbBot[/]",
                f"тиков [{WHITE}]{snapshot.scan_ticks}[/]",
                f"сигналов [{WHITE}]{snapshot.opportunities}[/]",
                f"позиций [{WHITE}]{snapshot.open_positions}[/]",
                f"ошибок [{WHITE}]{snapshot.errors}[/]",
                f"тик [{WHITE}]{snapshot.last_tick_ms:.0f}[/] мс" if snapshot.last_tick_ms is not None else None,
                f"котировки [{WHITE}]{snapshot.avg_quote_age_ms:.0f}[/] мс" if snapshot.avg_quote_age_ms is not None else None,
                "[dim]Ctrl+C — остановка и отчёт[/]",
            ],
        )
        return Text.from_markup(summary)
