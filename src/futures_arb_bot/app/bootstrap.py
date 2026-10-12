"""Сборка зависимостей приложения (ленивые одиночки — как DI-контейнер прежней версии)."""

from __future__ import annotations

from functools import cached_property

from futures_arb_bot.app.cli import CliOptions
from futures_arb_bot.core.clock import Clock, SystemClock
from futures_arb_bot.core.engine import logging_bridge
from futures_arb_bot.core.engine.arbitrage_scanner import DefaultArbitrageScanner
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.event_log import InMemoryEventLog
from futures_arb_bot.core.engine.spread_calculator import DefaultSpreadCalculator
from futures_arb_bot.core.engine.spread_notifier import QueuedSpreadNotifier
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.symbol_filter import DefaultSymbolFilter
from futures_arb_bot.core.engine.trade_executor import ArbTradeExecutor
from futures_arb_bot.infrastructure import config as config_loader
from futures_arb_bot.infrastructure.ccxt_factory import CcxtExchangeFactory
from futures_arb_bot.infrastructure.config import YamlConfigProvider
from futures_arb_bot.infrastructure.max_messenger import MaxMessengerClient


class Services:
    """Зависимости создаются при первом обращении и живут весь сеанс."""

    def __init__(self, cli: CliOptions, clock: Clock | None = None) -> None:
        self.cli = cli
        self.clock: Clock = clock or SystemClock()
        self.config: YamlConfigProvider = config_loader.load(cli.config_dir)

    @cached_property
    def event_log(self) -> InMemoryEventLog:
        return InMemoryEventLog(self.config, self.clock)

    @cached_property
    def registry(self) -> ConnectorRegistry:
        return ConnectorRegistry()

    @cached_property
    def factory(self) -> CcxtExchangeFactory:
        return CcxtExchangeFactory(self.event_log, self.clock)

    @cached_property
    def statistics(self) -> DefaultStatisticsCollector:
        return DefaultStatisticsCollector(self.config, self.clock)

    @cached_property
    def executor(self) -> ArbTradeExecutor:
        return ArbTradeExecutor(self.config, self.registry, self.statistics, self.event_log, self.clock)

    @cached_property
    def max_client(self) -> MaxMessengerClient:
        return MaxMessengerClient(self.config, self.clock)

    @cached_property
    def notifier(self) -> QueuedSpreadNotifier:
        # отправитель всегда реальный: он сам молчит, пока раздел notifications выключен или не заполнен,
        # поэтому файл можно создать и включить без перезапуска робота
        return QueuedSpreadNotifier(self.config, self.max_client, self.event_log, self.clock)

    @cached_property
    def scanner(self) -> DefaultArbitrageScanner:
        return DefaultArbitrageScanner(
            self.config,
            self.registry,
            DefaultSpreadCalculator(self.config, self.clock),
            DefaultSymbolFilter(self.config),
            self.executor,
            self.statistics,
            self.notifier,
            self.event_log,
            self.clock,
        )

    def attach_logging(self) -> None:
        """Все записи logging приложения уходят в журнал событий (дашборд + файл)."""
        logging_bridge.attach(self.event_log)

    async def aclose(self) -> None:
        if "notifier" in self.__dict__:
            await self.notifier.aclose()
        if "max_client" in self.__dict__:
            await self.max_client.aclose()
        if "registry" in self.__dict__:
            await self.registry.aclose()
        if "event_log" in self.__dict__:
            self.event_log.close()


def build(cli: CliOptions) -> Services:
    services = Services(cli)
    services.attach_logging()
    return services
