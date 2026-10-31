"""
Исполнитель арбитражных сделок: открывает встречные фьючерсные позиции (лонг на дешёвой
бирже + шорт на дорогой), ведёт их и закрывает по порогу «вниз» (прибыль), стоп-лоссу
или таймауту. В режиме DryRun все сделки симулируются по текущим котировкам.
"""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from futures_arb_bot.core.abstractions import ConfigProvider, EventLog, ExchangeConnector, StatisticsCollector
from futures_arb_bot.core.cancellation import CancellationToken, OperationCanceledError
from futures_arb_bot.core.clock import Clock
from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, PositionPair, SpreadEstimate, funding_cost_percent
from futures_arb_bot.core.domain.enums import CloseReason, NetworkMode, OrderSide, OrderType, PositionStatus, TimeInForce
from futures_arb_bot.core.domain.market_data import ExchangeFees, MarketInfo, OrderBookSnapshot, OrderRequest, TickerSnapshot
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
from futures_arb_bot.core.engine.maker_hedge import MakerQuotePlan, deviation_bps, plan_quote, replan, simulated_fill
from futures_arb_bot.core.errors import message_of
from futures_arb_bot.core.numbers import HUNDRED, ZERO, dec_str, round_away

_NONE = CancellationToken.none()
_TEN_THOUSAND = Decimal(10_000)

RECONCILE_INTERVAL = timedelta(seconds=60)
"""Период сверки фактической экспозиции с биржами (fetch_positions — приватный запрос)."""

BOOK_REJECT_COOLDOWN = timedelta(seconds=10)
"""Спред не подтвердился стаканом — столько не перепроверяем ту же связку (не спамим запросами и журналом)."""

POSITION_SETTLE_DELAY_SECONDS = 0.7
"""Пауза перед повторной сверкой позиции: биржа (Bybit) показывает позицию с задержкой после исполнения."""

EXIT_BOOK_RECHECK = timedelta(seconds=2)
"""Тейк-профит не подтвердился стаканом — столько не перепроверяем ту же позицию (не спамим REST и журналом)."""

STALE_EXIT_NOTE_EVERY = timedelta(seconds=30)
"""Как часто напоминать в журнале, что тейк-профит отложен из-за старых котировок."""

CLOSE_RETRY_BASE = timedelta(seconds=1)
"""Пауза после первой неудачной попытки закрытия; дальше удваивается."""

CLOSE_RETRY_MAX = timedelta(seconds=30)
"""Предельная пауза между повторами закрытия."""

CLOSE_ALERT_AFTER = 10
"""Столько неудачных попыток закрытия подряд — громкое предупреждение (и далее каждые 100)."""

MAKER_REJECT_COOLDOWN = timedelta(seconds=5)
"""Заявку-мейкер не удалось выставить (post-only отклонён, нет маржи) — столько не пробуем этот символ снова."""

MAKER_HEDGE_FAIL_COOLDOWN = timedelta(seconds=60)
"""Хедж не прошёл и мейкер-нога откачена — столько не ставим заявку по этому символу."""

MAKER_WITHDRAW_COOLDOWN = timedelta(seconds=1)
"""После снятия заявки-мейкера столько не ставим новую по тому же символу (не дёргаем стакан)."""

VOLATILITY_NOTE_EVERY = timedelta(seconds=60)
"""Как часто писать в журнал, что заявка-мейкер по символу не ставится из-за волатильности."""

_FOUR_PLACES = Decimal("0.0001")

_StreakKey = tuple[str, str, str]
"""(символ, биржа лонга, биржа шорта)."""


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


@dataclass(slots=True)
class _CloseAttempts:
    """Начатое закрытие позиции: причина и счётчик неудач для паузы между повторами."""

    reason: CloseReason
    failures: int = 0
    retry_at: datetime | None = None


