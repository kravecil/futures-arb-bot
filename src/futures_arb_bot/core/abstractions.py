"""Абстракции ядра: реализации живут в engine/ и infrastructure/, в тестах — фейки."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.domain.arbitrage import PositionPair, SpreadEstimate
from futures_arb_bot.core.domain.enums import AppLogLevel, CloseReason, NetworkMode
from futures_arb_bot.core.domain.market_data import (
    ExchangeFees,
    FetchTickersResult,
    MarketInfo,
    OrderRequest,
    OrderResult,
    OrderUpdate,
    PositionSnapshot,
    TickerSnapshot,
)
from futures_arb_bot.core.domain.notifications import NotificationDelivery
from futures_arb_bot.core.domain.options import BotOptions, ExchangeConfigEntry
from futures_arb_bot.core.domain.statistics import DashboardSnapshot, LogEvent, SessionReport


class ConfigProvider(Protocol):
    """Доступ к конфигурации (горячая перечитка файлов при изменении)."""

    @property
    def current(self) -> BotOptions: ...

    @property
    def config_directory(self) -> str:
        """Каталог, из которого загружены файлы конфигурации."""
        ...


class EventLog(Protocol):
    """Журнал событий: кольцевой буфер в памяти + опциональный файл."""

    def log(self, level: AppLogLevel, message: str) -> None: ...

    def latest(self, count: int) -> list[LogEvent]: ...

    def tail(self, after_id: int, max_count: int) -> list[LogEvent]: ...

    def errors(self, count: int) -> list[LogEvent]: ...

    def debug(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...

    def success(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...

    def error(self, message: str) -> None: ...


class SpreadCalculator(Protocol):
    def calculate(
        self, cheaper: TickerSnapshot, richer: TickerSnapshot, cheap_fees: ExchangeFees, rich_fees: ExchangeFees
    ) -> SpreadEstimate | None: ...


class SymbolFilter(Protocol):
    def is_allowed(self, market: MarketInfo, quote_volume_24h: object) -> bool: ...


@runtime_checkable
class ExchangeConnector(Protocol):
    """Абстракция над конкретной биржей (реализована через CCXT)."""

    @property
    def id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    @property
    def mode(self) -> NetworkMode: ...

    @property
    def default_fees(self) -> ExchangeFees: ...

    @property
    def market_count(self) -> int: ...

    @property
    def perpetual_markets(self) -> Sequence[MarketInfo]: ...

    async def connect(self, ct: CancellationToken | None = None) -> None: ...

    async def fetch_tickers(self, ct: CancellationToken | None = None) -> FetchTickersResult: ...

    async def place_order(self, request: OrderRequest, ct: CancellationToken | None = None) -> OrderResult: ...

    async def fetch_order(self, order_id: str, symbol: str, ct: CancellationToken | None = None) -> OrderUpdate | None: ...

    async def fetch_positions(self, ct: CancellationToken | None = None) -> list[PositionSnapshot]: ...

    async def fetch_funding_rates_percent(self, ct: CancellationToken | None = None) -> Mapping[str, object]: ...

    async def cancel_order(self, order_id: str, symbol: str, ct: CancellationToken | None = None) -> bool: ...

    async def set_leverage(self, leverage: int, symbol: str, ct: CancellationToken | None = None) -> None: ...

    async def verify_access(self, ct: CancellationToken | None = None) -> bool: ...

    def try_get_fees(self, symbol: str) -> tuple[bool, ExchangeFees]: ...

    async def aclose(self) -> None: ...


class ExchangeFactory(Protocol):
    @property
    def supported_ids(self) -> list[str]: ...

    def create(self, entry: ExchangeConfigEntry, mode: NetworkMode) -> ExchangeConnector: ...


class TradeExecutor(Protocol):
    @property
    def has_open_positions(self) -> bool: ...

    async def process_opportunities(self, candidates: Sequence[SpreadEstimate], ct: CancellationToken) -> None: ...

    async def manage_open_positions(self, tickers_by_exchange: Mapping[str, Mapping[str, TickerSnapshot]], ct: CancellationToken) -> None: ...

    async def close_all(self, reason: CloseReason, ct: CancellationToken) -> None: ...


class StatisticsCollector(Protocol):
    def record_scan_tick(self, tickers_fetched: int) -> None: ...

    def record_opportunity(self, estimate: SpreadEstimate) -> None: ...

    def record_request(self, exchange_id: str, latency: timedelta, success: bool) -> None: ...

    def record_markets(self, exchange_id: str, display_name: str, count: int, fees: ExchangeFees, mode: NetworkMode) -> None: ...

    def record_opened(self, position: PositionPair) -> None: ...

    def record_open_failed(self, symbol: str, error_message: str) -> None: ...

    def record_closed(self, position: PositionPair) -> None: ...

    def snapshot(self, ended_at: datetime) -> SessionReport: ...

    def build_dashboard(self, top: list[SpreadEstimate], online_exchanges: int, tracked_symbols: int) -> DashboardSnapshot: ...


class ArbitrageScanner(Protocol):
    async def run(self, ct: CancellationToken) -> None: ...

    async def scan_once(self, ct: CancellationToken) -> None: ...

    @property
    def snapshot(self) -> DashboardSnapshot | None: ...


class SpreadNotifier(Protocol):
    """Неблокирующий отправитель уведомлений: очередь внутренняя, сбои логируются."""

    def notify(self, estimate: SpreadEstimate) -> None: ...


class NotificationTransport(Protocol):
    """Доставка текста уведомления во внешний мессенджер (с повторами внутри)."""

    async def send(self, text: str, ct: CancellationToken | None = None) -> NotificationDelivery: ...
