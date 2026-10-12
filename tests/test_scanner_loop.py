"""Цикл сканирования соблюдает general.refresh_interval_ms (пауза между тиками)."""

import asyncio
import time

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.engine.arbitrage_scanner import DefaultArbitrageScanner
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.spread_calculator import DefaultSpreadCalculator
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.symbol_filter import DefaultSymbolFilter

from .fakes import BASE_TIME, FakeConfigProvider, FakeConnector, FakeExecutor, FakeNotifier, FixedClock, make_log


async def run_for(seconds: float, interval_ms: int) -> int:
    config = FakeConfigProvider()
    config.current.general.refresh_interval_ms = interval_ms
    registry = ConnectorRegistry()
    await registry.replace([FakeConnector("a"), FakeConnector("b")])
    clock = FixedClock(BASE_TIME)
    stats = DefaultStatisticsCollector(config, clock)
    scanner = DefaultArbitrageScanner(
        config,
        registry,
        DefaultSpreadCalculator(config, clock),
        DefaultSymbolFilter(config),
        FakeExecutor(),
        stats,
        FakeNotifier(),
        make_log(config, clock),
        clock,
    )

    ct = CancellationToken()
    task = asyncio.create_task(scanner.run(ct))
    await asyncio.sleep(seconds)
    ct.cancel()
    started = time.monotonic()
    await task
    assert time.monotonic() - started < 0.2  # пауза прерывается остановкой сразу
    return stats.snapshot(BASE_TIME).scan_ticks


async def test_ticks_respect_refresh_interval():
    # 0.55 с при интервале 200 мс — тики в 0, 200 и 400 мс
    assert await run_for(0.55, 200) == 3


async def test_long_interval_is_interrupted_by_cancellation():
    assert await run_for(0.1, 60_000) == 1
