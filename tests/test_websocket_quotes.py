"""
Котировки по WebSocket: поток кладёт лучшие цены в кэш, коннектор отдаёт их без REST-запроса,
при молчании потока откатывается на REST (не чаще refresh_interval_ms), сканер подписывает пары,
торгуемые минимум на двух биржах.
"""

import asyncio
import time

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.clock import SystemClock
from futures_arb_bot.core.domain.enums import MarketDataSource, NetworkMode
from futures_arb_bot.core.domain.options import ExchangeConfigEntry, MarketDataOptions
from futures_arb_bot.core.engine.arbitrage_scanner import DefaultArbitrageScanner
from futures_arb_bot.core.engine.connector_registry import ConnectorRegistry
from futures_arb_bot.core.engine.spread_calculator import DefaultSpreadCalculator
from futures_arb_bot.core.engine.statistics_collector import DefaultStatisticsCollector
from futures_arb_bot.core.engine.symbol_filter import DefaultSymbolFilter
from futures_arb_bot.infrastructure import ccxt_connector
from futures_arb_bot.infrastructure.ccxt_connector import CcxtExchangeConnector

from .fakes import BASE_TIME, D, FakeConfigProvider, FakeConnector, FakeExecutor, FakeNotifier, FixedClock, make_log, ticker

BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"
NONE = CancellationToken.none()


class FakeApi:
    """Минимальная замена биржи ccxt.pro: рынки, REST-тикеры и очередь сообщений потока."""

    def __init__(self, has_ws: bool = True) -> None:
        self.has = {"watchBidsAsks": has_ws}
        self.rest_calls = 0
        self.watch_calls: list[list[str]] = []
        self.updates: asyncio.Queue[dict] = asyncio.Queue()
        self.closed = False

    async def load_markets(self):
        market = {"swap": True, "linear": True, "active": True, "taker": 0.0005, "maker": 0.0002, "quote": "USDT"}
        return {BTC: dict(market), ETH: dict(market)}

    async def fetch_tickers(self):
        self.rest_calls += 1
        now_ms = int(time.time() * 1000)
        return {
            BTC: {"bid": 100.0, "ask": 100.1, "last": 100.05, "quoteVolume": 9e8, "timestamp": now_ms},
            ETH: {"bid": 10.0, "ask": 10.01, "last": 10.0, "quoteVolume": 5e8, "timestamp": now_ms},
        }

    async def fetch_bids_asks(self):
        import ccxt

        raise ccxt.NotSupported("нет")

    async def watch_bids_asks(self, symbols):
        self.watch_calls.append(list(symbols))
        return await self.updates.get()

    async def close(self):
        self.closed = True


def make_connector(monkeypatch, api: FakeApi) -> CcxtExchangeConnector:
    monkeypatch.setattr(ccxt_connector, "instantiate", lambda _id, _cfg: api)
    return CcxtExchangeConnector(ExchangeConfigEntry(id="okx", name="OKX"), NetworkMode.DRY_RUN, make_log(), SystemClock())


