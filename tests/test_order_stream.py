"""
Статусы заявок через WebSocket (watch_orders): кэш по id, страховочная перепроверка REST,
свежие чтения после отмены, пробуждение исполнителя по событию вместо опроса.
"""

import asyncio
import time
from datetime import UTC, datetime

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.clock import SystemClock
from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.core.domain.enums import MarketDataSource, NetworkMode, OrderSide, OrderStatus, OrderType
from futures_arb_bot.core.domain.market_data import OrderRequest, OrderResult, OrderUpdate
from futures_arb_bot.core.domain.options import ExchangeConfigEntry, OrdersOptions
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.trade_executor import ArbTradeExecutor
from futures_arb_bot.infrastructure import ccxt_connector
from futures_arb_bot.infrastructure.ccxt_connector import CcxtExchangeConnector, parse_order_update
from futures_arb_bot.infrastructure.order_stream import OrderStream

from .fakes import D, FakeConfigProvider, FakeConnector, ManualClock, make_log

BTC = "BTC/USDT:USDT"
NONE = CancellationToken.none()
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class OrdersApi:
    def __init__(self, ws_orders: bool = True, ws_create: bool = False) -> None:
        self.has = {"watchOrders": ws_orders, "createOrderWs": ws_create, "cancelOrderWs": ws_create}
        self.events: asyncio.Queue[list] = asyncio.Queue()
        self.fetch_calls = 0
        self.created_ws = 0
        self.created_rest = 0
        self.cancel_ws = 0
        self.state = {"id": "1", "status": "open", "filled": 0.0, "symbol": BTC}

    async def load_markets(self):
        market = {"swap": True, "linear": True, "active": True, "taker": 0.0005, "maker": 0.0002, "quote": "USDT"}
        return {BTC: market}

    def amount_to_precision(self, symbol, amount):
        return str(amount)

    def price_to_precision(self, symbol, price):
        return str(price)

    async def watch_orders(self, symbol=None, since=None, limit=None, params=None):
        return await self.events.get()

    async def fetch_order(self, order_id, symbol):
        self.fetch_calls += 1
        return dict(self.state)

    async def create_order(self, symbol, type_, side, amount, price, params):
        self.created_rest += 1
        return {"id": "1", "status": "open", "filled": 0.0, "price": price, "clientOrderId": params.get("clientOrderId")}

    async def create_order_ws(self, symbol, type_, side, amount, price, params):
        self.created_ws += 1
        return {"id": "1", "status": "open", "filled": 0.0, "price": price, "clientOrderId": params.get("clientOrderId")}

    async def cancel_order_ws(self, order_id, symbol):
        self.cancel_ws += 1

    async def cancel_order(self, order_id, symbol):
        pass

    async def close(self):
        pass


def make_connector(monkeypatch, api: OrdersApi, mode=NetworkMode.LIVE) -> CcxtExchangeConnector:
    monkeypatch.setattr(ccxt_connector, "instantiate", lambda _id, _cfg: api)
    return CcxtExchangeConnector(ExchangeConfigEntry(id="okx", name="OKX"), mode, make_log(), SystemClock())


async def test_status_comes_from_stream_without_rest_until_cache_ages(monkeypatch):
    api = OrdersApi()
    connector = make_connector(monkeypatch, api)
    await connector.connect()
    assert connector.start_order_stream(OrdersOptions(max_cache_age_ms=200))

    result = await connector.place_order(OrderRequest(BTC, OrderSide.BUY, D(1), type=OrderType.LIMIT, price=D(100)))
    assert result.success and result.order_id == "1"

    # исходное состояние — из ответа на заявку, без запроса
    first = await connector.fetch_order("1", BTC)
    assert first is not None and first.status == OrderStatus.OPEN and api.fetch_calls == 0

    # событие потока: исполнено
    await api.events.put([{"id": "1", "status": "closed", "filled": 1.0, "average": 100.0, "symbol": BTC}])
    assert await connector.wait_order_update(1.0)
    filled = await connector.fetch_order("1", BTC)
    assert filled is not None and filled.is_filled and filled.filled_amount == D(1) and api.fetch_calls == 0

    # fresh=True — всегда запрос к бирже
    await connector.fetch_order("1", BTC, fresh=True)
    assert api.fetch_calls == 1

    # статус без изменений дольше max_cache_age_ms — перепроверка REST
    await asyncio.sleep(0.25)
    await connector.fetch_order("1", BTC)
    assert api.fetch_calls == 2

    await connector.aclose()


async def test_rest_updates_mode_never_uses_stream(monkeypatch):
    api = OrdersApi()
    connector = make_connector(monkeypatch, api)
    await connector.connect()
    assert not connector.start_order_stream(OrdersOptions(updates=MarketDataSource.REST))
    await connector.place_order(OrderRequest(BTC, OrderSide.BUY, D(1), type=OrderType.LIMIT, price=D(100)))
    await connector.fetch_order("1", BTC)
    assert api.fetch_calls == 1
    await connector.aclose()


async def test_stream_wait_update_times_out_without_events():
    stream = OrderStream(OrdersApi(), "okx", "OKX", make_log(), lambda o: parse_order_update(o))
    stream.start()
    started = time.monotonic()
    assert not await stream.wait_update(0.05)
    assert time.monotonic() - started < 0.5
    await stream.aclose()


def test_parse_order_update_maps_status_fill_and_fee():
    update = parse_order_update({"id": 7, "status": "closed", "filled": 2.0, "average": 50.0, "fee": {"cost": 0.05, "currency": "USDT"}}, BTC)
    assert update == OrderUpdate("7", OrderStatus.FILLED, D(2), D(50), D("0.05"))


