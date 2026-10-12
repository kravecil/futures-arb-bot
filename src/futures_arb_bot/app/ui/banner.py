"""Стартовый баннер с настройками сеанса."""

from __future__ import annotations

from rich.table import Table

from futures_arb_bot.app.console import LeftRule, console, esc, figlet
from futures_arb_bot.core.abstractions import ConfigProvider
from futures_arb_bot.core.domain.enums import MarketDataSource, NetworkMode
from futures_arb_bot.core.engine.formatting import GREY, SILVER, pct
from futures_arb_bot.core.numbers import dec_str

_MODES = {
    NetworkMode.DRY_RUN: "[yellow]DRY-RUN[/] [bright_black](симуляция, ордера не отправляются)[/]",
    NetworkMode.TESTNET: "[blue]TESTNET[/] [bright_black](тестовая сеть)[/]",
    NetworkMode.LIVE: "[bold red]LIVE[/] [red](реальные деньги!)[/]",
}


def print_banner(config: ConfigProvider) -> None:
    options = config.current
    arbitrage = options.arbitrage

    console.print(figlet("FUTURES ARB"))
    console.print(LeftRule(f"[{GREY}]межбиржевой арбитраж фьючерсов · CCXT[/]"))
    console.print()

    mode = _MODES.get(options.general.network_mode, "?")
    trading = (
        "[green]включена[/] [bright_black](ордера исполняются)[/]"
        if arbitrage.enabled
        else f"[{SILVER}]выключена[/] [bright_black](только мониторинг спредов)[/]"
    )

    grid = Table.grid(padding=(0, 1))
    grid.add_column()
    grid.add_column()
    grid.add_row("[bright_black]Режим сети:[/]", mode)
    grid.add_row("[bright_black]Торговля:[/]", trading)
    grid.add_row(
        "[bright_black]Пороги спреда:[/]",
        f"вход {pct(arbitrage.min_spread_percent_up)} / фиксация {pct(arbitrage.min_spread_percent_down)} / стоп {pct(arbitrage.stop_loss_spread_percent)}",
    )
    grid.add_row(
        "[bright_black]Ордер:[/]",
        f"{dec_str(arbitrage.order_size_usd)} USD × {arbitrage.leverage} (плечо), комиссия {'учитывается' if arbitrage.include_fees else 'НЕ учитывается'}",
    )
    md = options.general.market_data
    if md.source == MarketDataSource.WEBSOCKET:
        quotes = (
            f"[green]WebSocket[/] [bright_black](расчёт каждые {md.scan_interval_ms} мс; поток молчит > {md.stale_after_ms} мс — "
            f"REST раз в {options.general.refresh_interval_ms} мс)[/]"
        )
    else:
        quotes = f"REST [bright_black](опрос раз в {options.general.refresh_interval_ms} мс)[/]"
    grid.add_row("[bright_black]Котировки:[/]", quotes)
    grid.add_row("[bright_black]Конфигурация:[/]", esc(config.config_directory))

    console.print(grid)
    console.print()
    console.print("[bright_black]Остановка: Ctrl+C — корректное завершение с итоговой статистикой.[/]")
    console.print()
