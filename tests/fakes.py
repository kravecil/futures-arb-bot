"""Тестовые двойники — порт Fakes.cs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from futures_arb_bot.domain.arbitrage import SpreadEstimate
from futures_arb_bot.domain.enums import NetworkMode, OrderStatus
from futures_arb_bot.domain.market_data import (
    ExchangeFees,
    FetchTickersResult,
    MarketInfo,
    OrderRequest,
    OrderResult,
    OrderUpdate,
    PositionSnapshot,
    TickerSnapshot,
)
from futures_arb_bot.domain.options import BotOptions

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
TICKER_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


class FakeConfigProvider:
    """Тестовый конфиг-провайдер без файловой системы."""

    def __init__(self, options: BotOptions | None = None) -> None:
        self.Current = options or BotOptions()
        self.Current.General.WriteLogFile = False  # тесты не пишут файлы журнала
        self.ConfigDirectory = "test"


def make_ticker(
    exchange: str,
    symbol: str,
    bid: Decimal,
    ask: Decimal,
    volume: Decimal = Decimal("10000000"),
    ts: datetime | None = None,
) -> TickerSnapshot:
    """Тикер-строитель для тестов."""

    return TickerSnapshot(
        ExchangeId=exchange,
        Symbol=symbol,
        Bid=Decimal(bid),
        Ask=Decimal(ask),
        Last=(Decimal(bid) + Decimal(ask)) / 2,
        QuoteVolume24h=Decimal(volume),
        Timestamp=ts or TICKER_TS,
    )


class FakeConnector:
    """Фейковая биржа с настраиваемыми котировками, ордерами и комиссиями."""

    def __init__(self, exchange_id: str, taker_percent: Decimal = Decimal("0.1")) -> None:
        self.Id = exchange_id
        self.DisplayName = f"Fake {exchange_id}"
        self.Mode = NetworkMode.DryRun
        taker = Decimal(str(taker_percent))
        self._markets = [
            MarketInfo("BTC/USDT:USDT", True, True, "USDT", Decimal("0.0001"), taker, Decimal("0.02")),
            MarketInfo("ETH/USDT:USDT", True, True, "USDT", Decimal("0.001"), taker, Decimal("0.02")),
        ]
        self._fees = ExchangeFees(taker, Decimal("0.02"))
        self.DefaultFees = self._fees
        self.Tickers: dict[str, TickerSnapshot] = {}
        self.Connected = False
        # Фандинг-рейты «биржи»: symbol → ставка в % (пусто — данных нет).
        self.FundingRatesPercent: dict[str, Decimal] = {}
        self.FailFetchFundingRates = False
        # Все ордера, выставленные через фейк: orderId → запрос.
        self.PlacedOrders: dict[str, OrderRequest] = {}
        # Состояния фейковых ордеров: orderId → снимок (тесты могут менять до опроса).
        self.OrderStates: dict[str, OrderUpdate] = {}
        # Переопределение поведения размещения: вернуть Fail/Ok — тестовая проводка сбоев;
        # None — дефолт (полное исполнение).
        self.PlaceOrderOverride = None
        # Если True — фейковый limit исполняется «наполовину» при первом опросе статуса.
        self.LimitPartiallyFills = False
        # Отменённые через фейк заявки (orderId).
        self.CancelledOrders: list[str] = []
        # Перекрыть поведение отмены: False — отмена «не проходит», заявка остаётся в стакане.
        self.CancelOrderOverride = None
        # Фактические позиции «биржи» для тестов сверки лимитов (пусто — внешних позиций нет).
        self.OpenPositions: list[PositionSnapshot] = []
        self.FailFetchPositions = False
        self._order_counter = 0

    @property
    def MarketCount(self) -> int:
        return len(self._markets)

    @property
    def PerpetualMarkets(self) -> list[MarketInfo]:
        return self._markets

    async def ConnectAsync(self, ct=None) -> None:
        self.Connected = True

    async def FetchTickersAsync(self, ct=None) -> FetchTickersResult:
        return FetchTickersResult(self.Tickers, timedelta(milliseconds=12))

    async def FetchFundingRatesPercentAsync(self, ct=None) -> dict[str, Decimal]:
        if self.FailFetchFundingRates:
            raise RuntimeError("фандинг-рейты недоступны")
        return self.FundingRatesPercent

    async def PlaceOrderAsync(self, request: OrderRequest, ct=None) -> OrderResult:
        self._order_counter += 1
        index = self._order_counter
        order_id = f"fake-order-{index}"
        self.PlacedOrders[order_id] = request

        override_result = self.PlaceOrderOverride(request, index) if self.PlaceOrderOverride is not None else None
        # null из оверрайда = дефолтное поведение фейка (полное исполнение), как в C#
        result = override_result or OrderResult.Ok(order_id, request.Price, request.Amount)

        if result.Success and result.OrderId is not None:
            fully_filled = result.FilledAmount >= request.Amount
            status = OrderStatus.Filled if fully_filled else OrderStatus.Open
            self.OrderStates[result.OrderId] = OrderUpdate(result.OrderId, status, result.FilledAmount, result.AveragePrice)

        return result

    async def FetchOrderAsync(self, order_id: str, symbol: str, ct=None) -> OrderUpdate | None:
        state = self.OrderStates.get(order_id)
        if state is None:
            return None

        if state.Status == OrderStatus.Open and self.LimitPartiallyFills:
            request = self.PlacedOrders.get(order_id)
            if request is not None:
                half = request.Amount / 2
                state = OrderUpdate(order_id, OrderStatus.Open, half, request.Price)
                self.OrderStates[order_id] = state

        return state

    async def CancelOrderAsync(self, order_id: str, symbol: str, ct=None) -> bool:
        if self.CancelOrderOverride is not None and not self.CancelOrderOverride(order_id):
            return False

        state = self.OrderStates.get(order_id)
        if state is None or state.Status != OrderStatus.Open:
            return False

        self.OrderStates[order_id] = OrderUpdate(order_id, OrderStatus.Canceled, state.FilledAmount, state.AveragePrice)
        self.CancelledOrders.append(order_id)
        return True

    async def FetchPositionsAsync(self, ct=None) -> list[PositionSnapshot]:
        if self.FailFetchPositions:
            raise RuntimeError("сверка позиций недоступна")
        return list(self.OpenPositions)

    async def SetLeverageAsync(self, leverage: int, symbol: str, ct=None) -> None:
        return None

    async def VerifyAccessAsync(self, ct=None) -> bool:
        return True

    def TryGetFees(self, symbol: str) -> ExchangeFees | None:
        return self._fees

    async def Aclose(self) -> None:
        return None


class FakeNotifier:
    """Отправитель-регистратор: собирает сигналы, переданные в очередь уведомлений."""

    def __init__(self) -> None:
        self.Notified: list[SpreadEstimate] = []

    def Notify(self, estimate: SpreadEstimate) -> None:
        self.Notified.append(estimate)


class FakeExecutor:
    """Исполнитель-регистратор: вместо торговли фиксирует полученные кандидаты."""

    def __init__(self) -> None:
        self.ProcessedBatches: list[list[SpreadEstimate]] = []
        self.HasOpenPositions = False

    @property
    def Processed(self) -> list[SpreadEstimate]:
        return [item for batch in self.ProcessedBatches for item in batch]

    async def ProcessOpportunitiesAsync(self, candidates, ct=None) -> None:
        self.ProcessedBatches.append(list(candidates))

    async def ManageOpenPositionsAsync(self, tickers_by_exchange, ct=None) -> None:
        return None

    async def CloseAllAsync(self, reason, ct=None) -> None:
        return None


class FakeTransport:
    """
    Транспорт уведомлений: вместо HTTP записывает тексты и отдаёт заготовленные исходы.
    i-я отправка получает Outcomes[i] (последний элемент повторяется), None = успешно.
    """

    def __init__(self) -> None:
        self.Sent: list[str] = []
        # Исходы по порядку вызовов; None в списке = доставка успешна.
        self.Outcomes: list = []
        self.ThrowOnSend = None
        self.DelaySeconds = 0.0
        self.CallCount = 0

    async def SendAsync(self, text: str, ct=None):
        import asyncio

        from futures_arb_bot.domain.delivery import NotificationDelivery

        index = self.CallCount
        self.CallCount += 1
        self.Sent.append(text)

        if self.ThrowOnSend is not None:
            raise self.ThrowOnSend

        if self.DelaySeconds > 0:
            await asyncio.sleep(self.DelaySeconds)

        if not self.Outcomes:
            return NotificationDelivery.Delivered

        return self.Outcomes[min(index, len(self.Outcomes) - 1)] or NotificationDelivery.Delivered

    async def aclose(self) -> None:
        return None


class FakeTime:
    """Часы, которые тест сдвигает вручную (кулдаун, окно лимита в минуту)."""

    def __init__(self, start: datetime = NOW) -> None:
        self._now = start

    def GetUtcNow(self) -> datetime:
        return self._now

    def Advance(self, by: timedelta) -> None:
        self._now = self._now + by


class SteppingTime(FakeTime):
    """Часы, шагающие вперёд при каждом чтении — чтобы циклы ожидания не зависали."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        super().__init__(start)
        self._step = step
        self._reads = 0

    def GetUtcNow(self) -> datetime:
        value = self._now + self._step * self._reads
        self._reads += 1
        return value