class EventConnector(FakeConnector):
    """Фейковая биржа, где «поток заявок» будит исполнителя сразу после выставления."""

    def __init__(self, exchange_id: str) -> None:
        super().__init__(exchange_id)
        self.wakeups = 0

    async def wait_order_update(self, timeout: float) -> bool:
        self.wakeups += 1
        await asyncio.sleep(0.01)
        return True


async def test_executor_reacts_to_order_events_instead_of_poll_interval():
    config = FakeConfigProvider()
    config.current.general.network_mode = NetworkMode.TESTNET
    config.current.arbitrage.enabled = True
    config.current.arbitrage.order_size_usd = D(100)
    config.current.arbitrage.order_execution_timeout_ms = 60_000
    config.current.arbitrage.order_poll_interval_ms = 5_000  # опросом ждали бы по 5 с

    long_c, short_c = EventConnector("binanceusdm"), EventConnector("bybit")
    polls = {"n": 0}
    amounts: dict[str, object] = {}

    def place(request, index):
        amounts[f"o{index}"] = request.amount
        return OrderResult.ok(f"o{index}", request.price, D(0))

    long_c.place_order_override = place
    original_fetch = long_c.fetch_order

    async def fetch(order_id, symbol, ct=None, fresh=False):
        polls["n"] += 1
        if polls["n"] >= 2:  # «поток» сообщил: исполнено
            long_c.order_states[order_id] = OrderUpdate(order_id, OrderStatus.FILLED, amounts[order_id], D(100))
        return await original_fetch(order_id, symbol, ct, fresh)

    long_c.fetch_order = fetch  # type: ignore[method-assign]

    registry = ConnectorRegistry()
    await registry.replace([long_c, short_c])
    clock = ManualClock(NOW)
    executor = ArbTradeExecutor(config, registry, DefaultStatisticsCollector(config, clock), make_log(config, clock), clock)
    estimate = SpreadEstimate(
        BTC, OpportunityLeg("binanceusdm", D(100), D("0.1")), OpportunityLeg("bybit", D(101), D("0.1")), D(1), D(1), D(10**7), NOW
    )

    started = time.monotonic()
    await executor.process_opportunities([estimate], NONE)

    assert executor.has_open_positions
    assert time.monotonic() - started < 1.0  # не ждали 5-секундного интервала опроса
    assert long_c.wakeups >= 1


async def test_final_checks_after_cancel_are_fresh_reads():
    config = FakeConfigProvider()
    config.current.general.network_mode = NetworkMode.TESTNET
    config.current.arbitrage.enabled = True
    config.current.arbitrage.order_size_usd = D(100)
    config.current.arbitrage.order_execution_timeout_ms = 100
    config.current.arbitrage.order_poll_interval_ms = 20
    long_c, short_c = FakeConnector("binanceusdm"), FakeConnector("bybit")
    long_c.place_order_override = lambda r, i: OrderResult.ok(f"r{i}", None, r.amount) if r.reduce_only else OrderResult.ok(f"o{i}", r.price, D(0))
    short_c.place_order_override = lambda r, i: OrderResult.ok(f"o{i}", r.price, D(0))
    registry = ConnectorRegistry()
    await registry.replace([long_c, short_c])
    clock = ManualClock(NOW)
    executor = ArbTradeExecutor(config, registry, DefaultStatisticsCollector(config, clock), make_log(config, clock), clock)
    estimate = SpreadEstimate(
        BTC, OpportunityLeg("binanceusdm", D(100), D("0.1")), OpportunityLeg("bybit", D(101), D("0.1")), D(1), D(1), D(10**7), NOW
    )

    # часы стоят — таймаут не наступит сам; двигаем их фоном
    async def tick():
        for _ in range(50):
            await asyncio.sleep(0.01)
            clock.advance(__import__("datetime").timedelta(milliseconds=50))

    ticker_task = asyncio.create_task(tick())
    await executor.process_opportunities([estimate], NONE)
    await ticker_task

    assert not executor.has_open_positions
    # опрос во время ожидания — обычный, перечитывание после отмены — свежее
    assert any(not fresh for _, fresh in long_c.fetch_calls)
    assert any(fresh for _, fresh in long_c.fetch_calls)


async def test_orders_go_through_websocket_when_exchange_supports_it(monkeypatch):
    api = OrdersApi(ws_create=True)
    connector = make_connector(monkeypatch, api)
    await connector.connect()
    connector.update_orders(OrdersOptions())
    result = await connector.place_order(OrderRequest(BTC, OrderSide.BUY, D(1), type=OrderType.LIMIT, price=D(100)))
    assert result.success and api.created_ws == 1 and api.created_rest == 0
    await connector.cancel_order("1", BTC)
    assert api.cancel_ws == 1
    await connector.aclose()


async def test_rest_transport_keeps_orders_on_rest(monkeypatch):
    api = OrdersApi(ws_create=True)
    connector = make_connector(monkeypatch, api)
    await connector.connect()
    connector.update_orders(OrdersOptions(transport=MarketDataSource.REST))
    await connector.place_order(OrderRequest(BTC, OrderSide.BUY, D(1), type=OrderType.LIMIT, price=D(100)))
    await connector.cancel_order("1", BTC)
    assert api.created_rest == 1 and api.created_ws == 0 and api.cancel_ws == 0
    await connector.aclose()
