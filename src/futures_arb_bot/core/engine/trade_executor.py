"""
Исполнитель арбитражных сделок: открывает встречные фьючерсные позиции (лонг на дешёвой
бирже + шорт на дорогой), ведёт их и закрывает по порогу «вниз» (прибыль), стоп-лоссу
или таймауту. В режиме DryRun все сделки симулируются по текущим котировкам.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from futures_arb_bot.core.abstractions import ConfigProvider, EventLog, ExchangeConnector, StatisticsCollector
from futures_arb_bot.core.cancellation import CancellationToken, OperationCanceledError
from futures_arb_bot.core.clock import Clock
from futures_arb_bot.core.domain.arbitrage import PositionPair, SpreadEstimate
from futures_arb_bot.core.domain.enums import CloseReason, NetworkMode, OrderSide, OrderType, PositionStatus, TimeInForce
from futures_arb_bot.core.domain.market_data import MarketInfo, OrderRequest, TickerSnapshot
from futures_arb_bot.core.domain.options import ArbitrageOptions
from futures_arb_bot.core.domain.order_execution import (
    MARKET_ORDER,
    ChasePlan,
    ExchangeCapabilityMap,
    OrderPolicy,
    OrderPolicyResolver,
)
from futures_arb_bot.core.engine import formatting
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.logging_bridge import get_logger
from futures_arb_bot.core.errors import message_of
from futures_arb_bot.core.numbers import HUNDRED, ZERO, dec_str, round_away

_NONE = CancellationToken.none()
_TEN_THOUSAND = Decimal(10_000)

RECONCILE_INTERVAL = timedelta(seconds=60)
"""Период сверки фактической экспозиции с биржами (fetch_positions — приватный запрос)."""


@dataclass(eq=False, slots=True)
class _LegState:
    """Состояние исполнения одной ноги: активная заявка плюс накопленный итог предыдущих заявок."""

    connector: ExchangeConnector
    request: OrderRequest
    """Текущая (последняя выставленная) заявка ноги — меняется при догонании."""
    policy: OrderPolicy
    target_amount: Decimal
    """Полный объём ноги, который нужно набрать."""
    start_price: Decimal
    """Цена первой заявки — от неё считается бюджет отклонения догонания."""
    quote_price: Decimal
    """Цена последней заявки ноги."""
    steps_total: int
    steps_left: int
    last_quote_at: datetime
    order_id: str | None = None
    filled_base: Decimal = ZERO
    """Суммарное исполнение всех снятых/заменённых заявок ноги."""
    current_filled: Decimal = ZERO
    """Исполнение текущей заявки."""
    cost_quote: Decimal = ZERO
    """Σ (объём × цена) по всем исполнениям ноги — для средней цены входа."""
    taker_percent: Decimal = ZERO
    """Ставка taker биржи ноги — для оценки комиссии, если биржа не сообщила фактическую."""
    fee_base: Decimal = ZERO
    """Комиссии всех снятых/заменённых заявок ноги, USDT."""
    current_cost: Decimal = ZERO
    """Σ (объём × цена) по текущей заявке — для оценки её комиссии."""
    current_fee: Decimal | None = None
    """Фактическая комиссия текущей заявки, сообщённая биржей (None — неизвестна)."""
    chase_finished: bool = False
    error: str | None = None
    done: bool = False
    dead: bool = False

    @property
    def filled(self) -> Decimal:
        return self.filled_base + self.current_filled

    @property
    def current_order_fee(self) -> Decimal:
        """Комиссия текущей заявки: фактическая от биржи, иначе оценка по taker."""
        if self.current_fee is not None:
            return self.current_fee
        return self.current_cost * self.taker_percent / HUNDRED

    @property
    def fee_quote(self) -> Decimal:
        """Комиссия ноги по всем её заявкам, USDT."""
        return self.fee_base + self.current_order_fee

    @property
    def average_price(self) -> Decimal | None:
        filled = self.filled
        return round_away(self.cost_quote / filled, 8) if filled > ZERO else None


@dataclass(eq=False, slots=True)
class _StrayOrder:
    """Заявка, которую не удалось снять при откате открытия (может исполниться сама)."""

    connector: ExchangeConnector
    order_id: str
    symbol: str
    side: OrderSide


@dataclass(slots=True)
class _CloseLegResult:
    closed: Decimal
    average_price: Decimal | None
    error: str | None = None
    fee_quote: Decimal | None = None
    """Комиссия закрывающей заявки; None — оценить по taker."""


class ArbTradeExecutor:
    def __init__(
        self,
        config: ConfigProvider,
        registry: ConnectorRegistry,
        stats: StatisticsCollector,
        log: EventLog,
        clock: Clock,
    ) -> None:
        self._config = config
        self._registry = registry
        self._stats = stats
        self._log = log
        self._clock = clock
        self._logger = get_logger("ArbTradeExecutor")
        self._gate = asyncio.Lock()
        self._positions: dict[str, PositionPair] = {}
        self._policy_notes: set[str] = set()
        """Уже показанные пояснения политики исполнения (exchangeId|заметка) — один раз, а не на каждой сделке."""
        self._external_exposure: dict[str, set[str]] = {}
        """Экспозиция бирж вне учёта сеанса: exchangeId → символы с ненулевой позицией."""
        self._stray_orders: list[_StrayOrder] = []
        self._reconcile_fail_warned: set[str] = set()
        self._last_reconcile_at: datetime | None = None
        self._last_tickers: dict[str, dict[str, TickerSnapshot]] = {}
        self._markets_cache: dict[str, dict[str, MarketInfo]] = {}

    @property
    def has_open_positions(self) -> bool:
        return len(self._positions) > 0

    async def _acquire(self, ct: CancellationToken) -> None:
        ct.throw_if_cancellation_requested()
        await self._gate.acquire()

    # ------------------------- публичный цикл -------------------------

    async def process_opportunities(self, candidates: Sequence[SpreadEstimate], ct: CancellationToken) -> None:
        if not candidates:
            return

        await self._acquire(ct)
        try:
            options = self._config.current.arbitrage

            # лимиты обязаны считать фактическую экспозицию бирж, а не только память сеанса
            await self._reconcile_external_exposure(ct)

            for candidate in candidates:
                if ct.is_cancellation_requested:
                    return  # остановка робота: новых сделок не начинаем
                external_arbitrages = self._count_external_arbitrages()
                if candidate.symbol in self._positions:
                    continue  # на символ — одна арбитражная позиция

                if len(self._positions) + external_arbitrages >= options.max_open_positions:
                    return  # лимит сеансовых и найденных на биржах арбитражей исчерпан

                per_exchange = self._count_per_exchange()
                if (
                    per_exchange.get(candidate.long_leg.exchange_id, 0) >= options.max_positions_per_exchange
                    or per_exchange.get(candidate.short_leg.exchange_id, 0) >= options.max_positions_per_exchange
                ):
                    continue

                await self._open(candidate, options, ct)
        finally:
            self._gate.release()

    async def manage_open_positions(self, tickers_by_exchange: Mapping[str, Mapping[str, TickerSnapshot]], ct: CancellationToken) -> None:
        self._update_ticker_cache(tickers_by_exchange)

        # заявки, не снятые при откате открытия, живы в стакане и могут исполниться — каждый тик пробуем отменить снова
        await self._sweep_stray_orders(ct)

        options = self._config.current.arbitrage
        mode = self._config.current.general.network_mode
        now = self._clock.now()
        to_close: list[tuple[PositionPair, CloseReason]] = []
        to_rebalance: list[PositionPair] = []

        for position in list(self._positions.values()):
            long_ticker = self._find_ticker(position.long_exchange_id, position.symbol)
            short_ticker = self._find_ticker(position.short_exchange_id, position.symbol)
            if long_ticker is None or short_ticker is None or not long_ticker.is_tradable or not short_ticker.is_tradable:
                continue

            current_gross = (short_ticker.bid - long_ticker.ask) / long_ticker.ask * HUNDRED
            # «текущий спред» меряется сторонами входа и потому оптимистичен на ширину обоих стаканов:
            # тейк-профит разрешаем только при положительном PnL немедленного выхода
            net_now = self._net_profit_percent(position, long_ticker, short_ticker)
            reason: CloseReason | None
            if current_gross <= options.min_spread_percent_down and net_now > ZERO:
                reason = CloseReason.TAKE_PROFIT
            elif current_gross >= options.stop_loss_spread_percent:
                reason = CloseReason.STOP_LOSS
            elif now - position.opened_at >= timedelta(minutes=options.max_position_age_minutes):
                reason = CloseReason.TIMEOUT
            else:
                reason = None

            if reason is not None:
                to_close.append((position, reason))
                continue

            # закрытие ног не завершено — сначала добьём закрытие, не ребалансируя наполовину голые ноги
            if position.long_closed != position.short_closed:
                continue

            if mode != NetworkMode.DRY_RUN and abs(position.imbalance) > self._rebalance_tolerance(position, options):
                to_rebalance.append(position)

        if not to_close and not to_rebalance:
            return

        await self._acquire(ct)
        try:
            closing_symbols = {p.symbol for p, _ in to_close}

            for position, reason in to_close:
                await self._close(position, reason, ct)

            for position in to_rebalance:
                if position.symbol in closing_symbols:
                    continue  # позиция закрывается — перекос закроется вместе с ней
                await self._rebalance(position, options, ct)
        finally:
            self._gate.release()

    async def close_all(self, reason: CloseReason, ct: CancellationToken) -> None:
        snapshot = list(self._positions.values())
        if not snapshot:
            return

        self._log.warning(f"Закрываю все позиции ({len(snapshot)}) перед выходом…")
        await self._acquire(ct)
        try:
            for position in snapshot:
                await self._close(position, reason, ct)
        finally:
            self._gate.release()

    # ------------------------- «потерянные» заявки -------------------------

    async def _sweep_stray_orders(self, ct: CancellationToken) -> None:
        """
        Доснимает заявки, не отменённые при откате открытия: живая лимитка может исполниться
        и создать экспозицию вне учёта. Исполнилась — символ уходит во внешнюю экспозицию.
        """
        if not self._stray_orders:
            return

        for stray in list(self._stray_orders):
            update = await stray.connector.fetch_order(stray.order_id, stray.symbol, ct, fresh=True)
            if update is None or update.is_dead:
                self._remove_stray(stray)
                self._log.warning(f"[{stray.connector.display_name}] «потерянная» заявка {stray.symbol} {stray.side} больше не живёт на бирже")
                continue

            if update.is_filled:
                self._remove_stray(stray)
                self._record_external_exposure(stray.connector.id, stray.symbol)
                self._log.error(
                    f"[{stray.connector.display_name}] «потерянная» заявка {stray.symbol} {stray.side} исполнилась — "
                    "позиция вне учёта, учитываю её в лимитах"
                )
                continue

            await stray.connector.cancel_order(stray.order_id, stray.symbol, ct)

    def _remove_stray(self, stray: _StrayOrder) -> None:
        if stray in self._stray_orders:
            self._stray_orders.remove(stray)

    def _record_external_exposure(self, exchange_id: str, symbol: str) -> None:
        self._external_exposure.setdefault(exchange_id, set()).add(symbol)

    # ------------------------- открытие -------------------------

    async def _open(self, estimate: SpreadEstimate, options: ArbitrageOptions, ct: CancellationToken) -> None:
        mode = self._config.current.general.network_mode
        long_connector = self._registry.find(estimate.long_leg.exchange_id)
        short_connector = self._registry.find(estimate.short_leg.exchange_id)
        if long_connector is None or short_connector is None:
            return

        amount = self._resolve_amount(long_connector, short_connector, estimate, options)
        if amount is None:
            return

        if mode == NetworkMode.DRY_RUN:
            self._open_simulated(estimate, amount, options)
            return

        # политики ног: конфигурация (глобальная + переопределение биржи) и возможности биржи
        long_policy = self._resolve_policy(long_connector, options, close=False)
        short_policy = self._resolve_policy(short_connector, options, close=False)
        long_price = (
            _offset_price(estimate.long_leg.price, OrderSide.BUY, long_policy.limit_offset_bps)
            if long_policy.requires_price
            else estimate.long_leg.price
        )
        short_price = (
            _offset_price(estimate.short_leg.price, OrderSide.SELL, short_policy.limit_offset_bps)
            if short_policy.requires_price
            else estimate.short_leg.price
        )

        self._log.info(
            f"Открываю арбитраж {estimate.symbol}: {_entry_description(long_policy, OrderSide.BUY, long_price, amount)} на {long_connector.display_name}, "
            f"{_entry_description(short_policy, OrderSide.SELL, short_price, amount)} на {short_connector.display_name} "
            f"(нетто {formatting.pct(estimate.net_percent)})"
        )

        await long_connector.set_leverage(options.leverage, estimate.symbol, ct)
        await short_connector.set_leverage(options.leverage, estimate.symbol, ct)

        # по умолчанию это marketable limit: покупка по ask дешёвой биржи, продажа по bid дорогой
        long_leg = self._build_leg(long_connector, long_policy, estimate.symbol, OrderSide.BUY, estimate.long_leg.price, amount)
        short_leg = self._build_leg(short_connector, short_policy, estimate.symbol, OrderSide.SELL, estimate.short_leg.price, amount)
        legs = [long_leg, short_leg]

        # С момента выставления первой заявки открытие доводится до конца без токена отмены:
        # Ctrl+C посреди открытия не бросает живые заявки и набранный объём, а прекращает
        # ожидание и откатывает ноги (или регистрирует позицию, если обе уже исполнены).

        # 1) выставляем ноги последовательно: отказ первой — вторая не выставляется вообще
        for index, leg in enumerate(legs):
            if index > 0 and ct.is_cancellation_requested:
                self._log.warning(f"{estimate.symbol}: остановка робота посреди открытия — вторая нога не выставляется, откатываю первую")
                residual_long, residual_short = await self._abort_legs(legs, _NONE)
                self._stats.record_open_failed(estimate.symbol, "остановка робота посреди открытия")
                if residual_long > ZERO or residual_short > ZERO:
                    self._register_partial_position(estimate, long_leg, short_leg, residual_long, residual_short)
                return

            if not await self._place(leg, _NONE):
                fail_reason = f"{_describe_leg(leg.policy.type, leg.request.side)} на {leg.connector.display_name} отклонён: {leg.error}"
                self._log.error(f"{estimate.symbol}: {fail_reason}; вторая нога не выставляется")
                residual_long, residual_short = await self._abort_legs(legs, _NONE)
                self._stats.record_open_failed(estimate.symbol, fail_reason)

                if residual_long > ZERO or residual_short > ZERO:
                    # откат набранного не прошёл — позиция остаётся в учёте для цикла ведения
                    self._register_partial_position(estimate, long_leg, short_leg, residual_long, residual_short)
                return

        # 2) ждём полного исполнения обеих ног (или смерти заявки/таймаута)
        await self._wait_for_legs(legs, options, ct)

        # 3) если что-то не исполнено — отменяем живые заявки и откатываем набранное
        if any(not leg.done for leg in legs):
            reasons = "; ".join(
                f"{leg.request.side} на {leg.connector.display_name}: {leg.error or ('заявка мертва' if leg.dead else 'не исполнена вовремя')}"
                for leg in legs
                if not leg.done
            )
            self._log.error(f"{estimate.symbol}: открытие не удалось ({reasons}); отменяю и откатываю ноги")
            residual_long, residual_short = await self._abort_legs(legs, _NONE)
            self._stats.record_open_failed(estimate.symbol, reasons)

            if residual_long > ZERO or residual_short > ZERO:
                # откат прошёл не полностью — регистрируем «кривую» позицию, её доведёт цикл ведения
                self._register_partial_position(estimate, long_leg, short_leg, residual_long, residual_short)
            return

        # 4) успех — позиция с раздельными объёмами ног
        self._register_position(estimate, long_leg, short_leg)

    async def _place(self, leg: _LegState, ct: CancellationToken) -> bool:
        """Выставить заявку ноги. False — биржа отказала (нога мертва)."""
        result = await leg.connector.place_order(leg.request, ct)
        if not result.success:
            leg.error = result.error or "ордер отклонён"
            leg.dead = True
            self._log.error(
                f"[{leg.connector.display_name}] {_describe_leg(leg.policy.type, leg.request.side)} {leg.request.symbol} не выставлен: {leg.error}"
            )
            return False

        leg.order_id = result.order_id
        _record_fill(leg, result.filled_amount, result.average_price, result.fee_quote)
        leg.done = leg.filled >= leg.target_amount
        return True

    async def _wait_for_legs(self, legs: list[_LegState], options: ArbitrageOptions, ct: CancellationToken) -> None:
        """
        Опрашивает ноги до полного исполнения, смерти заявки или таймаута. При локальном
        догонании (ChaseLimit + Simulated) неисполненная заявка переставляется ближе к рынку.

        Запросы к бирже здесь идут без токена отмены: по Ctrl+C ожидание прекращается
        (как по таймауту), а не обрывается на полпути с живой заявкой без учёта.
        """
        deadline = self._clock.now() + timedelta(milliseconds=options.order_execution_timeout_ms)
        while True:
            pending = [leg for leg in legs if not leg.done and not leg.dead]
            if not pending:
                break

            if ct.is_cancellation_requested:
                for leg in pending:
                    if leg.error is None:
                        leg.error = f"ожидание прервано остановкой робота, набрано {formatting.volume(leg.filled)}"
                break

            for leg in pending:
                if leg.order_id is None:
                    leg.dead = True
                    continue

                update = await leg.connector.fetch_order(leg.order_id, leg.request.symbol, _NONE)
                if update is None:
                    leg.dead = True
                    if leg.error is None:
                        leg.error = "заявка не найдена при опросе"
                    continue

                _record_fill(leg, update.filled_amount, update.average_price, update.fee_quote)
                if update.is_filled:
                    leg.done = True
                elif update.is_dead:
                    leg.dead = True
                elif leg.filled >= leg.target_amount:
                    # объём добран (возможен перелив) — снимаем живую заявку, чтобы не набрать лишнего
                    leg.done = True
                    if leg.order_id is not None:
                        await leg.connector.cancel_order(leg.order_id, leg.request.symbol, _NONE)
                else:
                    await self._chase_step(leg, _NONE)

            if self._clock.now() >= deadline:
                for leg in legs:
                    if not leg.done and not leg.dead and leg.error is None:
                        leg.error = f"не исполнена за {options.order_execution_timeout_ms} мс, набрано {formatting.volume(leg.filled)}"
                break

            try:
                await self._wait_for_order_events(legs, options.order_poll_interval_ms / 1000, ct)
            except OperationCanceledError:
                pass  # следующий виток зафиксирует остановку и выйдет из ожидания

    async def _wait_for_order_events(self, legs: list[_LegState], seconds: float, ct: CancellationToken) -> None:
        """
        Пауза между проверками ног: до seconds, но с потоком заявок — до первого события по ним
        (исполнение замечается сразу, а не на следующем опросе). Прерывается остановкой робота.
        """
        ct.throw_if_cancellation_requested()
        connectors = list({id(leg.connector): leg.connector for leg in legs if not leg.done and not leg.dead}.values())
        if not connectors:
            return
        waiters = [asyncio.ensure_future(c.wait_order_update(seconds)) for c in connectors]
        stop = asyncio.ensure_future(ct.wait()) if ct is not _NONE else None
        try:
            pending = {*waiters, stop} if stop is not None else set(waiters)
            await asyncio.wait(pending, timeout=seconds + 0.05, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (*waiters, *([stop] if stop is not None else [])):
                task.cancel()
        ct.throw_if_cancellation_requested()

    # ------------------------- догонание цены -------------------------

    async def _chase_step(self, leg: _LegState, ct: CancellationToken) -> None:
        """
        Один такт локального догонания: переставить заявку по следующему шагу, а когда бюджет
        исчерпан — добить остаток market (fallback_to_market) либо остановиться и отдать откату.
        """
        chase = leg.policy.chase
        if not leg.policy.chases_locally or leg.chase_finished or chase is None:
            return

        if leg.steps_left <= 0 or not _has_chase_budget(leg, chase):
            leg.chase_finished = True
            if chase.fallback_to_market:
                await self._fill_remainder_by_market(leg, ct)
            else:
                leg.error = f"догонание исчерпано ({chase.max_steps} шагов по {dec_str(chase.step_bps)} bps), набрано {formatting.volume(leg.filled)}"
                self._log.warning(f"[{leg.connector.display_name}] {leg.request.symbol} {leg.request.side}: {leg.error}")
            return

        if self._clock.now() - leg.last_quote_at < timedelta(milliseconds=chase.step_interval_ms):
            return  # ещё не время переставлять заявку

        await self._requote(leg, _chase_price(leg, chase), ct)

    async def _requote(self, leg: _LegState, price: Decimal, ct: CancellationToken) -> None:
        """Снять текущую заявку ноги и переставить неисполненный остаток по новой цене."""
        symbol = leg.request.symbol
        step_number = leg.steps_total - leg.steps_left + 1

        if leg.order_id is not None:
            # отмена идёт без токена: снять заявку важнее, чем выйти из процесса
            await leg.connector.cancel_order(leg.order_id, symbol, _NONE)
            final = await leg.connector.fetch_order(leg.order_id, symbol, _NONE, fresh=True)
            if final is not None:
                _record_fill(leg, final.filled_amount, final.average_price, final.fee_quote)

            if final is not None and not final.is_dead and not final.is_filled:
                # заявку снять не удалось — не дублируем её, попробуем на следующем тике
                self._log.warning(
                    f"[{leg.connector.display_name}] {symbol} {leg.request.side}: заявка {leg.order_id} не снята — догонание откладывается"
                )
                return

            _fold_current_fill(leg)

        remaining = leg.target_amount - leg.filled
        if remaining <= ZERO:
            leg.done = True
            return

        leg.steps_left -= 1
        leg.last_quote_at = self._clock.now()
        leg.quote_price = price
        leg.request = replace(leg.request, amount=remaining, price=price)

        self._logger.debug(
            f"Chase {symbol} {leg.request.side} on {leg.connector.id}: step {step_number}/{leg.steps_total} → {price} (remaining {remaining})"
        )
        self._log.info(
            f"[{leg.connector.display_name}] догоняю {symbol} {leg.request.side}: шаг {step_number}/{leg.steps_total} "
            f"по {formatting.price(price)}, остаток {formatting.volume(remaining)}"
        )
        await self._place(leg, ct)

    async def _fill_remainder_by_market(self, leg: _LegState, ct: CancellationToken) -> None:
        """Добить неисполненный остаток ноги market-заявкой (chase.fallback_to_market)."""
        symbol = leg.request.symbol

        if leg.order_id is not None and not leg.dead:
            await leg.connector.cancel_order(leg.order_id, symbol, _NONE)
            final = await leg.connector.fetch_order(leg.order_id, symbol, _NONE, fresh=True)
            if final is not None:
                _record_fill(leg, final.filled_amount, final.average_price, final.fee_quote)

            if final is not None and not final.is_dead and not final.is_filled:
                self._log.warning(
                    f"[{leg.connector.display_name}] {symbol} {leg.request.side}: заявка {leg.order_id} не снята — market-добивка откладывается"
                )
                return

            _fold_current_fill(leg)

        remaining = leg.target_amount - leg.filled
        if remaining <= ZERO:
            leg.done = True
            return

        self._log.warning(
            f"[{leg.connector.display_name}] {symbol} {leg.request.side}: бюджет догонания исчерпан, добиваю {formatting.volume(remaining)} market"
        )

        # дальше нога живёт как market: перестановки больше не нужны
        leg.policy = replace(leg.policy, type=OrderType.MARKET, chase=None)
        leg.request = replace(leg.request, amount=remaining, type=OrderType.MARKET, price=None, time_in_force=TimeInForce.GTC, exchange_params=None)
        await self._place(leg, ct)

    async def _abort_legs(self, legs: list[_LegState], ct: CancellationToken) -> tuple[Decimal, Decimal]:
        """
        Отменяет живые заявки обеих ног и откатывает набранный объём market reduceOnly (с ретраями).
        Возвращает остатки (long, short), которые не удалось откатить. Выполняется без токена отмены:
        откат важнее выхода из процесса.
        """
        residuals = [ZERO] * len(legs)

        for i, leg in enumerate(legs):
            # добиваемся отмены: после cancel перечитываем финальное состояние (гонка отмены и исполнения)
            if not leg.done and not leg.dead and leg.order_id is not None:
                attempt = 0
                while attempt < 3 and not leg.dead:
                    await leg.connector.cancel_order(leg.order_id, leg.request.symbol, _NONE)
                    update = await leg.connector.fetch_order(leg.order_id, leg.request.symbol, _NONE, fresh=True)
                    if update is None or update.is_dead:
                        _record_fill(
                            leg,
                            update.filled_amount if update is not None else leg.current_filled,
                            update.average_price if update is not None else None,
                            update.fee_quote if update is not None else None,
                        )
                        _fold_current_fill(leg)
                        leg.dead = True
                    elif update.is_filled:
                        _record_fill(leg, update.filled_amount, update.average_price, update.fee_quote)
                        _fold_current_fill(leg)
                        leg.done = True
                    attempt += 1

            # откат набранного — market reduceOnly с ретраями
            remaining = leg.filled
            close_side = OrderSide.SELL if leg.request.side == OrderSide.BUY else OrderSide.BUY
            attempt = 0
            while attempt < 3 and remaining > ZERO:
                rollback = await leg.connector.place_order(OrderRequest(leg.request.symbol, close_side, remaining, reduce_only=True), _NONE)
                if rollback.success:
                    remaining -= rollback.filled_amount if rollback.filled_amount > ZERO else remaining
                else:
                    self._log.error(
                        f"[{leg.connector.display_name}] откат {close_side} {leg.request.symbol} × {dec_str(remaining)} "
                        f"не прошёл (попытка {attempt + 1}): {rollback.error}"
                    )
                    await asyncio.sleep(0.25)
                attempt += 1

            residuals[i] = max(remaining, ZERO)
            if residuals[i] > ZERO:
                self._log.error(
                    f"[{leg.connector.display_name}] после откатов остаётся {leg.request.side} × {formatting.volume(residuals[i])} — передаю цикл ведения"
                )

            if not leg.dead and not leg.done and leg.order_id is not None:
                # заявку так и не сняли: она может исполниться сама — цикл ведения продолжит отмену каждый тик
                self._stray_orders.append(_StrayOrder(leg.connector, leg.order_id, leg.request.symbol, leg.request.side))
                self._log.error(
                    f"[{leg.connector.display_name}] {leg.request.symbol} {leg.request.side} × {formatting.volume(leg.request.amount)}: "
                    "заявку снять не удалось — она остаётся в стакане, её отменой займётся цикл ведения"
                )

        return residuals[0], residuals[1]

    # ------------------------- политика исполнения -------------------------

    def _resolve_policy(self, connector: ExchangeConnector, options: ArbitrageOptions, close: bool) -> OrderPolicy:
        """Глобальный execution → переопределение биржи → возможности биржи (пояснения — один раз)."""
        entry = next((e for e in self._config.current.exchanges.items if e.id.lower() == connector.id.lower()), None)
        caps = ExchangeCapabilityMap.for_id(connector.id)
        per_exchange = entry.execution if entry is not None else None

        if close:
            policy = OrderPolicyResolver.resolve_close(options.execution, per_exchange, caps)
        else:
            policy = OrderPolicyResolver.resolve_entry(options.execution, per_exchange, caps)

        for note in policy.notes:
            key = f"{connector.id}|{note}"
            if key not in self._policy_notes:
                self._policy_notes.add(key)
                self._log.warning(f"[{connector.display_name}] {note}")

        return policy

    def _build_leg(
        self,
        connector: ExchangeConnector,
        policy: OrderPolicy,
        symbol: str,
        side: OrderSide,
        reference_price: Decimal,
        amount: Decimal,
        reduce_only: bool = False,
    ) -> _LegState:
        """Цена со смещением от котировки, тип заявки (локальный chase — это обычный limit) и бюджет шагов."""
        price = _offset_price(reference_price, side, policy.limit_offset_bps) if policy.requires_price else None

        # на биржу уходит то, что она исполняет: локальное догонание — обычные limit-заявки,
        # нативный chase остаётся ChaseLimit (коннектор добавит параметры биржи)
        request_type = OrderType.LIMIT if policy.chases_locally else policy.type
        steps = policy.chase.max_steps if policy.chases_locally and policy.chase is not None else 0

        return _LegState(
            connector=connector,
            policy=policy,
            target_amount=amount,
            start_price=price if price is not None else reference_price,
            quote_price=price if price is not None else reference_price,
            request=OrderRequest(
                symbol,
                side,
                amount,
                reduce_only=reduce_only,
                type=request_type,
                price=price,
                time_in_force=policy.time_in_force,
                exchange_params=policy.exchange_params,
            ),
            steps_total=steps,
            steps_left=steps,
            last_quote_at=self._clock.now(),
            taker_percent=_taker_percent(connector, symbol),
        )

    # ------------------------- регистрация позиций -------------------------

    def _register_position(self, estimate: SpreadEstimate, long_leg: _LegState, short_leg: _LegState) -> None:
        """Регистрирует полностью открытую позицию по факту исполнения обеих ног."""
        entry_long = long_leg.average_price if long_leg.average_price is not None else estimate.long_leg.price
        entry_short = short_leg.average_price if short_leg.average_price is not None else estimate.short_leg.price
        # фактические комиссии ног (биржа сообщает их в заявке; где не сообщает — оценка по taker)
        fees = long_leg.fee_quote + short_leg.fee_quote

        position = PositionPair(
            symbol=estimate.symbol,
            long_exchange_id=long_leg.connector.id,
            short_exchange_id=short_leg.connector.id,
            long_size=long_leg.filled,
            short_size=short_leg.filled,
            entry_long=entry_long,
            entry_short=entry_short,
            fees_entry_usd=fees,
            opened_at=self._clock.now(),
            simulated=False,
        )
        self._positions[position.symbol] = position
        self._stats.record_opened(position)

        # диагностика исполнения: насколько фактическая средняя цена ушла от котировки сигнала
        slip_long = _slippage_percent(entry_long, estimate.long_leg.price, OrderSide.BUY)
        slip_short = _slippage_percent(entry_short, estimate.short_leg.price, OrderSide.SELL)
        self._log.success(
            f"Открыт арбитраж {position.symbol}: {formatting.volume(position.matched_size)} × лонг {position.long_exchange_id} "
            f"@ {formatting.price(position.entry_long)} / шорт {position.short_exchange_id} @ {formatting.price(position.entry_short)}, "
            f"проскальзывание от котировки сигнала: лонг {formatting.pct(slip_long)}, шорт {formatting.pct(slip_short)}, "
            f"комиссия входа {formatting.usd(fees)}"
        )

    def _register_partial_position(
        self, estimate: SpreadEstimate, long_leg: _LegState, short_leg: _LegState, residual_long: Decimal, residual_short: Decimal
    ) -> None:
        """Позиция после неудачного открытия с остатками откатов: ребаланс и закрытие доведут её до конца."""
        entry_long = long_leg.average_price if long_leg.average_price is not None else estimate.long_leg.price
        entry_short = short_leg.average_price if short_leg.average_price is not None else estimate.short_leg.price

        position = PositionPair(
            symbol=estimate.symbol,
            long_exchange_id=long_leg.connector.id,
            short_exchange_id=short_leg.connector.id,
            long_size=residual_long,
            short_size=residual_short,
            entry_long=entry_long,
            entry_short=entry_short,
            fees_entry_usd=long_leg.fee_quote + short_leg.fee_quote,
            opened_at=self._clock.now(),
            simulated=False,
        )
        self._positions[position.symbol] = position
        self._stats.record_opened(position)
        self._log.warning(
            f"{position.symbol}: после отката остались ножки — лонг {formatting.volume(position.long_size)} / "
            f"шорт {formatting.volume(position.short_size)}, позиция передана циклу ведения"
        )

    def _open_simulated(self, estimate: SpreadEstimate, amount: Decimal, options: ArbitrageOptions) -> None:
        long_connector = self._registry.find(estimate.long_leg.exchange_id)
        short_connector = self._registry.find(estimate.short_leg.exchange_id)
        assert long_connector is not None and short_connector is not None
        fees = _estimate_fees(long_connector, short_connector, estimate.symbol, estimate.long_leg.price, estimate.short_leg.price, amount)

        position = PositionPair(
            symbol=estimate.symbol,
            long_exchange_id=estimate.long_leg.exchange_id,
            short_exchange_id=estimate.short_leg.exchange_id,
            long_size=amount,
            short_size=amount,
            entry_long=estimate.long_leg.price,
            entry_short=estimate.short_leg.price,
            fees_entry_usd=fees,
            opened_at=self._clock.now(),
            simulated=True,
        )
        self._positions[position.symbol] = position
        self._stats.record_opened(position)
        self._log.success(
            f"[СИМУЛЯЦИЯ] Открыт арбитраж {position.symbol}: {formatting.volume(amount)} × лонг {position.long_exchange_id} "
            f"@ {formatting.price(position.entry_long)} / шорт {position.short_exchange_id} @ {formatting.price(position.entry_short)} "
            f"(нетто {formatting.pct(estimate.net_percent)})"
        )

    # ------------------------- ребалансировка -------------------------

    def _rebalance_tolerance(self, position: PositionPair, options: ArbitrageOptions) -> Decimal:
        """Допустимый перекос: максимум из процента от большей ноги и минимального лота урезаемой биржи."""
        cut_long = position.imbalance > ZERO
        exchange_id = position.long_exchange_id if cut_long else position.short_exchange_id
        connector = self._registry.find(exchange_id)
        market = self._find_market(connector, position.symbol) if connector is not None else None
        min_lot = market.min_amount if market is not None and market.min_amount is not None else ZERO
        percent = max(position.long_size, position.short_size) * options.rebalance_tolerance_percent / HUNDRED
        return max(percent, min_lot)

    async def _rebalance(self, position: PositionPair, options: ArbitrageOptions, ct: CancellationToken) -> None:
        """
        Урезает бóльшую ногу reduceOnly-заявкой по текущему bid/ask до выравнивания ног.
        Политика — входная (позиция не закрывается, а перебалансировывается). Частичное
        исполнение фиксируется; остаток добирается на следующем тике.
        """
        cut_long = position.imbalance > ZERO
        exchange_id = position.long_exchange_id if cut_long else position.short_exchange_id
        connector = self._registry.find(exchange_id)
        ticker = self._find_ticker(exchange_id, position.symbol)
        if connector is None or ticker is None or not ticker.is_tradable:
            return

        delta = abs(position.imbalance)
        price = ticker.bid if cut_long else ticker.ask
        if price <= ZERO:
            return

        side = OrderSide.SELL if cut_long else OrderSide.BUY
        policy = self._resolve_policy(connector, options, close=False)
        leg = self._build_leg(connector, policy, position.symbol, side, price, delta, reduce_only=True)
        quote = leg.request.price if leg.request.price is not None else price
        which = "лонг" if cut_long else "шорт"

        self._log.info(
            f"{position.symbol}: ребаланс — урезаю {which} на {formatting.volume(delta)} @ {formatting.price(quote)} "
            f"(ноги {formatting.volume(position.long_size)}/{formatting.volume(position.short_size)})"
        )
        if not await self._place(leg, ct):
            return  # отказ — повторим на следующем тике
        # заявка выставлена: дальше без токена отмены, чтобы её исполнение не потерялось из учёта

        await self._wait_for_legs([leg], options, ct)

        # по таймауту снимаем заявку и перечитываем финальное исполнение (гонка отмены и исполнения)
        if not leg.done and not leg.dead and leg.order_id is not None:
            await connector.cancel_order(leg.order_id, position.symbol, _NONE)
            update = await connector.fetch_order(leg.order_id, position.symbol, _NONE, fresh=True)
            if update is not None:
                _record_fill(leg, update.filled_amount, update.average_price, update.fee_quote)

        cut = min(leg.filled, delta)
        if cut <= ZERO:
            suffix = "" if leg.error is None else f" ({leg.error})"
            self._log.warning(f"{position.symbol}: ребаланс не исполнился{suffix} — повторю на следующем тике")
            return

        if cut_long:
            position.long_size = max(ZERO, position.long_size - cut)
        else:
            position.short_size = max(ZERO, position.short_size - cut)

        _accrue_closed_leg(
            position,
            connector,
            is_long=cut_long,
            quantity=cut,
            exit_price=_first_not_none(leg.average_price, price),
            fee=leg.fee_quote * cut / leg.filled,
        )
        self._log.success(
            f"{position.symbol}: ребаланс — урезано {which} на {formatting.volume(cut)}, "
            f"ноги {formatting.volume(position.long_size)}/{formatting.volume(position.short_size)}"
        )

        if position.long_size <= ZERO and position.short_size <= ZERO:
            # обрезали остатки откатов до нуля — позиция фактически закрыта
            await self._close(position, CloseReason.ROLLBACK, ct)

    def _taker_percent_of(self, exchange_id: str, symbol: str) -> Decimal:
        connector = self._registry.find(exchange_id)
        return _taker_percent(connector, symbol) if connector is not None else ZERO

    def _net_profit_percent(self, position: PositionPair, long_ticker: TickerSnapshot, short_ticker: TickerSnapshot) -> Decimal:
        """
        Расчётный PnL пары (в % оборота) при закрытии прямо сейчас: валовый спред входа минус
        стоимость немедленного выхода (лонг — по его bid, шорт — по его ask) и taker-комиссии полного круга.
        """
        if position.entry_long <= ZERO or long_ticker.bid <= ZERO:
            return ZERO

        entry_gross = (position.entry_short - position.entry_long) / position.entry_long * HUNDRED
        unwind_cost = (short_ticker.ask - long_ticker.bid) / long_ticker.bid * HUNDRED
        round_trip_fees = 2 * (
            self._taker_percent_of(position.long_exchange_id, position.symbol) + self._taker_percent_of(position.short_exchange_id, position.symbol)
        )
        return entry_gross - unwind_cost - round_trip_fees

    # ------------------------- закрытие -------------------------

    async def _close_leg(
        self,
        position: PositionPair,
        connector: ExchangeConnector,
        is_long: bool,
        size: Decimal,
        reference: Decimal | None,
        options: ArbitrageOptions,
        ct: CancellationToken,
    ) -> _CloseLegResult:
        """
        Закрыть ногу по политике execution.close: market — сразу, limit — с ожиданием и снятием
        остатка по таймауту. Объём 0 в результате — нога не закрыта.
        """
        side = OrderSide.SELL if is_long else OrderSide.BUY
        policy = self._resolve_policy(connector, options, close=True)

        if policy.requires_price and not (reference is not None and reference > ZERO):
            # лимитную заявку выставить некуда (нет котировки) — страховка: закрываем market
            self._log.warning(f"[{connector.display_name}] {position.symbol}: нет котировки для лимитного закрытия — закрываю market")
            policy = MARKET_ORDER

        leg = self._build_leg(connector, policy, position.symbol, side, reference if reference is not None else ZERO, size, reduce_only=True)
        # закрытие ноги не обрывается остановкой: начатое закрытие должно попасть в учёт
        if not await self._place(leg, _NONE):
            return _CloseLegResult(ZERO, None, leg.error or "заявка отклонена")

        # market-заявку не опрашиваем: она обязана исполниться сразу
        if not leg.done and policy.type != OrderType.MARKET:
            await self._wait_for_legs([leg], options, ct)

            if not leg.done and not leg.dead and leg.order_id is not None:
                await connector.cancel_order(leg.order_id, position.symbol, _NONE)
                update = await connector.fetch_order(leg.order_id, position.symbol, _NONE, fresh=True)
                if update is not None:
                    _record_fill(leg, update.filled_amount, update.average_price, update.fee_quote)

        closed = min(leg.filled, size)
        if closed <= ZERO and policy.type == OrderType.MARKET:
            # исполнение market подтверждается статусом заявки, а при нуле — отсутствием позиции на бирже
            update = await connector.fetch_order(leg.order_id, position.symbol, _NONE, fresh=True) if leg.order_id is not None else None
            if update is not None:
                _record_fill(leg, update.filled_amount, update.average_price, update.fee_quote)
                closed = min(leg.filled, size)

            if closed <= ZERO and update is not None and update.is_filled:
                closed = size  # статус «исполнен», но объёма биржа не вернула — доверяем статусу

            if closed <= ZERO:
                exposure_side = OrderSide.BUY if side == OrderSide.SELL else OrderSide.SELL
                if await _has_exchange_position(connector, exposure_side, position.symbol, _NONE):
                    error = "рыночная заявка не подтверждена биржей" if update is None else f"рыночная заявка не исполнена (статус: {update.status})"
                    return _CloseLegResult(ZERO, leg.average_price, error)

                closed = size  # позиции на бирже нет — нога действительно закрыта

        # комиссию берём по заявке, если по ней есть исполнение; «закрыто по факту отсутствия позиции» — оценка
        return _CloseLegResult(closed, leg.average_price, leg.error, leg.fee_quote if leg.filled > ZERO else None)

    async def _close(self, position: PositionPair, reason: CloseReason, ct: CancellationToken) -> None:
        """
        Закрывает каждую ногу по её фактическому объёму. Если нога не закрылась — позиция остаётся
        под символом и закрытие повторится на следующем тике; «голой» экспозиции бот не теряет.
        """
        options = self._config.current.arbitrage
        long_connector = self._registry.find(position.long_exchange_id)
        short_connector = self._registry.find(position.short_exchange_id)
        if long_connector is None or short_connector is None:
            return

        long_ticker = self._find_ticker(position.long_exchange_id, position.symbol)
        short_ticker = self._find_ticker(position.short_exchange_id, position.symbol)

        if self._config.current.general.network_mode == NetworkMode.DRY_RUN:
            # симуляция: закрытие по текущим bid/ask (или по входу, если котировок нет)
            sim_exit_long = long_ticker.bid if long_ticker is not None else position.entry_long
            sim_exit_short = short_ticker.ask if short_ticker is not None else position.entry_short
            position.exit_long = sim_exit_long
            position.exit_short = sim_exit_short
            _accrue_closed_leg(position, long_connector, is_long=True, quantity=position.long_size, exit_price=sim_exit_long)
            _accrue_closed_leg(position, short_connector, is_long=False, quantity=position.short_size, exit_price=sim_exit_short)
            position.long_closed = True
            position.short_closed = True
        else:
            if not position.long_closed:
                if position.long_size <= ZERO:
                    position.long_closed = True
                else:
                    result = await self._close_leg(
                        position, long_connector, True, position.long_size, long_ticker.bid if long_ticker is not None else None, options, ct
                    )
                    if result.closed <= ZERO:
                        self._log.error(
                            f"[{long_connector.display_name}] закрытие лонга {position.symbol} не удалось: "
                            f"{result.error or 'заявка не исполнена'} — повторю на следующем тике"
                        )
                        return  # позиция остаётся в учёте, закроем позже

                    exit_long = _first_not_none(result.average_price, long_ticker.bid if long_ticker is not None else None, position.entry_long)
                    _accrue_closed_leg(position, long_connector, is_long=True, quantity=result.closed, exit_price=exit_long, fee=result.fee_quote)
                    position.long_size = max(ZERO, position.long_size - result.closed)
                    position.exit_long = exit_long
                    position.long_closed = position.long_size <= ZERO
                    if not position.long_closed:
                        self._log.warning(
                            f"{position.symbol}: лонг закрыт частично, остаток {formatting.volume(position.long_size)} — добью на следующем тике"
                        )

            if not position.short_closed:
                if position.short_size <= ZERO:
                    position.short_closed = True
                else:
                    result = await self._close_leg(
                        position, short_connector, False, position.short_size, short_ticker.ask if short_ticker is not None else None, options, ct
                    )
                    if result.closed <= ZERO:
                        # лонг уже закрыт — шорт остаётся голым, но позиция не теряется: повтор на следующем тике
                        self._log.error(
                            f"[{short_connector.display_name}] закрытие шорта {position.symbol} не удалось: "
                            f"{result.error or 'заявка не исполнена'} — позиция остаётся в учёте, повторю на следующем тике"
                        )
                        return

                    exit_short = _first_not_none(result.average_price, short_ticker.ask if short_ticker is not None else None, position.entry_short)
                    _accrue_closed_leg(position, short_connector, is_long=False, quantity=result.closed, exit_price=exit_short, fee=result.fee_quote)
                    position.short_size = max(ZERO, position.short_size - result.closed)
                    position.exit_short = exit_short
                    position.short_closed = position.short_size <= ZERO
                    if not position.short_closed:
                        self._log.warning(
                            f"{position.symbol}: шорт закрыт частично, остаток {formatting.volume(position.short_size)} — добью на следующем тике"
                        )

            if not position.long_closed or not position.short_closed:
                return  # одна нога ещё жива — финализируем, когда закроются обе

        position.reason = reason
        position.closed_at = self._clock.now()
        position.status = PositionStatus.CLOSED

        # накопленный по ногам PnL минус комиссии входа и выхода
        accrued = position.realized_pnl_usd if position.realized_pnl_usd is not None else ZERO
        position.realized_pnl_usd = accrued - position.fees_entry_usd - position.fees_exit_usd

        self._positions.pop(position.symbol, None)
        self._stats.record_closed(position)
        self._log.success(f"Закрыт арбитраж {position.symbol} ({_describe_reason(reason)}): PnL {formatting.usd(position.realized_pnl_usd)}")

    # ------------------------- вспомогательное -------------------------

    def _resolve_amount(
        self, long_connector: ExchangeConnector, short_connector: ExchangeConnector, estimate: SpreadEstimate, options: ArbitrageOptions
    ) -> Decimal | None:
        amount = options.order_size_usd / estimate.long_leg.price

        for connector, price in ((long_connector, estimate.long_leg.price), (short_connector, estimate.short_leg.price)):
            market = self._find_market(connector, estimate.symbol)
            if market is not None and market.min_amount is not None and market.min_amount > ZERO and amount < market.min_amount:
                required_usd = market.min_amount * price
                if required_usd > options.order_size_usd * 5:
                    self._log.warning(
                        f"{estimate.symbol}: минимальный объём на {connector.display_name} ({dec_str(market.min_amount)}) "
                        f"требует ~{formatting.usd(required_usd)} — пропуск"
                    )
                    return None
                amount = market.min_amount

        return amount if amount > ZERO else None

    def _find_market(self, connector: ExchangeConnector, symbol: str) -> MarketInfo | None:
        by_symbol = self._markets_cache.get(connector.id)
        if by_symbol is None:
            by_symbol = {m.symbol: m for m in connector.perpetual_markets}
            self._markets_cache[connector.id] = by_symbol
        return by_symbol.get(symbol)

    def _count_per_exchange(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        tracked = set(self._positions)
        for position in self._positions.values():
            counts[position.long_exchange_id] = counts.get(position.long_exchange_id, 0) + 1
            counts[position.short_exchange_id] = counts.get(position.short_exchange_id, 0) + 1

        # экспозиция вне сеанса тоже занимает слоты биржи; символы своих позиций не считаем дважды
        for exchange_id, symbols in self._external_exposure.items():
            extra = sum(1 for s in symbols if s not in tracked)
            if extra > 0:
                counts[exchange_id] = counts.get(exchange_id, 0) + extra
        return counts

    def _count_external_arbitrages(self) -> int:
        """Число «внешних» арбитражей: символ с ненулевой экспозицией сразу на 2+ биржах."""
        tracked = set(self._positions)
        legs_per_symbol: dict[str, int] = {}
        for symbols in self._external_exposure.values():
            for symbol in symbols:
                if symbol in tracked:
                    continue  # наша же позиция: её слот уже учтён как сеансовая
                legs_per_symbol[symbol] = legs_per_symbol.get(symbol, 0) + 1
        return sum(1 for legs in legs_per_symbol.values() if legs >= 2)

    async def _reconcile_external_exposure(self, ct: CancellationToken) -> None:
        """
        Сверка с биржами: лимиты должны учитывать фактическую экспозицию, а не только позиции сеанса —
        иначе после рестарта бот открывал «ещё одну» поверх живой. Символы своих позиций исключаются.
        """
        if self._config.current.general.network_mode == NetworkMode.DRY_RUN:
            return  # симуляция не выставляет заявок на биржу — сверяться не с чем

        now = self._clock.now()
        if self._last_reconcile_at is not None and now - self._last_reconcile_at < RECONCILE_INTERVAL:
            return  # не чаще раза в минуту: fetch_positions — приватный запрос

        self._last_reconcile_at = now

        # снимок прошлой сверки: при сбое запроса сохраняем известную экспозицию
        previous = {k: set(v) for k, v in self._external_exposure.items()}

        fresh: dict[str, set[str]] = {}
        for connector in self._registry.connectors:
            try:
                positions = await connector.fetch_positions(ct)
                symbols = {p.symbol for p in positions if p.amount > ZERO}
                if symbols:
                    fresh[connector.id] = symbols
            except OperationCanceledError:
                raise
            except Exception as ex:
                if connector.id not in self._reconcile_fail_warned:
                    self._reconcile_fail_warned.add(connector.id)
                    self._log.warning(
                        f"[{connector.display_name}] сверка позиций недоступна: {message_of(ex)} — лимиты считаются только по позициям сеанса"
                    )
                self._logger.debug(f"FetchPositions reconcile failed on {connector.id}", exc_info=ex)
                stale = previous.get(connector.id)
                if stale is not None:
                    fresh[connector.id] = stale  # консервативно: помним прошлый результат

        for position in self._positions.values():
            longs = fresh.get(position.long_exchange_id)
            if longs is not None:
                longs.discard(position.symbol)
            shorts = fresh.get(position.short_exchange_id)
            if shorts is not None:
                shorts.discard(position.symbol)

        report = not self._external_exposure and any(fresh.values())
        self._external_exposure = {k: v for k, v in fresh.items() if v}

        if report:
            description = "; ".join(f"{k}: {', '.join(sorted(v))}" for k, v in sorted(fresh.items()) if v)
            self._log.warning(
                f"На биржах найдены позиции вне учёта этого сеанса ({description}) — они занимают слоты лимитов MaxOpenPositions/MaxPositionsPerExchange"
            )

    def _update_ticker_cache(self, tickers_by_exchange: Mapping[str, Mapping[str, TickerSnapshot]]) -> None:
        for exchange_id, tickers in tickers_by_exchange.items():
            self._last_tickers.setdefault(exchange_id, {}).update(tickers)

    def _find_ticker(self, exchange_id: str, symbol: str) -> TickerSnapshot | None:
        bucket = self._last_tickers.get(exchange_id)
        return bucket.get(symbol) if bucket is not None else None


# ------------------------- функции без состояния -------------------------


def _record_fill(leg: _LegState, current_order_filled: Decimal, price: Decimal | None, fee: Decimal | None = None) -> None:
    """
    Учитывает исполнение текущей заявки: прирост объёма — в итог, цена — в средневзвешенную цену,
    комиссия (накопленная по заявке, если биржа её сообщает) — в комиссию ноги.
    """
    if fee is not None:
        leg.current_fee = fee
    delta = current_order_filled - leg.current_filled
    if delta <= ZERO:
        leg.current_filled = max(leg.current_filled, current_order_filled)
        return
    cost = delta * (price if price is not None else leg.quote_price)
    leg.current_filled += delta
    leg.cost_quote += cost
    leg.current_cost += cost


def _fold_current_fill(leg: _LegState) -> None:
    """Переносит исполнение снятой/заменённой заявки в накопленный итог ноги."""
    leg.filled_base += leg.current_filled
    leg.fee_base += leg.current_order_fee
    leg.current_filled = ZERO
    leg.current_cost = ZERO
    leg.current_fee = None


def _next_step_bps(leg: _LegState, chase: ChasePlan) -> Decimal:
    return chase.step_bps * (leg.steps_total - leg.steps_left + 1)


def _has_chase_budget(leg: _LegState, chase: ChasePlan) -> bool:
    return leg.steps_left > 0 and _next_step_bps(leg, chase) <= chase.max_deviation_bps


def _chase_price(leg: _LegState, chase: ChasePlan) -> Decimal:
    """Смещение от стартовой цены на (шагов + 1) × step_bps в сторону агрессии, не дальше max_deviation_bps."""
    bps = min(_next_step_bps(leg, chase), chase.max_deviation_bps)
    return _offset_price(leg.start_price, leg.request.side, bps)


def _offset_price(reference: Decimal, side: OrderSide, offset_bps: Decimal) -> Decimal:
    """Цена со смещением в bps: BUY — выше (агрессивнее), SELL — ниже."""
    if offset_bps == ZERO:
        return reference
    sign = 1 if side == OrderSide.BUY else -1
    return round_away(reference * (1 + sign * offset_bps / _TEN_THOUSAND), 8)


def _describe_leg(order_type: OrderType, side: OrderSide) -> str:
    if order_type == OrderType.MARKET:
        return f"MARKET {side}"
    if order_type == OrderType.CHASE_LIMIT:
        return f"CHASE-LIMIT {side}"
    return f"LIMIT {side}"


def _entry_description(policy: OrderPolicy, side: OrderSide, price: Decimal, amount: Decimal) -> str:
    if policy.requires_price:
        return f"{_describe_leg(policy.type, side)} {formatting.volume(amount)} @ {formatting.price(price)}"
    return f"{_describe_leg(policy.type, side)} {formatting.volume(amount)}"


def _slippage_percent(filled: Decimal, quoted: Decimal, side: OrderSide) -> Decimal:
    """Проскальзывание ноги: покупка дороже и продажа дешевле котировки — потеря (положительный знак)."""
    if quoted <= ZERO:
        return ZERO
    deviation = filled - quoted if side == OrderSide.BUY else quoted - filled
    return deviation / quoted * HUNDRED


def _taker_percent(connector: ExchangeConnector, symbol: str) -> Decimal:
    found, fees = connector.try_get_fees(symbol)
    return fees.taker_percent if found else connector.default_fees.taker_percent


def _estimate_fees(
    long_connector: ExchangeConnector, short_connector: ExchangeConnector, symbol: str, long_price: Decimal, short_price: Decimal, size: Decimal
) -> Decimal:
    return size * (long_price * _taker_percent(long_connector, symbol) + short_price * _taker_percent(short_connector, symbol)) / HUNDRED


def _accrue_closed_leg(
    position: PositionPair, connector: ExchangeConnector, *, is_long: bool, quantity: Decimal, exit_price: Decimal, fee: Decimal | None = None
) -> None:
    """
    Начисляет реализованный PnL и комиссию закрытия на урезанный/закрытый фрагмент ноги.
    fee — фактическая комиссия заявки; None — оценка по taker (симуляция, биржа не сообщила).
    """
    if quantity <= ZERO:
        return

    entry = position.entry_long if is_long else position.entry_short
    pnl = quantity * (exit_price - entry if is_long else entry - exit_price)
    position.realized_pnl_usd = (position.realized_pnl_usd if position.realized_pnl_usd is not None else ZERO) + pnl
    position.fees_exit_usd += fee if fee is not None else quantity * exit_price * _taker_percent(connector, position.symbol) / HUNDRED

    if is_long:
        position.closed_long_volume += quantity
    else:
        position.closed_short_volume += quantity


async def _has_exchange_position(connector: ExchangeConnector, exposure_side: OrderSide, symbol: str, ct: CancellationToken) -> bool:
    """Есть ли у биржи фактическая позиция ноги. Сбой сверки трактуем как «позиция есть»."""
    try:
        positions = await connector.fetch_positions(ct)
        return any(p.symbol == symbol and p.side == exposure_side and p.amount > ZERO for p in positions)
    except OperationCanceledError:
        raise
    except Exception:
        return True


def _first_not_none(*values: Decimal | None) -> Decimal:
    for value in values:
        if value is not None:
            return value
    return ZERO


def _describe_reason(reason: CloseReason) -> str:
    return {
        CloseReason.TAKE_PROFIT: "тейк-профит",
        CloseReason.STOP_LOSS: "стоп-лосс",
        CloseReason.TIMEOUT: "таймаут позиции",
        CloseReason.SESSION_END: "завершение сеанса",
        CloseReason.ROLLBACK: "откат сделки",
    }.get(reason, str(reason))
