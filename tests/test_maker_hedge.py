"""
Вход «maker + хедж»: расчёт цены заявки-мейкера, выставление post-only, перестановка за котировкой
хедж-биржи, снятие по пропаже сигнала, хедж по факту исполнения, откат при неудачном хедже, DryRun.
"""

from datetime import timedelta
from decimal import Decimal

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.core.domain.enums import MakerLeg, NetworkMode, OrderSide, OrderStatus, OrderType, TimeInForce
from futures_arb_bot.core.domain.market_data import ExchangeFees, OrderResult, OrderUpdate
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.maker_hedge import plan_long_maker, plan_quote, plan_short_maker, simulated_fill
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.trade_executor import ArbTradeExecutor

from .fakes import BASE_TIME, D, FakeConfigProvider, FakeConnector, ManualClock, make_log, ticker

NONE = CancellationToken.none()
BTC = "BTC/USDT:USDT"
FEES = ExchangeFees(D("0.05"), D("0.02"))


# ------------------------- расчёт цены -------------------------


def test_short_maker_price_covers_target_fees_and_buffer():
    cheap = ticker("okx", BTC, D("99.9"), D(100))
    rich = ticker("bybit", BTC, D("100.2"), D("100.25"))
    plan = plan_short_maker(cheap, rich, FEES, FEES, D("0.4"), D("0.03"), include_fees=True)
    assert plan is not None
    # комиссии круга 0.02 + 0.05 + 0.05 + 0.05 = 0.17; нужно брутто 0.4 + 0.17 + 0.03 = 0.6 % от ask дешёвой
    assert plan.price == D("100.6")
    assert plan.maker_side == OrderSide.SELL
    assert plan.hedge_side == OrderSide.BUY
    assert plan.maker_exchange_id == "bybit" and plan.hedge_exchange_id == "okx"
    assert plan.expected_net_percent == D("0.4")


def test_maker_price_never_crosses_own_book():
    # спред уже больше нужного: заявка встаёт на лучший ask, а не ниже (post-only отклонился бы)
    cheap = ticker("okx", BTC, D("99.9"), D(100))
    rich = ticker("bybit", BTC, D("101.0"), D("101.1"))
    plan = plan_short_maker(cheap, rich, FEES, FEES, D("0.4"), D("0.03"), include_fees=True)
    assert plan is not None
    assert plan.price == D("101.1")
    assert plan.distance_bps == 0
    assert plan.expected_net_percent > D("0.4")

    long_plan = plan_long_maker(cheap, rich, FEES, FEES, D("0.4"), D("0.03"), include_fees=True)
    assert long_plan is not None
    assert long_plan.price == D("99.9")  # покупка не выше своего bid


def test_auto_picks_leg_closer_to_its_book_and_respects_allowed_exchanges():
    cheap = ticker("okx", BTC, D("99.9"), D(100))
    rich = ticker("bybit", BTC, D("100.2"), D("100.25"))
    common = dict(target_net=D("0.4"), buffer=D("0.03"), include_fees=True)

    auto = plan_quote(cheap, rich, FEES, FEES, leg=MakerLeg.AUTO, allowed_makers={"okx", "bybit"}, **common)
    assert auto is not None and auto.maker_side == OrderSide.BUY  # покупка на okx ближе к bid (≈30 bps против ≈35)

    only_bybit = plan_quote(cheap, rich, FEES, FEES, leg=MakerLeg.AUTO, allowed_makers={"bybit"}, **common)
    assert only_bybit is not None and only_bybit.maker_exchange_id == "bybit"

    assert plan_quote(cheap, rich, FEES, FEES, leg=MakerLeg.LONG, allowed_makers={"bybit"}, **common) is None


def test_simulated_fill_requires_market_to_reach_quote():
    assert not simulated_fill(OrderSide.SELL, D("100.6"), ticker("bybit", BTC, D("100.5"), D("100.55")))
    assert simulated_fill(OrderSide.SELL, D("100.6"), ticker("bybit", BTC, D("100.6"), D("100.65")))
    assert simulated_fill(OrderSide.BUY, D("99.6"), ticker("okx", BTC, D("99.5"), D("99.6")))


# ------------------------- исполнитель -------------------------


