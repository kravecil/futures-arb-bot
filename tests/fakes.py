"""Тестовые двойники: конфигурация без файлов, фейковые биржи, транспорт, исполнитель, часы."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.domain.arbitrage import SpreadEstimate
from futures_arb_bot.core.domain.enums import CloseReason, NetworkMode, OrderStatus
from futures_arb_bot.core.domain.market_data import (
    ExchangeFees,
    FetchTickersResult,
    MarketInfo,
    OrderBookSnapshot,
    OrderRequest,
    OrderResult,
    OrderUpdate,
    PositionSnapshot,
    TickerSnapshot,
)
from futures_arb_bot.core.domain.notifications import NotificationDelivery
from futures_arb_bot.core.domain.options import BotOptions
from futures_arb_bot.core.engine.event_log import InMemoryEventLog

D = Decimal
BASE_TIME = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


class FakeConfigProvider:
    """Тестовый конфиг-провайдер без файловой системы (одни и те же настройки на каждый доступ)."""

    def __init__(self, options: BotOptions | None = None) -> None:
        if options is None:
            options = BotOptions()
            # исторические тесты проверяют решение одного скана; подтверждение сканами — в test_entry_confirmation
            options.arbitrage.confirm_scans = 1
            options.arbitrage.confirm_order_book = False
        self._options = options

    @property
    def current(self) -> BotOptions:
        return self._options

    @property
    def config_directory(self) -> str:
        return "test"


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class ManualClock:
    """Часы, которые тест сдвигает вручную (кулдаун, окно лимита в минуту)."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, by: timedelta) -> None:
        self._now += by