@dataclass(eq=False, slots=True)
class _MakerQuote:
    """Заявка-мейкер входа «maker + хедж», ждущая исполнения."""

    symbol: str
    plan: MakerQuotePlan
    maker: ExchangeConnector
    hedge: ExchangeConnector
    amount: Decimal
    leg: _LegState | None
    """Нога-мейкер с живой заявкой; None — симуляция (DryRun)."""
    placed_at: datetime
    requoted_at: datetime
    signal_at: datetime
    """Когда сигнал (брутто ≥ trigger по этой связке) последний раз был на месте."""
    requotes: int = 0
    funding_cost: Decimal = ZERO
    """Плата за фандинг (funding_periods_in_net выплат), заложенная в цену заявки сверх целевого нетто, %."""

    @property
    def simulated(self) -> bool:
        return self.leg is None


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
        self._streaks: dict[_StreakKey, int] = {}
        """Сколько сканов подряд связка держится выше порога входа (confirm_scans)."""
        self._book_rejected_until: dict[_StreakKey, datetime] = {}
        self._close_attempts: dict[str, _CloseAttempts] = {}
        """Позиции, закрытие которых начато, но не завершено (символ → причина и повторы)."""
        self._position_settle_delay = POSITION_SETTLE_DELAY_SECONDS
        self._exit_streaks: dict[str, tuple[CloseReason, int]] = {}
        """Сколько сканов подряд держится условие выхода позиции (символ → причина, счётчик; exit_confirm_scans)."""
        self._exit_book_rejected_until: dict[str, datetime] = {}
        """Тейк-профит не подтвердился стаканом — до этого момента стакан по символу не перепроверяется."""
        self._stale_exit_noted_at: dict[str, datetime] = {}
        """Когда в журнал последний раз писали, что тейк-профит отложен из-за старых котировок."""
        self._reentry_blocked_until: dict[str, datetime] = {}
        """Символ → момент, до которого новая позиция по нему не открывается (пауза после закрытия)."""
        self._maker_quotes: dict[str, _MakerQuote] = {}
        """Висящие заявки-мейкеры входа «maker + хедж» (символ → заявка)."""
        self._maker_blocked_until: dict[str, datetime] = {}
        """Символ → момент, до которого новая заявка-мейкер по нему не ставится."""
        self._mid_history: dict[tuple[str, str], deque[tuple[datetime, Decimal]]] = {}
        """(биржа, символ) → недавние средние цены для проверки волатильности перед заявкой-мейкером."""
        self._volatility_noted_at: dict[str, datetime] = {}
        self._leverage_applied: set[tuple[str, str, int]] = set()
        """(биржа, символ, плечо), уже выставленные для заявок-мейкеров: не повторяем запрос на каждой заявке."""

    @property
    def has_open_positions(self) -> bool:
        return len(self._positions) > 0

    @property
    def has_pending_entries(self) -> bool:
        """Висят заявки-мейкеры входа (их надо снять при остановке)."""
        return len(self._maker_quotes) > 0

    async def _acquire(self, ct: CancellationToken) -> None:
        ct.throw_if_cancellation_requested()
        await self._gate.acquire()

    # ------------------------- публичный цикл -------------------------

    async def process_opportunities(self, candidates: Sequence[SpreadEstimate], ct: CancellationToken) -> None:
        # устойчивость сигнала: связка должна продержаться confirm_scans сканов подряд — одиночный
        # «скачок» из-за запоздавшей котировки одной из бирж в сделку не превращается
        self._streaks = {key: self._streaks.get(key, 0) + 1 for key in {_streak_key(c) for c in candidates}}
        if not candidates:
            return

        required = max(1, self._config.current.arbitrage.confirm_scans)
        candidates = [c for c in candidates if self._streaks.get(_streak_key(c), 0) >= required]
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
                if candidate.symbol in self._positions or candidate.symbol in self._maker_quotes:
                    continue  # на символ — одна арбитражная позиция (или уже висит заявка-мейкер)

                blocked_until = self._reentry_blocked_until.get(candidate.symbol)
                if blocked_until is not None:
                    if self._clock.now() < blocked_until:
                        continue  # пауза после недавнего закрытия по этому символу (reentry/stop_loss_cooldown)
                    self._reentry_blocked_until.pop(candidate.symbol, None)

                if len(self._positions) + len(self._maker_quotes) + external_arbitrages >= options.max_open_positions:
                    return  # лимит сеансовых и найденных на биржах арбитражей (и висящих заявок-мейкеров) исчерпан

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
        to_decide: list[tuple[PositionPair, CloseReason, Decimal, Decimal]] = []
        """Новые решения о выходе: позиция, причина, брутто и расчётный PnL выхода по котировкам."""
        to_rebalance: list[PositionPair] = []

        for position in list(self._positions.values()):
            pending = self._close_attempts.get(position.symbol)
            if pending is not None:
                # закрытие уже начато: доводим его до конца независимо от текущего спреда — иначе
                # наполовину закрытая пара (голая нога) ждала бы, пока условие выхода случайно повторится
                if pending.retry_at is None or now >= pending.retry_at:
                    to_close.append((position, pending.reason))
                continue

            long_ticker = self._find_ticker(position.long_exchange_id, position.symbol)
            short_ticker = self._find_ticker(position.short_exchange_id, position.symbol)
            if long_ticker is None or short_ticker is None or not long_ticker.is_tradable or not short_ticker.is_tradable:
                continue

            current_gross = (short_ticker.bid - long_ticker.ask) / long_ticker.ask * HUNDRED
            # «текущий спред» меряется сторонами входа и потому оптимистичен на ширину обоих стаканов:
            # тейк-профит разрешаем только при PnL немедленного выхода выше нуля и не ниже min_exit_profit_percent
            net_now = self._net_profit_percent(position, long_ticker, short_ticker)
            signal: CloseReason | None
            if current_gross <= options.min_spread_percent_down and net_now > ZERO and net_now >= options.min_exit_profit_percent:
                signal = CloseReason.TAKE_PROFIT
            elif current_gross >= options.stop_loss_spread_percent:
                signal = CloseReason.STOP_LOSS
            else:
                signal = None

            # тейк-профит по старой котировке (поток одной из бирж молчит/переподключается) не берём:
            # «прибыль» там часто фантомная. Стоп-лосс аварийный — его возраст котировок не блокирует
            if signal == CloseReason.TAKE_PROFIT and self._quotes_too_old(position, long_ticker, short_ticker, options, now):
                signal = None

            # подтверждение сканами: разовый скачок котировки позицию не закрывает
            reason = self._confirm_exit_signal(position.symbol, signal, options)
            if reason is None and now - position.opened_at >= timedelta(minutes=options.max_position_age_minutes):
                reason = CloseReason.TIMEOUT

            if reason is not None:
                to_decide.append((position, reason, current_gross, net_now))
                continue

            # закрытие ног не завершено — сначала добьём закрытие, не ребалансируя наполовину голые ноги
            if position.long_closed != position.short_closed:
                continue

            if mode != NetworkMode.DRY_RUN and abs(position.imbalance) > self._rebalance_tolerance(position, options):
                to_rebalance.append(position)

        if not to_close and not to_decide and not to_rebalance:
            return

        await self._acquire(ct)
        try:
            for position, reason, current_gross, net_now in to_decide:
                if reason == CloseReason.TAKE_PROFIT and options.exit_confirm_order_book:
                    # котировки обещают прибыль — проверяем, даст ли её реальный стакан под объём ног
                    book_net = await self._confirm_exit_with_order_books(position, net_now, options, ct)
                    if book_net is None:
                        continue
                    net_now = book_net

                held_minutes = int((now - position.opened_at).total_seconds() // 60)
                held_seconds = int((now - position.opened_at).total_seconds())
                held = f"{held_minutes} мин" if held_minutes > 0 else f"{held_seconds} с"
                self._log.info(
                    f"Закрываю арбитраж {position.symbol} ({_describe_reason(reason)}): брутто сейчас {formatting.pct(current_gross)}, "
                    f"расчётный PnL выхода {formatting.pct(net_now)}, позиция открыта {held}"
                )
                to_close.append((position, reason))

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

        if options.confirm_order_book:
            confirmed = await self._confirm_with_order_books(estimate, amount, long_connector, short_connector, options, ct)
            if confirmed is None:
                return
            estimate = confirmed

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

    async def _confirm_with_order_books(
        self,
        estimate: SpreadEstimate,
        amount: Decimal,
        long_connector: ExchangeConnector,
        short_connector: ExchangeConnector,
        options: ArbitrageOptions,
        ct: CancellationToken,
    ) -> SpreadEstimate | None:
        """
        Перепроверка сигнала по свежим стаканам обеих бирж (REST): цена, по которой реально наберётся
        объём сделки, и тот же расчёт нетто, что у сканера. Не подтвердилось — None (сделки не будет).
        Подтвердилось — оценка с ценами стакана: от них считаются цены заявок.
        """
        key = _streak_key(estimate)
        now = self._clock.now()
        until = self._book_rejected_until.get(key)
        if until is not None and now < until:
            return None

        def reject(reason: str) -> None:
            self._book_rejected_until[key] = now + BOOK_REJECT_COOLDOWN
            self._log.info(f"{estimate.symbol}: {reason} — сделка не открывается")

        depth = max(1, options.order_book_depth)
        try:
            long_book, short_book = await asyncio.wait_for(
                asyncio.gather(
                    long_connector.fetch_order_book(estimate.symbol, depth, ct),
                    short_connector.fetch_order_book(estimate.symbol, depth, ct),
                ),
                timeout=options.order_book_timeout_ms / 1000,
            )
        except TimeoutError:
            reject(f"стаканы не получены за {options.order_book_timeout_ms} мс")
            return None

        if long_book is None or short_book is None:
            missing = long_connector.display_name if long_book is None else short_connector.display_name
            reject(f"стакан {missing} недоступен")
            return None

        buy = _sweep(long_book, OrderSide.BUY, amount)
        sell = _sweep(short_book, OrderSide.SELL, amount)
        if buy is None or sell is None:
            thin = long_connector.display_name if buy is None else short_connector.display_name
            reject(f"в стакане {thin} не хватает глубины на {formatting.volume(amount)}")
            return None

        gross = (sell - buy) / buy * HUNDRED
        fees = (estimate.long_leg.fee_percent + estimate.short_leg.fee_percent) * 2 if options.include_fees else ZERO
        net = gross - fees - options.slippage_buffer_percent
        if net < options.min_spread_percent_up:
            reject(
                f"спред не подтвердился стаканом: по котировкам нетто {formatting.pct(estimate.net_percent)}, "
                f"по стакану {formatting.pct(net)} (купить {long_connector.id} @ {formatting.price(buy)}, "
                f"продать {short_connector.id} @ {formatting.price(sell)})"
            )
            return None

        self._book_rejected_until.pop(key, None)
        return replace(
            estimate,
            long_leg=replace(estimate.long_leg, price=buy),
            short_leg=replace(estimate.short_leg, price=sell),
            gross_percent=gross.quantize(_FOUR_PLACES, rounding=ROUND_HALF_EVEN),
            net_percent=net.quantize(_FOUR_PLACES, rounding=ROUND_HALF_EVEN),
        )

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
                # выходим, как только заявка мертва ИЛИ исполнена: повторный учёт уже свёрнутого
                # исполнения удваивал объём ноги, и откат шёл на вдвое больший объём (reduceOnly отказ)
                while attempt < 3 and not leg.dead and not leg.done:
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

            # откат набранного — market reduceOnly с ретраями; объём не больше фактической позиции биржи
            remaining = leg.filled
            if remaining > ZERO:
                found, position_amount = await _exchange_position_amount(leg.connector, leg.request.symbol, leg.request.side)
                if found and position_amount is not None:
                    remaining = min(remaining, position_amount)
            close_side = OrderSide.SELL if leg.request.side == OrderSide.BUY else OrderSide.BUY
            attempt = 0
            while attempt < 3 and remaining > ZERO:
                rollback = await leg.connector.place_order(OrderRequest(leg.request.symbol, close_side, remaining, reduce_only=True), _NONE)
                if rollback.success:
                    done = rollback.filled_amount if rollback.filled_amount > ZERO else remaining
                    price = f" @ {formatting.price(rollback.average_price)}" if rollback.average_price is not None else ""
                    self._log.warning(
                        f"[{leg.connector.display_name}] откат {close_side} {leg.request.symbol} × {formatting.volume(done)}{price} исполнен"
                    )
                    remaining -= done
                else:
                    self._log.error(
                        f"[{leg.connector.display_name}] откат {close_side} {leg.request.symbol} × {dec_str(remaining)} "
                        f"не прошёл (попытка {attempt + 1}): {rollback.error}"
                    )
                    await asyncio.sleep(0.25)
                    # отказ reduceOnly часто значит «позиция меньше, чем мы думаем» — сверяемся с биржей
                    found, position_amount = await _exchange_position_amount(leg.connector, leg.request.symbol, leg.request.side)
                    if found:
                        if position_amount is None or position_amount <= ZERO:
                            self._log.warning(f"[{leg.connector.display_name}] {leg.request.symbol}: позиции на бирже нет — откатывать нечего")
                            remaining = ZERO
                        else:
                            remaining = min(remaining, position_amount)
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

    # ------------------------- вход «maker + хедж» -------------------------

    async def process_maker_quotes(
        self,
        candidates: Sequence[SpreadEstimate],
        tickers_by_exchange: Mapping[str, Mapping[str, TickerSnapshot]],
        ct: CancellationToken,
    ) -> None:
        """
        Такт входа «maker + хедж»: висящие заявки проверяются на исполнение (исполнилась — сразу хедж),
        переставляются за котировкой хедж-биржи или снимаются, когда сигнал пропал; по новым сигналам
        (брутто ≥ trigger_gross_percent) ставятся новые заявки. candidates — связки сканера по лучшим ценам.
        """
        self._update_ticker_cache(tickers_by_exchange)
        options = self._config.current.arbitrage
        active = options.enabled and options.maker_hedge.enabled
        if not active and not self._maker_quotes:
            return
        self._record_mids(tickers_by_exchange, {c.symbol for c in candidates} | set(self._maker_quotes), options)

        await self._acquire(ct)
        try:
            if not active:
                for quote in list(self._maker_quotes.values()):
                    await self._withdraw_quote(quote, "вход maker + хедж выключен", options)
                return

            now = self._clock.now()
            signals = {c.symbol: c for c in candidates}
            for quote in list(self._maker_quotes.values()):
                await self._advance_quote(quote, signals.get(quote.symbol), options, now)

            if ct.is_cancellation_requested or not candidates:
                return  # остановка робота: новых заявок не ставим

            await self._reconcile_external_exposure(ct)
            for candidate in candidates:
                if len(self._maker_quotes) >= max(0, options.maker_hedge.max_quotes):
                    break
                if self._may_start_quote(candidate, options, now):
                    await self._start_quote(candidate, options, now)
        finally:
            self._gate.release()

    async def cancel_pending_entries(self, ct: CancellationToken) -> None:
        """Снять все заявки-мейкеры (остановка робота). Исполненное к этому моменту хеджируется."""
        if not self._maker_quotes:
            return
        options = self._config.current.arbitrage
        await self._acquire(ct)
        try:
            for quote in list(self._maker_quotes.values()):
                await self._withdraw_quote(quote, "остановка робота", options)
        finally:
            self._gate.release()

    def _may_start_quote(self, candidate: SpreadEstimate, options: ArbitrageOptions, now: datetime) -> bool:
        symbol = candidate.symbol
        if symbol in self._positions or symbol in self._maker_quotes:
            return False
        for blocked in (self._reentry_blocked_until, self._maker_blocked_until):
            until = blocked.get(symbol)
            if until is not None:
                if now < until:
                    return False
                blocked.pop(symbol, None)
        if len(self._positions) + len(self._maker_quotes) + self._count_external_arbitrages() >= options.max_open_positions:
            return False
        per_exchange = self._count_per_exchange()
        if (
            per_exchange.get(candidate.long_leg.exchange_id, 0) >= options.max_positions_per_exchange
            or per_exchange.get(candidate.short_leg.exchange_id, 0) >= options.max_positions_per_exchange
        ):
            return False
        mh = options.maker_hedge
        swing = self._volatility_bps(symbol, (candidate.long_leg.exchange_id, candidate.short_leg.exchange_id), options)
        if swing is not None:
            noted = self._volatility_noted_at.get(symbol)
            if noted is None or now - noted >= VOLATILITY_NOTE_EVERY:
                self._volatility_noted_at[symbol] = now
                self._log.info(
                    f"{symbol}: заявку-мейкер не ставлю — размах цены {dec_str(swing.quantize(Decimal('0.1')))} bps "
                    f"за {mh.volatility_window_ms} мс (предел {dec_str(mh.max_volatility_bps)} bps)"
                )
            return False
        return True

    def _record_mids(self, tickers_by_exchange: Mapping[str, Mapping[str, TickerSnapshot]], symbols: set[str], options: ArbitrageOptions) -> None:
        """Запоминает средние цены символов-кандидатов и висящих заявок на всех биржах (окно volatility_window_ms)."""
        mh = options.maker_hedge
        if mh.max_volatility_bps <= ZERO or mh.volatility_window_ms <= 0:
            self._mid_history.clear()
            return
        now = self._clock.now()
        horizon = now - timedelta(milliseconds=mh.volatility_window_ms)
        for exchange_id, tickers in tickers_by_exchange.items():
            for symbol in symbols:
                t = tickers.get(symbol)
                if t is None or not t.is_tradable:
                    continue
                history = self._mid_history.setdefault((exchange_id, symbol), deque())
                history.append((now, (t.bid + t.ask) / 2))
        for key in list(self._mid_history):
            history = self._mid_history[key]
            while history and history[0][0] < horizon:
                history.popleft()
            if not history:
                del self._mid_history[key]

    def _volatility_bps(self, symbol: str, exchange_ids: Sequence[str], options: ArbitrageOptions) -> Decimal | None:
        """Наибольший размах mid по биржам связки за окно, bps, если он выше max_volatility_bps; иначе None."""
        limit = options.maker_hedge.max_volatility_bps
        if limit <= ZERO:
            return None
        worst = ZERO
        for exchange_id in exchange_ids:
            history = self._mid_history.get((exchange_id, symbol))
            if not history:
                continue
            low = min(m for _, m in history)
            high = max(m for _, m in history)
            if low > ZERO:
                worst = max(worst, (high - low) / low * _TEN_THOUSAND)
        return worst if worst > limit else None

    def _maker_allowed(self, options: ArbitrageOptions, connectors: Sequence[ExchangeConnector]) -> set[str]:
        """Биржи, где можно стоять мейкером: из maker_exchanges (пусто — все) и только с post-only."""
        configured = {e.lower() for e in options.maker_hedge.maker_exchanges}
        allowed: set[str] = set()
        for connector in connectors:
            exchange_id = connector.id.lower()
            if configured and exchange_id not in configured:
                continue
            if not ExchangeCapabilityMap.for_id(connector.id).supports_post_only:
                note = f"{connector.id}|maker-post-only"
                if note not in self._policy_notes:
                    self._policy_notes.add(note)
                    self._log.warning(f"[{connector.display_name}] post-only не поддерживается — мейкером на этой бирже не встаю")
                continue
            allowed.add(exchange_id)
        return allowed

    def _maker_target(self, options: ArbitrageOptions) -> Decimal:
        target = options.maker_hedge.target_net_percent
        return target if target is not None else options.min_spread_percent_up

    async def _start_quote(self, candidate: SpreadEstimate, options: ArbitrageOptions, now: datetime) -> None:
        long_connector = self._registry.find(candidate.long_leg.exchange_id)
        short_connector = self._registry.find(candidate.short_leg.exchange_id)
        cheap = self._find_ticker(candidate.long_leg.exchange_id, candidate.symbol)
        rich = self._find_ticker(candidate.short_leg.exchange_id, candidate.symbol)
        if long_connector is None or short_connector is None or cheap is None or rich is None:
            return

        funding_cost = funding_cost_percent(candidate, options.funding_periods_in_net)
        plan = plan_quote(
            cheap,
            rich,
            _fees_of(long_connector, candidate.symbol),
            _fees_of(short_connector, candidate.symbol),
            target_net=self._maker_target(options) + funding_cost,
            buffer=options.slippage_buffer_percent,
            include_fees=options.include_fees,
            leg=options.maker_hedge.maker_leg,
            allowed_makers=self._maker_allowed(options, [long_connector, short_connector]),
        )
        if plan is None:
            return

        amount = self._resolve_amount(long_connector, short_connector, candidate, options)
        if amount is None:
            return

        maker, hedge = (short_connector, long_connector) if plan.maker_side == OrderSide.SELL else (long_connector, short_connector)
        description = (
            f"{candidate.symbol}: заявка-мейкер {plan.maker_side} {formatting.volume(amount)} @ {formatting.price(plan.price)} "
            f"на {maker.display_name} (+{dec_str(plan.distance_bps.quantize(Decimal('0.1')))} bps от края стакана), "
            f"хедж {plan.hedge_side} на {hedge.display_name} от {formatting.price(plan.hedge_reference)}, "
            f"расчётное нетто {formatting.pct(plan.expected_net_percent)} (брутто сейчас {formatting.pct(candidate.gross_percent)})"
            + (f", в т. ч. на фандинг {formatting.pct(funding_cost)}" if funding_cost > ZERO else "")
        )

        if self._config.current.general.network_mode == NetworkMode.DRY_RUN:
            self._maker_quotes[candidate.symbol] = _MakerQuote(
                candidate.symbol, plan, maker, hedge, amount, None, now, now, now, funding_cost=funding_cost
            )
            self._log.info(f"[СИМУЛЯЦИЯ] {description}")
            return

        for connector in (long_connector, short_connector):
            key = (connector.id, candidate.symbol, options.leverage)
            if key not in self._leverage_applied:
                await connector.set_leverage(options.leverage, candidate.symbol, _NONE)
                self._leverage_applied.add(key)

        leg = self._build_maker_leg(maker, candidate.symbol, plan, amount)
        if not await self._place(leg, _NONE):
            self._maker_blocked_until[candidate.symbol] = now + MAKER_REJECT_COOLDOWN
            return

        quote = _MakerQuote(candidate.symbol, plan, maker, hedge, amount, leg, now, now, now, funding_cost=funding_cost)
        self._maker_quotes[candidate.symbol] = quote
        self._log.info(description)
        if leg.filled > ZERO:
            await self._hedge_quote(quote, options)  # исполнилась сразу (биржа без post-only)

    def _build_maker_leg(self, maker: ExchangeConnector, symbol: str, plan: MakerQuotePlan, amount: Decimal) -> _LegState:
        caps = ExchangeCapabilityMap.for_id(maker.id)
        tif = TimeInForce.POST_ONLY if caps.supports_post_only else TimeInForce.GTC
        policy = OrderPolicy(OrderType.LIMIT, tif, ZERO, None, None)
        leg = self._build_leg(maker, policy, symbol, plan.maker_side, plan.price, amount)
        leg.taker_percent = _fees_of(maker, symbol).maker_percent  # оценка комиссии мейкер-ноги — по ставке maker
        return leg

    async def _advance_quote(self, quote: _MakerQuote, signal: SpreadEstimate | None, options: ArbitrageOptions, now: datetime) -> None:
        mh = options.maker_hedge
        long_id, short_id = (quote.hedge.id, quote.maker.id) if quote.plan.maker_side == OrderSide.SELL else (quote.maker.id, quote.hedge.id)
        if signal is not None and signal.long_leg.exchange_id == long_id and signal.short_leg.exchange_id == short_id:
            quote.signal_at = now
            quote.funding_cost = funding_cost_percent(signal, options.funding_periods_in_net)

        maker_ticker = self._find_ticker(quote.maker.id, quote.symbol)
        hedge_ticker = self._find_ticker(quote.hedge.id, quote.symbol)

        # 1) исполнение — хедж немедленно
        if quote.leg is None:
            if maker_ticker is not None and simulated_fill(quote.plan.maker_side, quote.plan.price, maker_ticker):
                self._fill_simulated_quote(quote, hedge_ticker, options)
                return
        else:
            leg = quote.leg
            if leg.order_id is not None and not leg.done and not leg.dead:
                update = await quote.maker.fetch_order(leg.order_id, quote.symbol, _NONE)
                if update is None:
                    update = await quote.maker.fetch_order(leg.order_id, quote.symbol, _NONE, fresh=True)
                if update is not None:
                    _record_fill(leg, update.filled_amount, update.average_price, update.fee_quote)
                    if update.is_filled or leg.filled >= leg.target_amount:
                        leg.done = True
                    elif update.is_dead:
                        _fold_current_fill(leg)
                        leg.dead = True
            if leg.filled > ZERO:
                await self._hedge_quote(quote, options)
                return
            if leg.dead:
                self._drop_quote(quote, MAKER_REJECT_COOLDOWN)
                self._log.warning(f"[{quote.maker.display_name}] {quote.symbol}: заявка-мейкер снята биржей без исполнения")
                return

        # 2) снять: сигнал пропал дольше cancel_after_ms или котировки бирж устарели
        max_age = timedelta(seconds=self._config.current.symbols.max_ticker_age_seconds)
        reason: str | None = None
        cooldown = MAKER_WITHDRAW_COOLDOWN
        swing = self._volatility_bps(quote.symbol, (quote.maker.id, quote.hedge.id), options)
        if now - quote.signal_at > timedelta(milliseconds=mh.cancel_after_ms):
            reason = f"сигнал пропал дольше {mh.cancel_after_ms} мс"
        elif maker_ticker is None or hedge_ticker is None or not maker_ticker.is_tradable or not hedge_ticker.is_tradable:
            reason = "нет котировок"
        elif any(t.timestamp is not None and now - t.timestamp > max_age for t in (maker_ticker, hedge_ticker)):
            reason = "котировки устарели"
        elif swing is not None:
            # исполнение в дёргающемся рынке — почти всегда «против нас»: хедж уйдёт по сдвинувшейся цене
            reason = f"рынок дёргается: размах цены {dec_str(swing.quantize(Decimal('0.1')))} bps за {mh.volatility_window_ms} мс"
            cooldown = timedelta(milliseconds=max(0, mh.volatility_cooldown_ms))
        if reason is not None:
            await self._withdraw_quote(quote, reason, options, cooldown)
            return

        # 3) переставить за котировкой хедж-биржи
        assert maker_ticker is not None and hedge_ticker is not None
        fresh = replan(
            quote.plan,
            maker_ticker,
            hedge_ticker,
            _fees_of(quote.maker, quote.symbol),
            _fees_of(quote.hedge, quote.symbol),
            target_net=self._maker_target(options) + quote.funding_cost,
            buffer=options.slippage_buffer_percent,
            include_fees=options.include_fees,
        )
        if fresh is None:
            await self._withdraw_quote(quote, "цену не пересчитать", options)
            return
        if deviation_bps(quote.plan.price, fresh.price) < mh.requote_threshold_bps:
            return
        # котировка хеджа ушла ПРОТИВ заявки (её исполнение теперь дало бы меньше расчётного) — переставляем сразу,
        # не дожидаясь min_requote_interval_ms: пауза между перестановками бережёт лимиты, а не наш спред
        adverse = (
            fresh.hedge_reference > quote.plan.hedge_reference
            if quote.plan.maker_side == OrderSide.SELL
            else fresh.hedge_reference < quote.plan.hedge_reference
        )
        if not adverse and now - quote.requoted_at < timedelta(milliseconds=mh.min_requote_interval_ms):
            return
        await self._requote_maker(quote, fresh, options, now)

    async def _cancel_maker_order(self, quote: _MakerQuote) -> bool:
        """
        Снять заявку-мейкер и учесть её финальное исполнение. False — снять не удалось (заявка жива).
        """
        leg = quote.leg
        if leg is None or leg.order_id is None or leg.done or leg.dead:
            return True
        await quote.maker.cancel_order(leg.order_id, quote.symbol, _NONE)
        final = await quote.maker.fetch_order(leg.order_id, quote.symbol, _NONE, fresh=True)
        if final is not None:
            _record_fill(leg, final.filled_amount, final.average_price, final.fee_quote)
            if final.is_filled:
                leg.done = True
                return True
            if not final.is_dead:
                return False
        _fold_current_fill(leg)
        leg.dead = True
        return True

    async def _requote_maker(self, quote: _MakerQuote, fresh: MakerQuotePlan, options: ArbitrageOptions, now: datetime) -> None:
        old_price = quote.plan.price
        if quote.leg is not None:
            if not await self._cancel_maker_order(quote):
                self._log.warning(f"[{quote.maker.display_name}] {quote.symbol}: заявка-мейкер не снята — перестановка откладывается")
                return
            if quote.leg.filled > ZERO:
                await self._hedge_quote(quote, options)  # исполнилась в гонке с отменой
                return
            leg = self._build_maker_leg(quote.maker, quote.symbol, fresh, quote.amount)
            if not await self._place(leg, _NONE):
                self._drop_quote(quote, MAKER_REJECT_COOLDOWN)
                return
            quote.leg = leg

        quote.plan = fresh
        quote.requoted_at = now
        quote.requotes += 1
        self._logger.debug(f"Maker requote {quote.symbol} on {quote.maker.id}: {old_price} → {fresh.price}")
        if quote.leg is not None and quote.leg.filled > ZERO:
            await self._hedge_quote(quote, options)

    async def _withdraw_quote(
        self, quote: _MakerQuote, reason: str, options: ArbitrageOptions, cooldown: timedelta = MAKER_WITHDRAW_COOLDOWN
    ) -> None:
        if quote.leg is not None:
            if not await self._cancel_maker_order(quote):
                self._log.warning(f"[{quote.maker.display_name}] {quote.symbol}: заявка-мейкер не снята ({reason}) — повторю на следующем такте")
                return
            if quote.leg.filled > ZERO:
                await self._hedge_quote(quote, options)
                return
        self._drop_quote(quote, cooldown)
        self._log.info(
            f"{'[СИМУЛЯЦИЯ] ' if quote.simulated else ''}{quote.symbol}: заявка-мейкер на {quote.maker.display_name} снята — {reason} "
            f"(перестановок {quote.requotes})"
        )

    def _drop_quote(self, quote: _MakerQuote, cooldown: timedelta) -> None:
        self._maker_quotes.pop(quote.symbol, None)
        self._maker_blocked_until[quote.symbol] = self._clock.now() + cooldown

    async def _hedge_quote(self, quote: _MakerQuote, options: ArbitrageOptions) -> None:
        """
        Мейкер исполнился (целиком или частично): добрать финальный объём (остаток заявки снимается)
        и сразу закрыть риск второй ногой — IOC-лимиткой со смещением, остаток — market. Хедж не прошёл —
        мейкер-нога откатывается. Всё без токена отмены: голая нога важнее остановки.
        """
        maker_leg = quote.leg
        assert maker_leg is not None
        self._maker_quotes.pop(quote.symbol, None)
        symbol = quote.symbol
        mh = options.maker_hedge
        started = self._clock.now()

        if not maker_leg.done and not maker_leg.dead and maker_leg.order_id is not None:
            if not await self._cancel_maker_order(quote):
                # остаток снять не удалось — хеджируем набранное, заявку доснимет цикл ведения
                self._stray_orders.append(_StrayOrder(quote.maker, maker_leg.order_id, symbol, maker_leg.request.side))
                _fold_current_fill(maker_leg)
        elif maker_leg.current_filled > ZERO:
            _fold_current_fill(maker_leg)
        filled = maker_leg.filled
        maker_leg.target_amount = filled

        hedge_ticker = self._find_ticker(quote.hedge.id, symbol)
        side = quote.plan.hedge_side
        reference = quote.plan.hedge_reference
        if hedge_ticker is not None and hedge_ticker.is_tradable:
            reference = hedge_ticker.ask if side == OrderSide.BUY else hedge_ticker.bid
        caps = ExchangeCapabilityMap.for_id(quote.hedge.id)
        policy = OrderPolicy(OrderType.LIMIT, TimeInForce.IOC, mh.hedge_offset_bps, None, None) if caps.supports_ioc else MARKET_ORDER
        hedge_leg = self._build_leg(quote.hedge, policy, symbol, side, reference, filled)

        self._log.info(
            f"{symbol}: заявка-мейкер на {quote.maker.display_name} исполнена на {formatting.volume(filled)} "
            f"@ {formatting.price(maker_leg.average_price or quote.plan.price)} — хеджирую {side} на {quote.hedge.display_name}"
        )

        if await self._place(hedge_leg, _NONE) and not hedge_leg.done and hedge_leg.order_id is not None:
            # IOC живёт мгновение: финальное состояние — запросом
            final = await quote.hedge.fetch_order(hedge_leg.order_id, symbol, _NONE, fresh=True)
            if final is not None:
                _record_fill(hedge_leg, final.filled_amount, final.average_price, final.fee_quote)
                hedge_leg.done = final.is_filled or hedge_leg.filled >= hedge_leg.target_amount
                if not hedge_leg.done and not final.is_dead:
                    await quote.hedge.cancel_order(hedge_leg.order_id, symbol, _NONE)
        _fold_current_fill(hedge_leg)  # исполнение IOC — в итог ноги до возможной market-добивки

        remainder = hedge_leg.target_amount - hedge_leg.filled
        if remainder > ZERO and not hedge_leg.done and mh.hedge_fallback_to_market:
            self._log.warning(f"[{quote.hedge.display_name}] {symbol}: хедж IOC добрал {formatting.volume(hedge_leg.filled)}, остаток market")
            hedge_leg.policy = MARKET_ORDER
            hedge_leg.request = replace(
                hedge_leg.request, amount=remainder, type=OrderType.MARKET, price=None, time_in_force=TimeInForce.GTC, exchange_params=None
            )
            hedge_leg.dead = False
            hedge_leg.error = None
            if await self._place(hedge_leg, _NONE):
                _fold_current_fill(hedge_leg)
                hedge_leg.done = hedge_leg.filled >= hedge_leg.target_amount

        long_leg, short_leg = (hedge_leg, maker_leg) if quote.plan.maker_side == OrderSide.SELL else (maker_leg, hedge_leg)
        estimate = self._maker_estimate(quote, long_leg, short_leg, reference)
        elapsed_ms = int((self._clock.now() - started).total_seconds() * 1000)

        if hedge_leg.filled <= ZERO:
            reason = f"хедж {side} на {quote.hedge.display_name} не исполнен: {hedge_leg.error or 'нет исполнения'}"
            self._log.error(f"{symbol}: {reason} — откатываю мейкер-ногу")
            self._maker_blocked_until[symbol] = self._clock.now() + MAKER_HEDGE_FAIL_COOLDOWN
            hedge_leg.dead = True
            maker_leg.done = False
            maker_leg.dead = True  # заявка уже снята — откат только набранного
            residual_long, residual_short = await self._abort_legs([long_leg, short_leg], _NONE)
            self._stats.record_open_failed(symbol, reason)
            if residual_long > ZERO or residual_short > ZERO:
                self._register_partial_position(estimate, long_leg, short_leg, residual_long, residual_short)
            return

        if hedge_leg.filled < filled:
            self._log.warning(
                f"{symbol}: хедж набрал {formatting.volume(hedge_leg.filled)} из {formatting.volume(filled)} — перекос выровняет ребаланс"
            )
        self._register_position(estimate, long_leg, short_leg)
        self._log.info(f"{symbol}: вход maker + хедж за {elapsed_ms} мс от обнаружения исполнения, перестановок заявки {quote.requotes}")
        self._warn_hedge_slippage(quote, hedge_leg.average_price or reference, maker_leg.average_price or quote.plan.price, options)

    def _maker_estimate(self, quote: _MakerQuote, long_leg: _LegState, short_leg: _LegState, hedge_reference: Decimal) -> SpreadEstimate:
        """Оценка-«сигнал» для регистрации позиции: цена мейкера и котировка хеджа в момент исполнения."""
        if quote.plan.maker_side == OrderSide.SELL:
            long_price, short_price = hedge_reference, quote.plan.price
            long_fee, short_fee = _fees_of(quote.hedge, quote.symbol).taker_percent, _fees_of(quote.maker, quote.symbol).maker_percent
        else:
            long_price, short_price = quote.plan.price, hedge_reference
            long_fee, short_fee = _fees_of(quote.maker, quote.symbol).maker_percent, _fees_of(quote.hedge, quote.symbol).taker_percent
        gross = (short_price - long_price) / long_price * HUNDRED if long_price > ZERO else ZERO
        return SpreadEstimate(
            quote.symbol,
            OpportunityLeg(long_leg.connector.id, long_price, long_fee),
            OpportunityLeg(short_leg.connector.id, short_price, short_fee),
            gross.quantize(_FOUR_PLACES, rounding=ROUND_HALF_EVEN),
            quote.plan.expected_net_percent.quantize(_FOUR_PLACES, rounding=ROUND_HALF_EVEN),
            ZERO,
            self._clock.now(),
        )

    def _fill_simulated_quote(self, quote: _MakerQuote, hedge_ticker: TickerSnapshot | None, options: ArbitrageOptions) -> None:
        """DryRun: заявка «исполнилась» по своей цене, хедж — по текущей котировке хедж-биржи."""
        self._maker_quotes.pop(quote.symbol, None)
        side = quote.plan.hedge_side
        hedge_price = quote.plan.hedge_reference
        if hedge_ticker is not None and hedge_ticker.is_tradable:
            hedge_price = hedge_ticker.ask if side == OrderSide.BUY else hedge_ticker.bid

        maker_fees = _fees_of(quote.maker, quote.symbol)
        hedge_fees = _fees_of(quote.hedge, quote.symbol)
        fees = quote.amount * (quote.plan.price * maker_fees.maker_percent + hedge_price * hedge_fees.taker_percent) / HUNDRED
        if quote.plan.maker_side == OrderSide.SELL:
            long_id, short_id, entry_long, entry_short = quote.hedge.id, quote.maker.id, hedge_price, quote.plan.price
        else:
            long_id, short_id, entry_long, entry_short = quote.maker.id, quote.hedge.id, quote.plan.price, hedge_price

        position = PositionPair(
            symbol=quote.symbol,
            long_exchange_id=long_id,
            short_exchange_id=short_id,
            long_size=quote.amount,
            short_size=quote.amount,
            entry_long=entry_long,
            entry_short=entry_short,
            fees_entry_usd=fees,
            opened_at=self._clock.now(),
            simulated=True,
        )
        self._positions[position.symbol] = position
        self._stats.record_opened(position)
        gross = (entry_short - entry_long) / entry_long * HUNDRED
        self._log.success(
            f"[СИМУЛЯЦИЯ] Открыт арбитраж maker + хедж {quote.symbol}: {formatting.volume(quote.amount)} × лонг {long_id} "
            f"@ {formatting.price(entry_long)} / шорт {short_id} @ {formatting.price(entry_short)}, брутто {formatting.pct(gross)} "
            f"(расчёт при выставлении {formatting.pct(quote.plan.expected_net_percent)} нетто), перестановок {quote.requotes}"
        )
        self._warn_hedge_slippage(quote, hedge_price, quote.plan.price, options)

    def _warn_hedge_slippage(self, quote: _MakerQuote, hedge_price: Decimal, maker_price: Decimal, options: ArbitrageOptions) -> None:
        """
        Хедж ушёл хуже котировки, от которой считалась цена мейкера, — громко в журнал: это не шум, а цена
        входа «maker + хедж» (мейкер исполняется, когда рынок двинулся, и хедж догоняет уже сдвинувшуюся цену).
        """
        reference = quote.plan.hedge_reference
        if reference <= ZERO or hedge_price <= ZERO:
            return
        worse = hedge_price - reference if quote.plan.hedge_side == OrderSide.BUY else reference - hedge_price
        slip_bps = worse / reference * _TEN_THOUSAND
        if slip_bps <= options.maker_hedge.hedge_slippage_warn_bps:
            return
        long_price, short_price = (hedge_price, maker_price) if quote.plan.maker_side == OrderSide.SELL else (maker_price, hedge_price)
        gross = (short_price - long_price) / long_price * HUNDRED if long_price > ZERO else ZERO
        self._log.warning(
            f"{'[СИМУЛЯЦИЯ] ' if quote.simulated else ''}{quote.symbol}: хедж {quote.plan.hedge_side} на {quote.hedge.display_name} "
            f"хуже расчёта на {dec_str(slip_bps.quantize(Decimal('0.1')))} bps ({formatting.price(reference)} → {formatting.price(hedge_price)}), "
            f"брутто входа {formatting.pct(gross)} вместо расчётного нетто {formatting.pct(quote.plan.expected_net_percent)}"
        )

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
        return self._net_exit_percent(position, long_ticker.bid, short_ticker.ask)

    def _net_exit_percent(self, position: PositionPair, long_exit: Decimal, short_exit: Decimal) -> Decimal:
        """Расчётный PnL пары (в % оборота) при выходе продажей лонга по long_exit и откупом шорта по short_exit."""
        if position.entry_long <= ZERO or long_exit <= ZERO:
            return ZERO

        entry_gross = (position.entry_short - position.entry_long) / position.entry_long * HUNDRED
        unwind_cost = (short_exit - long_exit) / long_exit * HUNDRED
        round_trip_fees = 2 * (
            self._taker_percent_of(position.long_exchange_id, position.symbol) + self._taker_percent_of(position.short_exchange_id, position.symbol)
        )
        return entry_gross - unwind_cost - round_trip_fees

    # ------------------------- защита выхода -------------------------

    def _confirm_exit_signal(self, symbol: str, signal: CloseReason | None, options: ArbitrageOptions) -> CloseReason | None:
        """
        Условие выхода должно продержаться exit_confirm_scans сканов подряд с той же причиной:
        одиночный скачок котировки тонкого символа позицию не закрывает. None — выходить рано.
        """
        if signal is None:
            self._exit_streaks.pop(symbol, None)
            return None

        previous = self._exit_streaks.get(symbol)
        count = previous[1] + 1 if previous is not None and previous[0] == signal else 1
        self._exit_streaks[symbol] = (signal, count)
        return signal if count >= max(1, options.exit_confirm_scans) else None

    def _quotes_too_old(
        self, position: PositionPair, long_ticker: TickerSnapshot, short_ticker: TickerSnapshot, options: ArbitrageOptions, now: datetime
    ) -> bool:
        """Котировка какой-то ноги старше exit_max_quote_age_ms — решение о тейк-профите откладывается."""
        limit_ms = options.exit_max_quote_age_ms
        if limit_ms <= 0:
            return False

        ages = [_ticker_age_ms(t, now) for t in (long_ticker, short_ticker)]
        if all(age is None or age <= limit_ms for age in ages):
            self._stale_exit_noted_at.pop(position.symbol, None)
            return False

        noted = self._stale_exit_noted_at.get(position.symbol)
        if noted is None or now - noted >= STALE_EXIT_NOTE_EVERY:
            self._stale_exit_noted_at[position.symbol] = now
            text = "/".join("н/д" if a is None else f"{a:.0f}" for a in ages)
            self._log.info(f"{position.symbol}: тейк-профит отложен — котировки старше {limit_ms} мс (лонг/шорт {text} мс), жду свежих")
        return True

    async def _confirm_exit_with_order_books(
        self, position: PositionPair, quoted_net: Decimal, options: ArbitrageOptions, ct: CancellationToken
    ) -> Decimal | None:
        """
        Перепроверка тейк-профита по свежим стаканам обеих бирж (REST): по каким ценам реально
        продастся лонг и откупится шорт фактического объёма. PnL выхода ниже порога — None (не закрываем).
        """
        symbol = position.symbol
        now = self._clock.now()
        until = self._exit_book_rejected_until.get(symbol)
        if until is not None and now < until:
            return None

        def reject(reason: str) -> None:
            self._exit_book_rejected_until[symbol] = now + EXIT_BOOK_RECHECK
            self._exit_streaks.pop(symbol, None)  # подтверждение сканами начнётся заново
            self._log.info(f"{symbol}: {reason} — позиция остаётся открытой")

        long_connector = self._registry.find(position.long_exchange_id)
        short_connector = self._registry.find(position.short_exchange_id)
        if long_connector is None or short_connector is None:
            return None

        depth = max(1, options.order_book_depth)
        try:
            long_book, short_book = await asyncio.wait_for(
                asyncio.gather(
                    long_connector.fetch_order_book(symbol, depth, ct),
                    short_connector.fetch_order_book(symbol, depth, ct),
                ),
                timeout=options.order_book_timeout_ms / 1000,
            )
        except TimeoutError:
            reject(f"тейк-профит не проверен: стаканы не получены за {options.order_book_timeout_ms} мс")
            return None

        if long_book is None or short_book is None:
            missing = long_connector.display_name if long_book is None else short_connector.display_name
            reject(f"тейк-профит не проверен: стакан {missing} недоступен")
            return None

        sell_long = _sweep(long_book, OrderSide.SELL, position.long_size)
        buy_short = _sweep(short_book, OrderSide.BUY, position.short_size)
        if sell_long is None or buy_short is None:
            thin = long_connector.display_name if sell_long is None else short_connector.display_name
            reject(f"тейк-профит не подтвердился: в стакане {thin} не хватает глубины на объём позиции")
            return None

        net = self._net_exit_percent(position, sell_long, buy_short)
        required = max(options.min_exit_profit_percent, ZERO)
        if net <= ZERO or net < required:
            reject(
                f"тейк-профит не подтвердился стаканом: по котировкам PnL выхода {formatting.pct(quoted_net)}, "
                f"по стакану {formatting.pct(net)} (продать {long_connector.id} @ {formatting.price(sell_long)}, "
                f"откупить {short_connector.id} @ {formatting.price(buy_short)})"
            )
            return None

        self._exit_book_rejected_until.pop(symbol, None)
        return net

    def _forget_exit_state(self, symbol: str) -> None:
        self._exit_streaks.pop(symbol, None)
        self._exit_book_rejected_until.pop(symbol, None)
        self._stale_exit_noted_at.pop(symbol, None)

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
        exposure_side = OrderSide.BUY if is_long else OrderSide.SELL
        policy = self._resolve_policy(connector, options, close=True)

        if policy.requires_price and not (reference is not None and reference > ZERO):
            # лимитную заявку выставить некуда (нет котировки) — страховка: закрываем market
            self._log.warning(f"[{connector.display_name}] {position.symbol}: нет котировки для лимитного закрытия — закрываю market")
            policy = MARKET_ORDER

        leg = self._build_leg(connector, policy, position.symbol, side, reference if reference is not None else ZERO, size, reduce_only=True)
        # закрытие ноги не обрывается остановкой: начатое закрытие должно попасть в учёт
        if not await self._place(leg, _NONE):
            # отказ reduce-only часто означает «позиции уже нет» (её закрыла прошлая заявка, чей статус
            # не дошёл, — SAND 18.10.2026): без сверки с биржей нога навсегда остаётся в учёте фантомом
            remaining = await self._settled_exchange_position(connector, exposure_side, position.symbol)
            if remaining is not None and remaining < size:
                closed = size - remaining
                self._log.warning(
                    f"[{connector.display_name}] {position.symbol}: заявка закрытия отклонена, но позиция на бирже "
                    f"{formatting.volume(remaining)} из {formatting.volume(size)} — учитываю закрытым {formatting.volume(closed)}"
                )
                return _CloseLegResult(closed, None, None)
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
                # статус неясен — решает фактическая позиция (с повторной сверкой: биржа обновляет её с задержкой)
                remaining = await self._settled_exchange_position(connector, exposure_side, position.symbol)
                if remaining is None or remaining >= size:
                    error = "рыночная заявка не подтверждена биржей" if update is None else f"рыночная заявка не исполнена (статус: {update.status})"
                    return _CloseLegResult(ZERO, leg.average_price, error)

                closed = size - remaining  # позиции на бирже нет (или меньше) — нога закрыта на разницу

        # комиссию берём по заявке, если по ней есть исполнение; «закрыто по факту отсутствия позиции» — оценка
        return _CloseLegResult(closed, leg.average_price, leg.error, leg.fee_quote if leg.filled > ZERO else None)

    async def _settled_exchange_position(self, connector: ExchangeConnector, exposure_side: OrderSide, symbol: str) -> Decimal | None:
        """
        Фактический объём позиции ноги на бирже (в базовой монете). Если позиция видна, сверка повторяется
        через паузу: сразу после исполнения биржа может ещё отдавать старый снимок. None — сверка недоступна.
        """
        amount = ZERO
        for attempt in range(2):
            try:
                positions = await connector.fetch_positions(_NONE)
            except OperationCanceledError:
                raise
            except Exception as ex:
                self._logger.debug(f"FetchPositions on {connector.id} failed", exc_info=ex)
                return None

            amount = sum((p.amount for p in positions if p.symbol == symbol and p.side == exposure_side and p.amount > ZERO), ZERO)
            if amount <= ZERO or attempt > 0:
                break
            await asyncio.sleep(self._position_settle_delay)
        return amount

    def _register_close_failure(self, position: PositionPair) -> timedelta:
        """Неудачная попытка закрытия: пауза до следующей (1, 2, 4 … 30 с) и предупреждение при затяжных отказах."""
        attempts = self._close_attempts.get(position.symbol)
        if attempts is None:
            return timedelta(0)

        attempts.failures += 1
        delay = min(CLOSE_RETRY_MAX, CLOSE_RETRY_BASE * (2 ** min(attempts.failures - 1, 10)))
        attempts.retry_at = self._clock.now() + delay
        if attempts.failures == CLOSE_ALERT_AFTER or (attempts.failures > CLOSE_ALERT_AFTER and attempts.failures % 100 == 0):
            self._log.error(
                f"ВНИМАНИЕ: {position.symbol} не закрывается {attempts.failures} попыток подряд — проверьте позиции "
                f"на {position.long_exchange_id}/{position.short_exchange_id} вручную; повтор раз в {int(CLOSE_RETRY_MAX.total_seconds())} с"
            )
        return delay

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

        # закрытие начато: дальше оно повторяется до конца (с паузами), даже если условие выхода пропадёт
        self._close_attempts.setdefault(position.symbol, _CloseAttempts(reason))

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
                        delay = self._register_close_failure(position)
                        self._log.error(
                            f"[{long_connector.display_name}] закрытие лонга {position.symbol} не удалось: "
                            f"{result.error or 'заявка не исполнена'} — повтор через {int(delay.total_seconds())} с"
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
                        # лонг уже закрыт — шорт остаётся голым, но позиция не теряется: повтор после паузы
                        delay = self._register_close_failure(position)
                        self._log.error(
                            f"[{short_connector.display_name}] закрытие шорта {position.symbol} не удалось: "
                            f"{result.error or 'заявка не исполнена'} — позиция остаётся в учёте, повтор через {int(delay.total_seconds())} с"
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
        self._close_attempts.pop(position.symbol, None)
        self._forget_exit_state(position.symbol)

        # пауза перед повторным входом: сразу после выхода спред по символу обычно ещё «дёргается»
        cooldown = options.reentry_cooldown_seconds
        if reason == CloseReason.STOP_LOSS and options.stop_loss_cooldown_seconds > 0:
            cooldown = options.stop_loss_cooldown_seconds
        if cooldown > 0:
            self._reentry_blocked_until[position.symbol] = position.closed_at + timedelta(seconds=cooldown)

        self._stats.record_closed(position)
        self._log.success(f"Закрыт арбитраж {position.symbol} ({_describe_reason(reason)}): PnL {formatting.usd(position.realized_pnl_usd)}")

    # ------------------------- вспомогательное -------------------------

    def _resolve_amount(
        self, long_connector: ExchangeConnector, short_connector: ExchangeConnector, estimate: SpreadEstimate, options: ArbitrageOptions
    ) -> Decimal | None:
        """
        Объём сделки в базовой монете, одинаковый для обеих ног: не меньше минимального объёма каждой биржи
        и кратный шагу лота обеих (шаги уже пересчитаны из контрактов в монеты). Иначе биржа округлит
        ноги по-разному, и «дельта-нейтральная» пара окажется с перекосом.
        """
        amount = options.order_size_usd / estimate.long_leg.price
        markets = [self._find_market(c, estimate.symbol) for c in (long_connector, short_connector)]
        minimum = max((m.min_amount for m in markets if m is not None and m.min_amount is not None and m.min_amount > ZERO), default=ZERO)
        step = _common_step([m.amount_step for m in markets if m is not None and m.amount_step is not None and m.amount_step > ZERO])

        amount = max(amount, minimum)
        if step is not None:
            floored = _round_to_step(amount, step, ROUND_FLOOR)
            amount = floored if floored > ZERO and floored >= minimum else _round_to_step(amount, step, ROUND_CEILING)

        price = max(estimate.long_leg.price, estimate.short_leg.price)
        required_usd = amount * price
        if required_usd > options.order_size_usd * 5:
            step_text = f", общий шаг лота {dec_str(step)}" if step is not None else ""
            self._log.warning(
                f"{estimate.symbol}: минимальный объём сделки на обеих биржах ({dec_str(amount)}{step_text}) "
                f"требует ~{formatting.usd(required_usd)} — пропуск"
            )
            return None

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
        # висящая заявка-мейкер — будущая позиция на обеих биржах
        for quote in self._maker_quotes.values():
            counts[quote.maker.id] = counts.get(quote.maker.id, 0) + 1
            counts[quote.hedge.id] = counts.get(quote.hedge.id, 0) + 1

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


def _streak_key(estimate: SpreadEstimate) -> _StreakKey:
    return estimate.symbol, estimate.long_leg.exchange_id, estimate.short_leg.exchange_id


def _ticker_age_ms(ticker: TickerSnapshot, now: datetime) -> float | None:
    """Возраст котировки, мс (None — биржа не дала время)."""
    return max(0.0, (now - ticker.timestamp).total_seconds() * 1000) if ticker.timestamp is not None else None


def _sweep(book: OrderBookSnapshot, side: OrderSide, amount: Decimal) -> Decimal | None:
    return book.sweep_price(side, amount)


def _common_step(steps: Sequence[Decimal]) -> Decimal | None:
    """Наименьший общий шаг лота (НОК десятичных шагов): объём, кратный ему, кратен шагу каждой биржи."""
    result: Decimal | None = None
    for raw in steps:
        step = raw.normalize()
        if step <= ZERO:
            continue
        if result is None:
            result = step
            continue
        exponent = max(0, -int(result.as_tuple().exponent), -int(step.as_tuple().exponent))
        scale = Decimal(10) ** exponent
        a = int((result * scale).to_integral_value())
        b = int((step * scale).to_integral_value())
        if a <= 0 or b <= 0:
            continue
        result = (Decimal(math.lcm(a, b)) / scale).normalize()
    return result


def _round_to_step(amount: Decimal, step: Decimal, rounding: str) -> Decimal:
    return (amount / step).to_integral_value(rounding=rounding) * step


async def _exchange_position_amount(connector: ExchangeConnector, symbol: str, side: OrderSide) -> tuple[bool, Decimal | None]:
    """
    Фактический объём позиции ноги на бирже (side — сторона входа: Buy — лонг).
    (False, None) — сверка не удалась; (True, None) — позиции нет; (True, объём) — есть.
    """
    try:
        positions = await connector.fetch_positions(_NONE)
    except OperationCanceledError:
        raise
    except Exception:
        return False, None
    amounts = [p.amount for p in positions if p.symbol == symbol and p.side == side and p.amount > ZERO]
    return True, (sum(amounts, ZERO) if amounts else None)


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


def _fees_of(connector: ExchangeConnector, symbol: str) -> ExchangeFees:
    found, fees = connector.try_get_fees(symbol)
    return fees if found else connector.default_fees


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