class Harness:
    def __init__(self, mode: NetworkMode = NetworkMode.TESTNET) -> None:
        self.config = FakeConfigProvider()
        arb = self.config.current.arbitrage
        self.config.current.general.network_mode = mode
        arb.enabled = True
        arb.order_size_usd = D(100)
        arb.min_spread_percent_up = D("0.4")
        arb.slippage_buffer_percent = D("0.03")
        arb.maker_hedge.enabled = True
        arb.maker_hedge.trigger_gross_percent = D("0.1")
        arb.maker_hedge.maker_leg = MakerLeg.SHORT
        self.cheap = FakeConnector("okx", taker_percent=D("0.05"))
        self.rich = FakeConnector("bybit", taker_percent=D("0.05"))
        self.clock = ManualClock(BASE_TIME)
        self.stats = DefaultStatisticsCollector(self.config, self.clock)
        self.log = make_log(self.config, self.clock)
        self.registry = ConnectorRegistry()
        self.executor: ArbTradeExecutor | None = None
        # заявки-мейкеры (post-only) встают в стакан неисполненными, остальное исполняется сразу
        for connector in (self.cheap, self.rich):
            connector.place_order_override = _resting_post_only
        self.set_quotes(cheap_ask=D(100), rich_bid=D("100.2"))

    async def start(self) -> ArbTradeExecutor:
        await self.registry.replace([self.cheap, self.rich])
        self.executor = ArbTradeExecutor(self.config, self.registry, self.stats, self.log, self.clock)
        return self.executor

    def set_quotes(self, cheap_ask: Decimal, rich_bid: Decimal, rich_ask: Decimal | None = None) -> None:
        now = self.clock.now()
        self.cheap.tickers = {BTC: ticker("okx", BTC, cheap_ask - D("0.1"), cheap_ask, ts=now)}
        self.rich.tickers = {BTC: ticker("bybit", BTC, rich_bid, rich_ask if rich_ask is not None else rich_bid + D("0.05"), ts=now)}

    def signal(self) -> SpreadEstimate:
        cheap = self.cheap.tickers[BTC]
        rich = self.rich.tickers[BTC]
        gross = (rich.bid - cheap.ask) / cheap.ask * 100
        return SpreadEstimate(
            BTC, OpportunityLeg("okx", cheap.ask, D("0.05")), OpportunityLeg("bybit", rich.bid, D("0.05")), gross, D(0), D(0), self.clock.now()
        )

    async def tick(self, with_signal: bool = True) -> None:
        assert self.executor is not None
        tickers = {"okx": self.cheap.tickers, "bybit": self.rich.tickers}
        await self.executor.process_maker_quotes([self.signal()] if with_signal else [], tickers, NONE)

    def maker_orders(self):
        return [(oid, r) for oid, r in self.rich.placed_orders.items() if r.time_in_force == TimeInForce.POST_ONLY]

    def fill(self, connector: FakeConnector, order_id: str, amount: Decimal) -> None:
        request = connector.placed_orders[order_id]
        status = OrderStatus.FILLED if amount >= request.amount else OrderStatus.OPEN
        connector.order_states[order_id] = OrderUpdate(order_id, status, amount, request.price)


def _resting_post_only(request, index):  # type: ignore[no-untyped-def]
    if request.time_in_force == TimeInForce.POST_ONLY:
        return OrderResult.ok(f"fake-order-{index}", request.price, D(0))
    return None


async def test_quote_is_post_only_limit_on_maker_exchange_only():
    h = Harness()
    executor = await h.start()

    await h.tick()

    assert executor.has_pending_entries
    assert not executor.has_open_positions
    assert h.cheap.placed_orders == {}  # хедж-нога не выставляется заранее
    ((_, order),) = h.maker_orders()
    assert order.side == OrderSide.SELL
    assert order.type == OrderType.LIMIT
    assert order.price == D("100.6")
    assert order.amount == D(1)


async def test_fill_triggers_immediate_ioc_hedge_and_registers_position():
    h = Harness()
    executor = await h.start()
    await h.tick()
    ((order_id, _),) = h.maker_orders()

    h.fill(h.rich, order_id, D(1))
    await h.tick()

    assert not executor.has_pending_entries
    assert executor.has_open_positions
    (hedge,) = h.cheap.placed_orders.values()
    assert hedge.side == OrderSide.BUY
    assert hedge.type == OrderType.LIMIT and hedge.time_in_force == TimeInForce.IOC
    assert hedge.amount == D(1)
    assert hedge.price == D("100.3")  # ask 100 + 30 bps
    position = executor._positions[BTC]
    assert position.long_exchange_id == "okx" and position.short_exchange_id == "bybit"
    assert position.entry_short == D("100.6")
    assert position.long_size == position.short_size == D(1)


