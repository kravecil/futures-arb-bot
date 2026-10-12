"""Статистика сеанса, снимки дашборда и журнала."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from futures_arb_bot.core.domain.arbitrage import PositionPair, SpreadEstimate
from futures_arb_bot.core.domain.enums import AppLogLevel, CloseReason, NetworkMode


@dataclass(frozen=True, slots=True)
class LogEvent:
    id: int
    time: datetime
    level: AppLogLevel
    message: str


@dataclass(frozen=True, slots=True)
class ExchangeRuntimeStat:
    """Агрегированная статистика по одной бирже за сеанс."""

    id: str
    display_name: str
    mode: NetworkMode
    markets: int
    requests: int
    errors: int
    avg_latency_ms: float
    taker_percent: Decimal


@dataclass(frozen=True, slots=True)
class ClosedPositionRecord:
    symbol: str
    long_exchange_id: str
    short_exchange_id: str
    size: Decimal
    entry_long: Decimal
    entry_short: Decimal
    exit_long: Decimal | None
    exit_short: Decimal | None
    pnl_usd: Decimal
    fees_usd: Decimal
    reason: CloseReason
    opened_at: datetime
    closed_at: datetime
    simulated: bool


@dataclass(frozen=True, slots=True)
class LatencyStats:
    """Замеры скорости реакции за сеанс (мс)."""

    ticks: int
    avg_tick_ms: float
    max_tick_ms: float
    signals: int
    avg_quote_age_ms: float | None
    max_quote_age_ms: float | None


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """Снимок состояния для дашборда (обновляется каждый тик сканера)."""

    updated_at: datetime
    online_exchanges: int
    tracked_symbols: int
    top: list[SpreadEstimate]
    scan_ticks: int
    opportunities: int
    errors: int
    best_net_ever_percent: Decimal
    best_symbol: str | None
    open_positions: int
    realized_pnl_usd: Decimal
    pnl_simulated: bool
    trading_enabled: bool
    last_tick_ms: float | None = None
    """Длительность последнего тика: получение котировок + расчёт спредов."""
    avg_quote_age_ms: float | None = None
    """Средний возраст котировок в сигналах за сеанс (сколько им было в момент решения)."""


@dataclass(frozen=True, slots=True)
class SessionReport:
    """Итоговый отчёт за сеанс (печатается при выходе)."""

    mode: NetworkMode
    started_at: datetime
    ended_at: datetime
    scan_ticks: int
    tickers_processed: int
    opportunities_detected: int
    best_ever: SpreadEstimate | None
    orders_opened: int
    orders_failed: int
    closed_positions: list[ClosedPositionRecord]
    still_open: list[PositionPair]
    exchanges: list[ExchangeRuntimeStat]
    top_symbols: list[tuple[str, int]]
    trading_was_enabled: bool
    latency: LatencyStats | None = None

    @property
    def duration(self) -> timedelta:
        return self.ended_at - self.started_at

    @property
    def opportunities_per_hour(self) -> float:
        hours = self.duration.total_seconds() / 3600
        return self.opportunities_detected / hours if hours > 0.005 else float(self.opportunities_detected)

    @property
    def realized_pnl_usd(self) -> Decimal:
        return sum((p.pnl_usd for p in self.closed_positions), Decimal(0))

    @property
    def fees_paid_usd(self) -> Decimal:
        return sum((p.fees_usd for p in self.closed_positions), Decimal(0))