async def wait_for(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert condition()


async def test_stream_quotes_replace_rest_and_keep_rest_volumes(monkeypatch):
    api = FakeApi()
    connector = make_connector(monkeypatch, api)
    await connector.connect()
    connector.update_market_data(MarketDataOptions(stale_after_ms=1_000), 500)

    first = await connector.fetch_tickers(NONE)
    assert first.source == "rest" and api.rest_calls == 1

    assert connector.subscribe_quotes([BTC]) == [BTC]
    stamp_ms = int(time.time() * 1000)
    await api.updates.put({BTC: {"symbol": BTC, "bid": 101.5, "ask": 101.6, "timestamp": stamp_ms}})
    await wait_for(lambda: connector.stream_messages == 1)

    result = await connector.fetch_tickers(NONE)
    assert result.source == "ws"
    assert api.rest_calls == 1  # без нового запроса REST
    btc = result.tickers[BTC]
    assert (btc.bid, btc.ask) == (D("101.5"), D("101.6"))
    assert btc.quote_volume_24h == D("9E+8")  # оборот — из снимка REST
    assert ETH in result.tickers  # неподписанные пары — из снимка REST (отсеются по возрасту)
    assert api.watch_calls[0] == [BTC]

    await connector.aclose()
    assert api.closed


async def test_silent_stream_falls_back_to_rest_no_more_often_than_refresh_interval(monkeypatch):
    api = FakeApi()
    connector = make_connector(monkeypatch, api)
    await connector.connect()
    connector.update_market_data(MarketDataOptions(stale_after_ms=500), 60_000)

    await connector.fetch_tickers(NONE)
    connector.subscribe_quotes([BTC])  # подписались, но поток молчит

    cached = await connector.fetch_tickers(NONE)
    assert cached.source == "cache" and api.rest_calls == 1  # снимок свежий — нового запроса нет

    connector.update_market_data(MarketDataOptions(stale_after_ms=500), 500)
    connector._rest_at = time.monotonic() - 1  # снимок устарел
    fallback = await connector.fetch_tickers(NONE)
    assert fallback.source == "rest" and api.rest_calls == 2

    await connector.aclose()


async def test_rest_mode_ignores_stream(monkeypatch):
    api = FakeApi()
    connector = make_connector(monkeypatch, api)
    await connector.connect()
    connector.update_market_data(MarketDataOptions(source=MarketDataSource.REST), 500)
    connector.subscribe_quotes([BTC])
    await api.updates.put({BTC: {"symbol": BTC, "bid": 101.5, "ask": 101.6}})
    await wait_for(lambda: connector.stream_messages == 1)

    assert (await connector.fetch_tickers(NONE)).source == "rest"
    await connector.aclose()


async def test_exchange_without_stream_is_rest_only(monkeypatch):
    connector = make_connector(monkeypatch, FakeApi(has_ws=False))
    await connector.connect()
    assert not connector.supports_streaming
    assert connector.subscribe_quotes([BTC]) == []
    await connector.aclose()


async def test_scanner_subscribes_cross_exchange_liquid_symbols_and_switches_interval():
    config = FakeConfigProvider()
    config.current.general.market_data = MarketDataOptions(source=MarketDataSource.WEBSOCKET)
    config.current.symbols.min_quote_volume24h_usd = D(1_000_000)

    a, b, c = FakeConnector("a"), FakeConnector("b"), FakeConnector("c")
    for connector in (a, b, c):
        connector.streaming_supported = True
    c.streaming_supported = False
    a.tickers = {BTC: ticker("a", BTC, D(100), D("100.1")), ETH: ticker("a", ETH, D(10), D("10.01"), volume=D(10))}
    b.tickers = {BTC: ticker("b", BTC, D(100), D("100.1")), ETH: ticker("b", ETH, D(10), D("10.01"), volume=D(10))}
    c.tickers = {BTC: ticker("c", BTC, D(100), D("100.1"))}

    registry = ConnectorRegistry()
    await registry.replace([a, b, c])
    clock = FixedClock(BASE_TIME)
    stats = DefaultStatisticsCollector(config, clock)
    log = make_log(config, clock)
    scanner = DefaultArbitrageScanner(
        config, registry, DefaultSpreadCalculator(config, clock), DefaultSymbolFilter(config), FakeExecutor(), stats, FakeNotifier(), log, clock
    )

    await scanner.scan_once(NONE)

    assert a.subscribed == {BTC}  # ETH отсеян по обороту
    assert b.subscribed == {BTC}
    assert c.subscribed == set()  # у биржи нет потока
    assert a.market_data is config.current.general.market_data
    assert scanner._streaming
    assert any("потока котировок в ccxt нет" in e.message for e in log.latest(50))
    assert any("котировки по WebSocket" in e.message for e in log.latest(50))


def test_report_quotes_column():
    from futures_arb_bot.app.ui.report import _quotes_source

    assert _quotes_source(0, 0) == "—"
    assert _quotes_source(0, 5) == "REST"
    assert _quotes_source(99, 1) == "WS 99%"