async def test_partial_fill_cancels_rest_and_hedges_filled_part():
    h = Harness()
    executor = await h.start()
    await h.tick()
    ((order_id, _),) = h.maker_orders()

    h.fill(h.rich, order_id, D("0.4"))
    await h.tick()

    assert order_id in h.rich.cancelled_orders
    (hedge,) = h.cheap.placed_orders.values()
    assert hedge.amount == D("0.4")
    position = executor._positions[BTC]
    assert position.short_size == D("0.4") and position.long_size == D("0.4")


async def test_quote_withdrawn_when_signal_gone_longer_than_cancel_after():
    h = Harness()
    executor = await h.start()
    await h.tick()
    ((order_id, _),) = h.maker_orders()

    h.clock.advance(timedelta(milliseconds=1500))
    h.set_quotes(cheap_ask=D(100), rich_bid=D("100.2"))
    await h.tick(with_signal=False)
    assert executor.has_pending_entries  # 1.5 с < cancel_after_ms = 2 с

    h.clock.advance(timedelta(milliseconds=600))
    h.set_quotes(cheap_ask=D(100), rich_bid=D("100.2"))
    await h.tick(with_signal=False)

    assert not executor.has_pending_entries
    assert order_id in h.rich.cancelled_orders
    assert h.cheap.placed_orders == {}
    assert not executor.has_open_positions


async def test_quote_follows_hedge_price():
    h = Harness()
    executor = await h.start()
    await h.tick()
    ((first_id, _),) = h.maker_orders()

    # дешёвая биржа подорожала на 0.2 % — цена мейкера должна уйти выше
    h.clock.advance(timedelta(milliseconds=400))
    h.set_quotes(cheap_ask=D("100.2"), rich_bid=D("100.35"))
    await h.tick()

    assert first_id in h.rich.cancelled_orders
    orders = h.maker_orders()
    assert len(orders) == 2
    assert orders[-1][1].price == D("100.8012")  # 100.2 × 1.006
    assert executor.has_pending_entries


async def test_small_price_move_does_not_requote():
    h = Harness()
    await h.start()
    await h.tick()

    h.clock.advance(timedelta(milliseconds=400))
    h.set_quotes(cheap_ask=D("100.02"), rich_bid=D("100.2"))  # 2 bps < requote_threshold_bps = 5
    await h.tick()

    assert len(h.maker_orders()) == 1
    assert h.rich.cancelled_orders == []


async def test_failed_hedge_rolls_back_maker_leg():
    h = Harness()
    executor = await h.start()
    h.config.current.arbitrage.maker_hedge.hedge_fallback_to_market = False
    await h.tick()
    ((order_id, _),) = h.maker_orders()
    h.cheap.place_order_override = lambda _r, _i: OrderResult.fail("нет маржи")

    h.fill(h.rich, order_id, D(1))
    await h.tick()

    assert not executor.has_open_positions
    rollback = list(h.rich.placed_orders.values())[-1]
    assert rollback.reduce_only and rollback.side == OrderSide.BUY and rollback.type == OrderType.MARKET
    assert rollback.amount == D(1)
    report = h.stats.snapshot(BASE_TIME + timedelta(minutes=1))
    assert report.orders_failed == 1


async def test_ioc_shortfall_is_completed_by_market():
    h = Harness()
    executor = await h.start()
    await h.tick()
    ((order_id, _),) = h.maker_orders()
    h.cheap.place_order_override = lambda request, index: (
        OrderResult.ok(f"fake-order-{index}", request.price, D("0.3")) if request.time_in_force == TimeInForce.IOC else None
    )
    original_fetch = h.cheap.fetch_order

    async def ioc_expired(order_id_, symbol, ct=None, fresh=False):  # type: ignore[no-untyped-def]
        state = await original_fetch(order_id_, symbol, ct, fresh)
        return OrderUpdate(order_id_, OrderStatus.EXPIRED, state.filled_amount, state.average_price) if state is not None else None

    h.cheap.fetch_order = ioc_expired  # type: ignore[method-assign]

    h.fill(h.rich, order_id, D(1))
    await h.tick()

    ioc, market = h.cheap.placed_orders.values()
    assert ioc.time_in_force == TimeInForce.IOC
    assert market.type == OrderType.MARKET and market.amount == D("0.7")
    assert executor._positions[BTC].long_size == D(1)


