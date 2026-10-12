"""Переиспользуемые визуальные блоки дашборда (таблица возможностей)."""

from __future__ import annotations

from rich import box
from rich.table import Table

from futures_arb_bot.app.console import CYAN1, esc
from futures_arb_bot.core.domain.options import ArbitrageOptions
from futures_arb_bot.core.domain.statistics import DashboardSnapshot
from futures_arb_bot.core.engine.formatting import SILVER, WHITE, pct, price, volume


def opportunities_table(snapshot: DashboardSnapshot | None, arbitrage: ArbitrageOptions) -> Table:
    table = Table(box=box.ROUNDED, border_style=CYAN1, expand=True, title_style="none")

    if snapshot is None:
        table.title = "[cyan]Топ возможностей[/]"
        table.add_column("[cyan]Символ[/]")
        table.add_row("[bright_black]ожидание данных…[/]")
        return table

    table.title = (
        f"[cyan]Топ возможностей[/] [bright_black]· отслеживается пар: {snapshot.tracked_symbols} · "
        f"порог входа {pct(arbitrage.min_spread_percent_up)}[/]"
    )
    table.add_column("[cyan]Символ[/]", no_wrap=True)
    table.add_column("[cyan]Лонг (купить)[/]")
    table.add_column("[cyan]Шорт (продать)[/]")
    table.add_column("[cyan]Спред[/]", justify="right")
    table.add_column("[cyan]Нетто[/]", justify="right")
    table.add_column("[cyan]Объём 24ч[/]", justify="right")

    if not snapshot.top:
        table.add_row("[bright_black]—[/]", "[bright_black]пока нет пересекающихся пар с положительным нетто-спредом[/]", "", "", "", "")
        return table

    for estimate in snapshot.top:
        strong = estimate.net_percent >= arbitrage.min_spread_percent_up
        symbol = f"[bold {WHITE}]{esc(estimate.symbol)}[/] ▸" if strong else esc(estimate.symbol)

        if strong:
            net = f"[bold green]{pct(estimate.net_percent)}[/]"
        elif estimate.net_percent > 0:
            net = f"[green]{pct(estimate.net_percent)}[/]"
        else:
            net = f"[{SILVER}]{pct(estimate.net_percent)}[/]"

        table.add_row(
            symbol,
            f"[{WHITE}]{esc(estimate.long_leg.exchange_id)}[/] [bright_black]@[/] {price(estimate.long_leg.price)}",
            f"[{WHITE}]{esc(estimate.short_leg.exchange_id)}[/] [bright_black]@[/] {price(estimate.short_leg.price)}",
            pct(estimate.gross_percent),
            net,
            f"[{SILVER}]{volume(estimate.quote_volume_usd)}[/]",
        )

    return table
