"""
Сердце робота: опрашивает все включённые биржи, строит «книгу» котировок по символам,
ищет межбиржевые расхождения и передаёт сигналы исполнителю.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from futures_arb_bot.core.abstractions import (
    ConfigProvider,
    EventLog,
    ExchangeConnector,
    SpreadCalculator,
    SpreadNotifier,
    StatisticsCollector,
    SymbolFilter,
    TradeExecutor,
)
from futures_arb_bot.core.cancellation import CancellationToken, OperationCanceledError, delay
from futures_arb_bot.core.clock import Clock
from futures_arb_bot.core.domain.arbitrage import SpreadEstimate
from futures_arb_bot.core.domain.market_data import ExchangeFees, FetchTickersResult, TickerSnapshot
from futures_arb_bot.core.domain.options import BotOptions
from futures_arb_bot.core.domain.statistics import DashboardSnapshot
from futures_arb_bot.core.engine import formatting
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.logging_bridge import get_logger
from futures_arb_bot.core.errors import message_of

_FALLBACK_FEES = ExchangeFees(Decimal("0.1"), Decimal("0.05"))

Fetched = list[tuple[ExchangeConnector, FetchTickersResult]]
FundingMap = dict[str, Mapping[str, Decimal]]


class DefaultArbitrageScanner:
    def __init__(
        self,
        config: ConfigProvider,
        registry: ConnectorRegistry,
        calculator: SpreadCalculator,
        symbol_filter: SymbolFilter,
        executor: TradeExecutor,
        stats: StatisticsCollector,
        notifier: SpreadNotifier,
        log: EventLog,
        clock: Clock,
    ) -> None:
        self._config = config
        self._registry = registry
        self._calculator = calculator
        self._filter = symbol_filter
        self._executor = executor
        self._stats = stats
        self._notifier = notifier
        self._log = log
        self._clock = clock
        self._logger = get_logger("ArbitrageScanner")
        self._connectors: dict[str, ExchangeConnector] = {}
        self._allowed_symbols: dict[str, set[str]] = {}
        self._last_signal_log: dict[str, datetime] = {}
        self._last_funding_skip_log: dict[str, datetime] = {}
        self._snapshot: DashboardSnapshot | None = None
        self._last_tick_ms: float | None = None
        self._prepared = False

    @property
    def snapshot(self) -> DashboardSnapshot | None:
        return self._snapshot

    async def run(self, ct: CancellationToken) -> None:
        self._ensure_prepared()
        total = sum(len(s) for s in self._allowed_symbols.values())
        self._log.info(f"Сканер запущен: бирж {len(self._connectors)}, пар после фильтра {total}")

        while not ct.is_cancellation_requested:
            started = time.monotonic()
            try:
                await self.scan_once(ct)
                # тики не чаще refresh_interval_ms: опрос занимает часть интервала, ждём только остаток
                interval = self._config.current.general.refresh_interval_ms / 1000
                await delay(interval - (time.monotonic() - started), ct)
            except Exception as ex:
                if isinstance(ex, OperationCanceledError) and ct.is_cancellation_requested:
                    break
                self._log.error(f"Сбой цикла сканирования: {message_of(ex)}")
                self._logger.debug("scan loop failure", exc_info=ex)
                try:
                    await delay(1.0, ct)
                except OperationCanceledError:
                    break

        self._log.info("Сканер остановлен.")

    async def scan_once(self, ct: CancellationToken) -> None:
        self._ensure_prepared()
        options = self._config.current
        tick_started = time.monotonic()
        fetched = await self._fetch_all(ct)
        total_fetched = sum(len(result.tickers) for _, result in fetched)
        funding = await self._fetch_funding(ct)

        estimates, tracked_symbols = self._evaluate(fetched, funding, options)
        tick_ms = (time.monotonic() - tick_started) * 1000

        self._stats.record_scan_tick(total_fetched)
        self._stats.record_tick_duration(tick_ms)
        self._last_tick_ms = tick_ms
        self._log.debug(f"[scan] тик {tick_ms:.0f} мс")
        for estimate in estimates:
            self._stats.record_opportunity(estimate)

        top = estimates[: max(0, options.general.console_ui.top_rows)]
        self._snapshot = self._stats.build_dashboard(top, len(self._connectors), tracked_symbols)

        above_spread = [e for e in estimates if e.net_percent >= options.arbitrage.min_spread_percent_up]

        # фильтр по фандингу: чистый фандинг позиции за интервал = рейт шорта − рейт лонга;
        # строго ниже предела — фандинг съедает спред, и сделка пропускается целиком
        limit = options.arbitrage.min_funding_rate_percent
        candidates: list[SpreadEstimate] = []
        for candidate in above_spread:
            net_funding = _net_funding_percent(candidate)
            if net_funding is not None and net_funding < limit:
                self._log_funding_skip(candidate, limit)
                continue
            candidates.append(candidate)

        # возраст котировок в момент решения: сколько прошло с самой старой котировки сигнала
        decided_at = self._clock.now()
        for candidate in candidates:
            age = _quote_age_ms(candidate, decided_at)
            if age is not None:
                self._stats.record_quote_age(age)

        self._log_signals(candidates)
        self._notify_signals(candidates)

        if options.arbitrage.enabled and candidates:
            await self._executor.process_opportunities(candidates, ct)

        if options.arbitrage.enabled or self._executor.has_open_positions:
            by_exchange = {connector.id: result.tickers for connector, result in fetched}
            await self._executor.manage_open_positions(by_exchange, ct)

    def _ensure_prepared(self) -> None:
        """Строит карту бирж и множества разрешённых символов (один раз за сеанс)."""
        if self._prepared:
            return
        self._prepare()
        self._prepared = True

    def _prepare(self) -> None:
        connectors = self._registry.connectors
        self._connectors = {c.id: c for c in connectors}
        self._allowed_symbols = {}

        for connector in connectors:
            allowed = {m.symbol for m in connector.perpetual_markets if self._filter.is_allowed(m, 0)}
            self._allowed_symbols[connector.id] = allowed
            self._stats.record_markets(connector.id, connector.display_name, connector.market_count, connector.default_fees, connector.mode)
            self._log.info(f"[{connector.display_name}] режим {connector.mode}, рынков {connector.market_count}, после фильтра {len(allowed)}")

    async def _fetch_all(self, ct: CancellationToken) -> Fetched:
        async def fetch(connector: ExchangeConnector) -> tuple[ExchangeConnector, FetchTickersResult | None]:
            try:
                result = await connector.fetch_tickers(ct)
                self._stats.record_request(connector.id, result.latency, success=True)
                return connector, result
            except OperationCanceledError:
                raise
            except Exception as ex:
                self._stats.record_request(connector.id, timedelta(0), success=False)
                self._log.error(f"[{connector.display_name}] не удалось получить тикеры: {message_of(ex)}")
                self._logger.debug(f"fetch tickers failed for {connector.id}", exc_info=ex)
                return connector, None

        results = await asyncio.gather(*(fetch(c) for c in self._registry.connectors), return_exceptions=True)
        _reraise_first(results)
        return [(c, r) for c, r in results if r is not None]  # type: ignore[misc]

    async def _fetch_funding(self, ct: CancellationToken) -> FundingMap:
        """Фандинг-рейты по всем биржам (коннекторы кэшируют сами); ошибка одной биржи = «данных нет»."""

        async def fetch(connector: ExchangeConnector) -> tuple[str, Mapping[str, Decimal]]:
            try:
                return connector.id, await connector.fetch_funding_rates_percent(ct)  # type: ignore[return-value]
            except OperationCanceledError:
                raise
            except Exception as ex:
                self._log.debug(f"[funding] {connector.id}: {message_of(ex)}")
                return connector.id, {}

        results = await asyncio.gather(*(fetch(c) for c in self._registry.connectors), return_exceptions=True)
        _reraise_first(results)
        return dict(results)  # type: ignore[arg-type]

    def _evaluate(self, fetched: Fetched, funding: FundingMap, options: BotOptions) -> tuple[list[SpreadEstimate], int]:
        """Ищет лучшие пары «дешёвая/дорогая биржа» по каждому символу."""
        symbols_options = options.symbols
        volume_minimum = symbols_options.min_quote_volume24h_usd
        max_age = timedelta(seconds=symbols_options.max_ticker_age_seconds)
        max_skew = timedelta(seconds=symbols_options.max_ticker_skew_seconds)
        now = self._clock.now()

        book: dict[str, dict[str, TickerSnapshot]] = {}
        for connector, result in fetched:
            allowed = self._allowed_symbols.get(connector.id)
            if allowed is None:
                continue

            for symbol, ticker in result.tickers.items():
                if symbol not in allowed:
                    continue

                # объём = 0 означает «биржа не отдаёт оборот в тикере» — такие символы не отбрасываем
                if ticker.quote_volume_24h > 0 and ticker.quote_volume_24h < volume_minimum:
                    continue

                if ticker.timestamp is not None and ticker.timestamp < now - max_age:
                    continue

                book.setdefault(symbol, {})[connector.id] = ticker

        cross_exchange = [(symbol, quotes, max(t.quote_volume_24h for t in quotes.values())) for symbol, quotes in book.items() if len(quotes) >= 2]

        total = sum(len(result.tickers) for _, result in fetched)
        self._log.debug(f"[scan] тикеров после фильтра бирж: {total}, символов в книге: {len(book)}, на ≥2 биржах: {len(cross_exchange)}")

        tracked_symbols = len(cross_exchange)

        # ограничение числа отслеживаемых пар — берём самые ликвидные
        if symbols_options.max_symbols > 0 and len(cross_exchange) > symbols_options.max_symbols:
            cross_exchange = sorted(cross_exchange, key=lambda x: x[2], reverse=True)[: symbols_options.max_symbols]
            tracked_symbols = len(cross_exchange)

        estimates: list[SpreadEstimate] = []
        for symbol, quotes, _ in cross_exchange:
            cheaper: TickerSnapshot | None = None
            richer: TickerSnapshot | None = None
            for ticker in quotes.values():
                if cheaper is None or ticker.ask < cheaper.ask:
                    cheaper = ticker
                if richer is None or ticker.bid > richer.bid:
                    richer = ticker

            if cheaper is None or richer is None or cheaper.exchange_id == richer.exchange_id:
                continue

            # котировки разных бирж снимаются с разными задержками: если одна нога старше другой
            # больше чем на max_ticker_skew_seconds, расхождение мог породить сдвиг рынка — пропускаем
            if cheaper.timestamp is not None and richer.timestamp is not None and abs(cheaper.timestamp - richer.timestamp) > max_skew:
                continue

            estimate = self._calculator.calculate(
                cheaper, richer, self._fees_of(cheaper.exchange_id, symbol), self._fees_of(richer.exchange_id, symbol)
            )
            if estimate is None:
                continue

            long_funding = _funding_of(funding, cheaper.exchange_id, symbol)
            short_funding = _funding_of(funding, richer.exchange_id, symbol)
            if long_funding is not None or short_funding is not None:
                estimate = replace(
                    estimate,
                    long_leg=replace(estimate.long_leg, funding_percent=long_funding),
                    short_leg=replace(estimate.short_leg, funding_percent=short_funding),
                )

            estimate = replace(
                estimate,
                long_leg=replace(estimate.long_leg, quoted_at=cheaper.timestamp),
                short_leg=replace(estimate.short_leg, quoted_at=richer.timestamp),
            )

            estimates.append(estimate)

        estimates.sort(key=lambda e: e.net_percent, reverse=True)
        return estimates, tracked_symbols

    def _fees_of(self, exchange_id: str, symbol: str) -> ExchangeFees:
        connector = self._connectors.get(exchange_id)
        if connector is None:
            return _FALLBACK_FEES
        found, fees = connector.try_get_fees(symbol)
        return fees if found else connector.default_fees

    def _log_funding_skip(self, candidate: SpreadEstimate, limit: Decimal) -> None:
        """Отказ по фандингу пишется в журнал не чаще раза в минуту на символ."""
        now = self._clock.now()
        last = self._last_funding_skip_log.get(candidate.symbol)
        if last is not None and now - last < timedelta(minutes=1):
            return

        self._last_funding_skip_log[candidate.symbol] = now
        self._log.info(
            f"Пропуск {candidate.symbol}: фандинг лонг {formatting.optional_pct(candidate.long_leg.funding_percent)} / "
            f"шорт {formatting.optional_pct(candidate.short_leg.funding_percent)}, чистый для позиции "
            f"{formatting.optional_pct(_net_funding_percent(candidate))} — ниже предела {formatting.pct(limit)}, сделка не открывается"
        )

    def _log_signals(self, candidates: list[SpreadEstimate]) -> None:
        """Сигналы пишутся в журнал не чаще раза в минуту на символ."""
        now = self._clock.now()
        for c in candidates:
            last = self._last_signal_log.get(c.symbol)
            if last is not None and now - last < timedelta(minutes=1):
                continue

            self._last_signal_log[c.symbol] = now
            self._log.success(
                f"Сигнал {c.symbol}: лонг {c.long_leg.exchange_id} @ {formatting.price(c.long_leg.price)} → "
                f"шорт {c.short_leg.exchange_id} @ {formatting.price(c.short_leg.price)}, нетто {formatting.pct(c.net_percent)}"
                f"{_ages_text(c, now)}"
            )

    def _notify_signals(self, candidates: list[SpreadEstimate]) -> None:
        """Отправить отобранные сигналы администратору (очередь неблокирующая)."""
        if not candidates or not self._config.current.notifications.is_usable:
            return
        for candidate in candidates:
            self._notifier.notify(candidate)


def _leg_age_ms(quoted_at: datetime | None, now: datetime) -> float | None:
    return max(0.0, (now - quoted_at).total_seconds() * 1000) if quoted_at is not None else None


def _quote_age_ms(candidate: SpreadEstimate, now: datetime) -> float | None:
    """Возраст самой старой из двух котировок сигнала, мс."""
    ages = [a for a in (_leg_age_ms(candidate.long_leg.quoted_at, now), _leg_age_ms(candidate.short_leg.quoted_at, now)) if a is not None]
    return max(ages) if ages else None


def _ages_text(candidate: SpreadEstimate, now: datetime) -> str:
    long_age = _leg_age_ms(candidate.long_leg.quoted_at, now)
    short_age = _leg_age_ms(candidate.short_leg.quoted_at, now)
    if long_age is None and short_age is None:
        return ""
    fmt = lambda a: "н/д" if a is None else f"{a:.0f}"  # noqa: E731
    return f", возраст котировок {fmt(long_age)}/{fmt(short_age)} мс"


def _reraise_first(results: list[object]) -> None:
    """Как Task.WhenAll: дождались всех, затем пробрасываем первое исключение (отмену или сбой)."""
    for item in results:
        if isinstance(item, BaseException):
            raise item


def _funding_of(funding: FundingMap, exchange_id: str, symbol: str) -> Decimal | None:
    rates = funding.get(exchange_id)
    if rates is None:
        return None
    return rates.get(symbol)


def _net_funding_percent(candidate: SpreadEstimate) -> Decimal | None:
    """Чистый фандинг пары: рейт шорта − рейт лонга; None — хотя бы по одной ноге данных нет (fail-open)."""
    long_rate = candidate.long_leg.funding_percent
    short_rate = candidate.short_leg.funding_percent
    if long_rate is None or short_rate is None:
        return None
    return short_rate - long_rate