async def test_taker_entry_skips_symbol_with_quote_and_quotes_take_position_slots():
    h = Harness()
    h.config.current.arbitrage.max_open_positions = 1
    executor = await h.start()
    await h.tick()

    taker_signal = SpreadEstimate(
        BTC, OpportunityLeg("okx", D(100), D("0.05")), OpportunityLeg("bybit", D(101), D("0.05")), D(1), D(1), D(0), BASE_TIME
    )
    await executor.process_opportunities([taker_signal], NONE)

    assert h.cheap.placed_orders == {}
    assert len(h.rich.placed_orders) == 1  # только заявка-мейкер


async def test_cancel_pending_entries_on_shutdown():
    h = Harness()
    executor = await h.start()
    await h.tick()
    ((order_id, _),) = h.maker_orders()

    await executor.cancel_pending_entries(NONE)

    assert not executor.has_pending_entries
    assert order_id in h.rich.cancelled_orders


async def test_disabling_maker_hedge_withdraws_quotes():
    h = Harness()
    executor = await h.start()
    await h.tick()
    h.config.current.arbitrage.maker_hedge.enabled = False

    await h.tick(with_signal=False)

    assert not executor.has_pending_entries
    assert len(h.rich.cancelled_orders) == 1


async def test_exchange_without_post_only_is_not_used_as_maker():
    h = Harness()
    h.rich = FakeConnector("binanceusdm", taker_percent=D("0.05"))  # post-only в фьючерсах Binance нет
    h.rich.place_order_override = _resting_post_only
    h.set_quotes(cheap_ask=D(100), rich_bid=D("100.2"))
    executor = await h.start()
    tickers = {"okx": h.cheap.tickers, "binanceusdm": h.rich.tickers}
    signal = SpreadEstimate(
        BTC, OpportunityLeg("okx", D(100), D("0.05")), OpportunityLeg("binanceusdm", D("100.2"), D("0.05")), D("0.2"), D(0), D(0), BASE_TIME
    )

    await executor.process_maker_quotes([signal], tickers, NONE)

    assert not executor.has_pending_entries
    assert h.rich.placed_orders == {}


async def test_dry_run_simulates_quote_and_fill():
    h = Harness(NetworkMode.DRY_RUN)
    executor = await h.start()
    await h.tick()
    assert executor.has_pending_entries
    assert h.rich.placed_orders == {}  # в симуляции заявок на биржу нет

    h.clock.advance(timedelta(milliseconds=200))
    h.set_quotes(cheap_ask=D(100), rich_bid=D("100.6"))  # покупатели дошли до цены мейкера
    await h.tick()

    assert not executor.has_pending_entries
    position = executor._positions[BTC]
    assert position.simulated
    assert position.entry_short == D("100.6") and position.entry_long == D(100)
    # комиссия входа: maker 0.02 % от 100.6 + taker 0.05 % от 100
    assert position.fees_entry_usd == D("100.6") * D("0.0002") + D(100) * D("0.0005")


# ------------------------- сканер -------------------------


async def test_scanner_passes_maker_signals_below_taker_threshold():
    from .test_funding_filter import NOW as SCAN_NOW
    from .test_funding_filter import create

    h = await create()
    h.options.arbitrage.maker_hedge.enabled = True
    h.options.arbitrage.maker_hedge.trigger_gross_percent = D("0.3")
    # брутто 0.35 %: taker-нетто после 0.4 % комиссий круга отрицательное — taker-сигнала нет, а для мейкера — есть
    h.long.tickers = {BTC: ticker("binanceusdm", BTC, bid=D("99.9"), ask=D(100), ts=SCAN_NOW)}
    h.short.tickers = {BTC: ticker("bybit", BTC, bid=D("100.35"), ask=D("100.4"), ts=SCAN_NOW)}

    await h.scanner.scan_once(NONE)

    assert h.executor.processed == []
    (batch,) = h.executor.maker_batches
    (signal,) = batch
    assert signal.long_leg.exchange_id == "binanceusdm" and signal.short_leg.exchange_id == "bybit"
    assert signal.gross_percent == D("0.35")

    # ниже trigger — сигнала нет
    h.short.tickers = {BTC: ticker("bybit", BTC, bid=D("100.2"), ask=D("100.25"), ts=SCAN_NOW)}
    await h.scanner.scan_once(NONE)
    assert h.executor.maker_batches[-1] == []


async def test_scanner_skips_maker_signals_when_disabled():
    from .test_funding_filter import create

    h = await create()
    await h.scanner.scan_once(NONE)
    assert h.executor.maker_batches == []