class SteppingClock:
    """Часы, шагающие вперёд при каждом чтении — чтобы циклы ожидания не зависали."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self._start = start
        self._step = step
        self._reads = 0

    def now(self) -> datetime:
        n = self._reads
        self._reads += 1
        return self._start + self._step * n


def make_log(config: FakeConfigProvider | None = None, clock: object | None = None) -> InMemoryEventLog:
    config = config or FakeConfigProvider()
    config.current.general.write_log_file = False
    return InMemoryEventLog(config, clock or FixedClock(BASE_TIME))  # type: ignore[arg-type]


def ticker(exchange: str, symbol: str, bid: Decimal, ask: Decimal, volume: Decimal = D(10_000_000), ts: datetime | None = None) -> TickerSnapshot:
    return TickerSnapshot(exchange, symbol, bid, ask, (bid + ask) / 2, volume, ts if ts is not None else BASE_TIME)


class FakeConnector:
    """Фейковая биржа с настраиваемыми котировками и комиссиями."""

    def __init__(self, exchange_id: str, taker_percent: Decimal = D("0.1")) -> None:
        self._id = exchange_id
        self._display_name = f"Fake {exchange_id}"
        self._markets = [
            MarketInfo("BTC/USDT:USDT", True, True, "USDT", D("0.0001"), taker_percent, D("0.02")),
            MarketInfo("ETH/USDT:USDT", True, True, "USDT", D("0.001"), taker_percent, D("0.02")),
        ]
        self._fees = ExchangeFees(taker_percent, D("0.02"))
        self.tickers: dict[str, TickerSnapshot] = {}
        self.connected = False
        self.funding_rates_percent: dict[str, Decimal] = {}
        self.fail_fetch_funding_rates = False
        self.placed_orders: dict[str, OrderRequest] = {}
        self.order_states: dict[str, OrderUpdate] = {}
        self.place_order_override: Callable[[OrderRequest, int], OrderResult | None] | None = None
        self.limit_partially_fills = False
        self.cancelled_orders: list[str] = []
        self.cancel_order_override: Callable[[str], bool] | None = None
        self.open_positions: list[PositionSnapshot] = []
        self.fail_fetch_positions = False
        self._order_counter = 0
        self.streaming_supported = False
        self.subscribed: set[str] = set()
        self.market_data = None
        self.orders = None
        self.fetch_calls: list[tuple[str, bool]] = []
        self.order_books: dict[str, OrderBookSnapshot | None] = {}
        """Стакан по символу; не задан — строится из тикера с большой глубиной."""
        self.order_book_calls: list[str] = []

    @property
    def id(self) -> str:
        return self._id

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def mode(self) -> NetworkMode:
        return NetworkMode.DRY_RUN

    @property
    def default_fees(self) -> ExchangeFees:
        return self._fees

    @property
    def market_count(self) -> int:
        return len(self._markets)

    @property
    def perpetual_markets(self) -> list[MarketInfo]:
        return self._markets

    @property
    def supports_streaming(self) -> bool:
        return self.streaming_supported

    @property
    def streamed_symbols(self) -> set[str]:
        return set(self.subscribed)

    @property
    def stream_messages(self) -> int:
        return 0

    @property
    def rest_tickers(self) -> Mapping[str, TickerSnapshot]:
        return self.tickers

    def update_market_data(self, market_data, refresh_interval_ms: int) -> None:  # type: ignore[no-untyped-def]
        self.market_data = market_data

    def subscribe_quotes(self, symbols) -> list[str]:  # type: ignore[no-untyped-def]
        if not self.streaming_supported:
            return []
        fresh = sorted(s for s in symbols if s not in self.subscribed)
        self.subscribed.update(fresh)
        return fresh

    async def connect(self, ct: CancellationToken | None = None) -> None:
        self.connected = True

    async def fetch_tickers(self, ct: CancellationToken | None = None) -> FetchTickersResult:
        return FetchTickersResult(self.tickers, timedelta(milliseconds=12))

    async def fetch_funding_rates_percent(self, ct: CancellationToken | None = None) -> Mapping[str, Decimal]:
        if self.fail_fetch_funding_rates:
            raise RuntimeError("фандинг-рейты недоступны")
        return self.funding_rates_percent

    async def place_order(self, request: OrderRequest, ct: CancellationToken | None = None) -> OrderResult:
        self._order_counter += 1
        index = self._order_counter
        order_id = f"fake-order-{index}"
        self.placed_orders[order_id] = request

        result = self.place_order_override(request, index) if self.place_order_override is not None else None
        if result is None:
            result = OrderResult.ok(order_id, request.price, request.amount)

        if result.success and result.order_id is not None:
            fully_filled = result.filled_amount >= request.amount
            self.order_states[result.order_id] = OrderUpdate(
                result.order_id, OrderStatus.FILLED if fully_filled else OrderStatus.OPEN, result.filled_amount, result.average_price
            )
        return result

    def update_orders(self, orders) -> None:  # type: ignore[no-untyped-def]
        self.orders = orders

    def start_order_stream(self, orders) -> bool:  # type: ignore[no-untyped-def]
        return False

    async def wait_order_update(self, timeout: float) -> bool:
        if timeout > 0:
            await asyncio.sleep(timeout)
        return False

    async def fetch_order(self, order_id: str, symbol: str, ct: CancellationToken | None = None, fresh: bool = False) -> OrderUpdate | None:
        self.fetch_calls.append((order_id, fresh))
        state = self.order_states.get(order_id)
        if state is None:
            return None

        request = self.placed_orders.get(order_id)
        if state.status == OrderStatus.OPEN and self.limit_partially_fills and request is not None:
            state = OrderUpdate(order_id, OrderStatus.OPEN, request.amount / 2, request.price)
            self.order_states[order_id] = state
        return state

    async def fetch_positions(self, ct: CancellationToken | None = None) -> list[PositionSnapshot]:
        if self.fail_fetch_positions:
            raise RuntimeError("сверка позиций недоступна")
        return list(self.open_positions)

    async def fetch_order_book(self, symbol: str, depth: int, ct: CancellationToken | None = None) -> OrderBookSnapshot | None:
        self.order_book_calls.append(symbol)
        if symbol in self.order_books:
            return self.order_books[symbol]
        quote = self.tickers.get(symbol)
        if quote is None:
            return None
        deep = D(1_000_000_000)
        return OrderBookSnapshot(self._id, symbol, ((quote.bid, deep),), ((quote.ask, deep),), quote.timestamp)

    async def cancel_order(self, order_id: str, symbol: str, ct: CancellationToken | None = None) -> bool:
        if self.cancel_order_override is not None and self.cancel_order_override(order_id) is False:
            return False

        state = self.order_states.get(order_id)
        if state is None or state.status != OrderStatus.OPEN:
            return False

        self.order_states[order_id] = replace(state, status=OrderStatus.CANCELED)
        self.cancelled_orders.append(order_id)
        return True

    async def set_leverage(self, leverage: int, symbol: str, ct: CancellationToken | None = None) -> None:
        return None

    async def verify_access(self, ct: CancellationToken | None = None) -> bool:
        return True

    def try_get_fees(self, symbol: str) -> tuple[bool, ExchangeFees]:
        return True, self._fees

    async def aclose(self) -> None:
        return None


class FakeNotifier:
    """Отправитель-регистратор: собирает сигналы, переданные в очередь уведомлений."""

    def __init__(self) -> None:
        self.notified: list[SpreadEstimate] = []

    def notify(self, estimate: SpreadEstimate) -> None:
        self.notified.append(estimate)


class FakeTransport:
    """Вместо HTTP записывает тексты и отдаёт заготовленные исходы (последний повторяется, None = успех)."""

    def __init__(self) -> None:
        self.outcomes: list[NotificationDelivery | None] = []
        self.throw_on_send: Exception | None = None
        self.delay: timedelta = timedelta(0)
        self._sent: list[str] = []
        self._calls = 0

    @property
    def sent(self) -> list[str]:
        return list(self._sent)

    @property
    def call_count(self) -> int:
        return self._calls

    async def send(self, text: str, ct: CancellationToken | None = None) -> NotificationDelivery:
        index = self._calls
        self._calls += 1
        self._sent.append(text)

        if self.throw_on_send is not None:
            raise self.throw_on_send

        if self.delay > timedelta(0):
            await asyncio.sleep(self.delay.total_seconds())

        if not self.outcomes:
            return NotificationDelivery.delivered()
        outcome = self.outcomes[min(index, len(self.outcomes) - 1)]
        return outcome if outcome is not None else NotificationDelivery.delivered()


class FakeExecutor:
    """Исполнитель-регистратор: вместо торговли фиксирует полученные кандидаты."""

    def __init__(self) -> None:
        self.processed_batches: list[list[SpreadEstimate]] = []

    @property
    def processed(self) -> list[SpreadEstimate]:
        return [e for batch in self.processed_batches for e in batch]

    @property
    def has_open_positions(self) -> bool:
        return False

    async def process_opportunities(self, candidates: Sequence[SpreadEstimate], ct: CancellationToken) -> None:
        if candidates:
            self.processed_batches.append(list(candidates))

    async def manage_open_positions(self, tickers_by_exchange: Mapping[str, Mapping[str, TickerSnapshot]], ct: CancellationToken) -> None:
        return None

    async def close_all(self, reason: CloseReason, ct: CancellationToken) -> None:
        return None
