"""Подробный отчёт по сеансу: печатается после остановки робота (в том числе по Ctrl+C)."""

from __future__ import annotations

from rich import box
from rich.table import Table
from rich.text import Text

from futures_arb_bot.app.console import CYAN1, center_rule, console, esc, plain_rule
from futures_arb_bot.core.domain.enums import CloseReason, NetworkMode
from futures_arb_bot.core.domain.statistics import SessionReport
from futures_arb_bot.core.engine import formatting
from futures_arb_bot.core.engine.event_log import InMemoryEventLog
from futures_arb_bot.core.engine.formatting import GREY
from futures_arb_bot.core.numbers import format_n0, format_number

_BAR_CHART_WIDTH = 70


def _table(title: str | None = None, *, show_header: bool = True) -> Table:
    return Table(box=box.ROUNDED, title=title, title_style="none", show_header=show_header)


class SessionReportPrinter:
    def __init__(self, log: InMemoryEventLog) -> None:
        self._log = log

    def print(self, report: SessionReport) -> None:
        console.print(center_rule("[cyan]Итоги сеанса[/]", "cyan"))
        console.print()

        self._print_general(report)
        self._print_trades(report)
        self._print_still_open(report)
        self._print_exchanges(report)
        self._print_top_symbols(report)
        self._print_errors()
        self._print_final_pnl(report)

    @staticmethod
    def _print_general(report: SessionReport) -> None:
        table = _table(show_header=False)
        table.add_column("Параметр")
        table.add_column("Значение")

        best = report.best_ever
        table.add_row("[cyan]Режим работы[/]", _describe_mode(report.mode, report.trading_was_enabled))
        table.add_row("[cyan]Начало сеанса[/]", f"{report.started_at.astimezone():%d.%m.%Y %H:%M:%S}")
        table.add_row("[cyan]Длительность[/]", formatting.duration(report.duration))
        table.add_row("[cyan]Тиков сканирования[/]", format_n0(report.scan_ticks))
        table.add_row("[cyan]Тикеров получено[/]", format_n0(report.tickers_processed))
        table.add_row(
            "[cyan]Сигналов найдено[/]",
            f"{format_n0(report.opportunities_detected)} ({format_number(report.opportunities_per_hour, min_decimals=0, max_decimals=0)}/час)",
        )
        table.add_row(
            "[cyan]Лучший спред[/]",
            "—"
            if best is None
            else f"{esc(best.symbol)} · {formatting.pct(best.net_percent)} ({esc(best.long_leg.exchange_id)} → {esc(best.short_leg.exchange_id)})",
        )
        table.add_row("[cyan]Ордеров открыто / ошибок[/]", f"{report.orders_opened} / {report.orders_failed}")
        table.add_row("[cyan]Позиций открыто / закрыто[/]", f"{report.orders_opened} / {len(report.closed_positions)}")

        console.print(table)
        console.print()

    @staticmethod
    def _print_trades(report: SessionReport) -> None:
        if not report.closed_positions:
            return

        table = _table("[cyan]Закрытые арбитражи[/]")
        table.add_column("[cyan]Символ[/]")
        table.add_column("[cyan]Лонг @[/]")
        table.add_column("[cyan]Шорт @[/]")
        table.add_column("[cyan]Выход Л@[/]")
        table.add_column("[cyan]Выход Ш@[/]")
        table.add_column("[cyan]Размер[/]", justify="right")
        table.add_column("[cyan]Причина[/]")
        table.add_column("[cyan]PnL[/]", justify="right")

        for trade in report.closed_positions:
            pnl = formatting.usd(trade.pnl_usd)
            if trade.pnl_usd > 0:
                pnl = f"[green]{pnl}[/]"
            elif trade.pnl_usd < 0:
                pnl = f"[red]{pnl}[/]"

            table.add_row(
                esc(trade.symbol),
                f"{esc(trade.long_exchange_id)} {formatting.price(trade.entry_long)}",
                f"{esc(trade.short_exchange_id)} {formatting.price(trade.entry_short)}",
                formatting.price(trade.exit_long if trade.exit_long is not None else 0),
                formatting.price(trade.exit_short if trade.exit_short is not None else 0),
                formatting.volume(trade.size),
                _describe_reason(trade.reason) + (" · сим." if trade.simulated else ""),
                pnl,
            )

        console.print(table)
        console.print()

    @staticmethod
    def _print_still_open(report: SessionReport) -> None:
        if not report.still_open:
            return

        table = _table("[yellow]Остались открытыми (ClosePositionsOnExit = false)[/]")
        table.add_column("[cyan]Символ[/]")
        table.add_column("[cyan]Лонг[/]")
        table.add_column("[cyan]Шорт[/]")
        table.add_column("[cyan]Размер[/]", justify="right")
        table.add_column("[cyan]Открыта[/]")

        for position in report.still_open:
            table.add_row(
                esc(position.symbol),
                f"{esc(position.long_exchange_id)} @ {formatting.price(position.entry_long)}",
                f"{esc(position.short_exchange_id)} @ {formatting.price(position.entry_short)}",
                formatting.volume(position.matched_size),
                f"{position.opened_at.astimezone():%H:%M:%S}",
            )

        console.print(table)
        console.print()

    @staticmethod
    def _print_exchanges(report: SessionReport) -> None:
        table = _table("[cyan]Биржи[/]")
        table.add_column("[cyan]Биржа[/]")
        table.add_column("[cyan]Режим[/]")
        table.add_column("[cyan]Рынков[/]", justify="right")
        table.add_column("[cyan]Запросов[/]", justify="right")
        table.add_column("[cyan]Ошибок[/]", justify="right")
        table.add_column("[cyan]Задержка[/]", justify="right")
        table.add_column("[cyan]Taker[/]", justify="right")

        for exchange in report.exchanges:
            taker = f"{format_number(exchange.taker_percent, min_decimals=0, max_decimals=4)}%" if exchange.taker_percent > 0 else "—"
            table.add_row(
                esc(exchange.display_name if exchange.display_name.strip() else exchange.id),
                str(exchange.mode),
                format_n0(exchange.markets),
                format_n0(exchange.requests),
                f"[red]{exchange.errors}[/]" if exchange.errors > 0 else "0",
                f"{format_number(exchange.avg_latency_ms, min_decimals=0, max_decimals=0)} мс",
                taker,
            )

        console.print(table)
        console.print()

    @staticmethod
    def _print_top_symbols(report: SessionReport) -> None:
        if not report.top_symbols:
            return

        # горизонтальная гистограмма в духе BarChart из Spectre.Console
        label_width = max(len(symbol) for symbol, _ in report.top_symbols)
        value_width = max(len(str(count)) for _, count in report.top_symbols)
        bar_width = max(1, _BAR_CHART_WIDTH - label_width - value_width - 2)
        maximum = max(count for _, count in report.top_symbols) or 1

        console.print(Text.from_markup("[cyan]Топ символов по числу сигналов[/]"))
        for symbol, count in report.top_symbols:
            bar = "█" * max(1, round(bar_width * count / maximum))
            line = Text(symbol.rjust(label_width) + " ")
            line.append(bar, style=CYAN1)
            line.append(f" {count}")
            console.print(line)
        console.print()

    def _print_errors(self) -> None:
        errors = self._log.errors(5)
        if not errors:
            return

        table = _table("[red]Последние ошибки[/]", show_header=False)
        table.add_column("")
        for error in errors:
            table.add_row(f"[dim]{error.time.astimezone():%H:%M:%S}[/] {esc(error.message)}")

        console.print(table)
        console.print()

    @staticmethod
    def _print_final_pnl(report: SessionReport) -> None:
        if not report.closed_positions:
            return

        pnl = report.realized_pnl_usd
        color = "green" if pnl > 0 else "red" if pnl < 0 else "bright_white"
        suffix = f" [{GREY}](симуляция)[/]" if report.mode == NetworkMode.DRY_RUN else ""
        console.print(plain_rule(GREY))
        console.print(
            f"Итоговая PnL за сеанс: [bold {color}]{formatting.usd(pnl)}[/]{suffix}, уплачено комиссий {formatting.usd(report.fees_paid_usd)}."
        )
        console.print()


def _describe_mode(mode: NetworkMode, trading: bool) -> str:
    if mode == NetworkMode.DRY_RUN:
        return "DRY-RUN (симуляция)"
    if mode == NetworkMode.TESTNET:
        return "TESTNET (торговля включена)" if trading else "TESTNET (мониторинг)"
    if mode == NetworkMode.LIVE:
        return "LIVE (реальная торговля)" if trading else "LIVE (мониторинг)"
    return str(mode)


def _describe_reason(reason: CloseReason) -> str:
    return {
        CloseReason.TAKE_PROFIT: "тейк-профит",
        CloseReason.STOP_LOSS: "стоп-лосс",
        CloseReason.TIMEOUT: "таймаут",
        CloseReason.SESSION_END: "конец сеанса",
        CloseReason.ROLLBACK: "откат",
    }.get(reason, str(reason))
