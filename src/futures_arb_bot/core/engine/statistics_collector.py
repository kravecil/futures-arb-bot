"""Сборщик статистики сеанса."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from futures_arb_bot.core.abstractions import ConfigProvider
from futures_arb_bot.core.clock import Clock
from futures_arb_bot.core.domain.arbitrage import PositionPair, SpreadEstimate
from futures_arb_bot.core.domain.enums import CloseReason, NetworkMode
from futures_arb_bot.core.domain.market_data import ExchangeFees
from futures_arb_bot.core.domain.statistics import ClosedPositionRecord, DashboardSnapshot, ExchangeRuntimeStat, SessionReport
from futures_arb_bot.core.numbers import ZERO


@dataclass(slots=True)
class _ExchangeAccumulator:
    display_name: str
    mode: NetworkMode = NetworkMode.DRY_RUN
    markets: int = 0
    taker_percent: Decimal = ZERO
    requests: int = 0
    errors: int = 0
    latency_sum_ms: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.latency_sum_ms / self.requests if self.requests > 0 else 0.0


class DefaultStatisticsCollector:
    def __init__(self, config: ConfigProvider, clock: Clock) -> None:
        self._config = config
        self._clock = clock
        self._started_at = clock.now()
        self._scan_ticks = 0
        self._tickers_processed = 0
        self._opportunities = 0
        self._orders_opened = 0
        self._orders_failed = 0
        self._exchanges: dict[str, _ExchangeAccumulator] = {}
        self._symbol_hits: dict[str, int] = {}
        self._closed: list[ClosedPositionRecord] = []
        self._open: list[PositionPair] = []
        self._best_ever: SpreadEstimate | None = None

    def record_scan_tick(self, tickers_fetched: int) -> None:
        self._scan_ticks += 1
        self._tickers_processed += tickers_fetched

    def record_opportunity(self, estimate: SpreadEstimate) -> None:
        self._opportunities += 1
        self._symbol_hits[estimate.symbol] = self._symbol_hits.get(estimate.symbol, 0) + 1
        if self._best_ever is None or estimate.net_percent > self._best_ever.net_percent:
            self._best_ever = estimate

    def record_request(self, exchange_id: str, latency: timedelta, success: bool) -> None:
        acc = self._accumulator(exchange_id)
        acc.requests += 1
        acc.latency_sum_ms += latency.total_seconds() * 1000.0
        if not success:
            acc.errors += 1

    def record_markets(self, exchange_id: str, display_name: str, count: int, fees: ExchangeFees, mode: NetworkMode) -> None:
        acc = self._accumulator(exchange_id)
        acc.display_name = display_name
        acc.markets = count
        acc.taker_percent = fees.taker_percent
        acc.mode = mode

    def record_opened(self, position: PositionPair) -> None:
        self._orders_opened += 1
        self._open.append(position)

    def record_open_failed(self, symbol: str, error_message: str) -> None:
        self._orders_failed += 1

    def record_closed(self, position: PositionPair) -> None:
        record = ClosedPositionRecord(
            position.symbol,
            position.long_exchange_id,
            position.short_exchange_id,
            position.closed_matched_volume,
            position.entry_long,
            position.entry_short,
            position.exit_long,
            position.exit_short,
            position.realized_pnl_usd if position.realized_pnl_usd is not None else ZERO,
            position.fees_entry_usd + position.fees_exit_usd,
            position.reason if position.reason is not None else CloseReason.SESSION_END,
            position.opened_at,
            position.closed_at if position.closed_at is not None else self._clock.now(),
            position.simulated,
        )
        if position in self._open:
            self._open.remove(position)
        self._closed.append(record)

    def snapshot(self, ended_at: datetime) -> SessionReport:
        exchanges = sorted(
            (
                ExchangeRuntimeStat(
                    key, acc.display_name, acc.mode, acc.markets, acc.requests, acc.errors, round(acc.avg_latency_ms, 1), acc.taker_percent
                )
                for key, acc in self._exchanges.items()
            ),
            key=lambda s: s.id.lower(),
        )
        top_symbols = sorted(self._symbol_hits.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        return SessionReport(
            mode=self._config.current.general.network_mode,
            started_at=self._started_at,
            ended_at=ended_at,
            scan_ticks=self._scan_ticks,
            tickers_processed=self._tickers_processed,
            opportunities_detected=self._opportunities,
            best_ever=self._best_ever,
            orders_opened=self._orders_opened,
            orders_failed=self._orders_failed,
            closed_positions=list(self._closed),
            still_open=list(self._open),
            exchanges=exchanges,
            top_symbols=top_symbols,
            trading_was_enabled=self._config.current.arbitrage.enabled,
        )

    def build_dashboard(self, top: list[SpreadEstimate], online_exchanges: int, tracked_symbols: int) -> DashboardSnapshot:
        options = self._config.current
        best = self._best_ever
        return DashboardSnapshot(
            updated_at=self._clock.now(),
            online_exchanges=online_exchanges,
            tracked_symbols=tracked_symbols,
            top=top,
            scan_ticks=self._scan_ticks,
            opportunities=self._opportunities,
            errors=sum(a.errors for a in self._exchanges.values()),
            best_net_ever_percent=best.net_percent if best is not None else ZERO,
            best_symbol=best.symbol if best is not None else None,
            open_positions=len(self._open),
            realized_pnl_usd=sum((p.pnl_usd for p in self._closed), ZERO),
            pnl_simulated=options.general.network_mode == NetworkMode.DRY_RUN,
            trading_enabled=options.arbitrage.enabled,
        )

    def _accumulator(self, exchange_id: str) -> _ExchangeAccumulator:
        acc = self._exchanges.get(exchange_id)
        if acc is None:
            acc = _ExchangeAccumulator(display_name=exchange_id)
            self._exchanges[exchange_id] = acc
        return acc
