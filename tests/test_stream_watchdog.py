"""
Сторожевой таймер потока котировок: «зависшее» соединение (не закрыто, но данные не идут) ccxt
замечает только по пропущенному ping-pong — у OKX/Bybit через ~36 с. Бот сбрасывает его сам, как
только поток молчит дольше reconnect_after_ms, и укорачивает период ping-pong (keep_alive_ms).
"""

import asyncio
import time

import ccxt

from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.domain.options import MarketDataOptions
from futures_arb_bot.infrastructure.quote_stream import QuoteStream

from .fakes import BASE_TIME, ManualClock, make_log
from .test_websocket_quotes import BTC, FakeApi, make_connector, wait_for

NONE = CancellationToken.none()


class FakeWsClient:
    def __init__(self, keys: list[str]) -> None:
        self.futures = {key: object() for key in keys}
        self.subscriptions: dict = {}
        self.errors: list[BaseException] = []

    def on_error(self, error: BaseException) -> None:
        self.errors.append(error)


def make_stream(api) -> QuoteStream:
    return QuoteStream(api, "okx", "OKX", make_log(), ManualClock(BASE_TIME))


def test_only_quote_connections_are_reset():
    api = FakeApi()
    quotes = FakeWsClient(["bidask::BTC/USDT:USDT", "bidask::ETH/USDT:USDT"])
    orders = FakeWsClient(["orders"])
    api.clients = {"wss://public": quotes, "wss://private": orders}
    stream = make_stream(api)

    assert stream.force_reconnect(7.0, 5.0) == 1
    assert len(quotes.errors) == 1 and isinstance(quotes.errors[0], ccxt.RequestTimeout)
    assert orders.errors == []  # поток своих заявок не трогаем
    assert stream.forced_reconnects == 1


def test_resets_are_spaced_and_backoff_resets_when_stream_revives():
    api = FakeApi()
    client = FakeWsClient(["bidask:BTC/USDT:USDT"])
    api.clients = {"wss://public": client}
    stream = make_stream(api)

    assert stream.force_reconnect(6.0, 5.0) == 1
    assert stream.force_reconnect(6.1, 5.0) == 0  # рано: следующий сброс не раньше паузы

    stream._forced_at = time.monotonic() - 6  # прошло 6 с, но пауза уже удвоилась до 10 с
    assert stream.force_reconnect(12.0, 5.0) == 0

    # поток ожил — пауза снова обычная
    stream._apply({BTC: {"bid": 1.0, "ask": 1.01}})
    stream._forced_at = time.monotonic() - 6
    assert stream.force_reconnect(6.0, 5.0) == 1


async def test_connector_resets_silent_stream_and_logs_it(monkeypatch):
    api = FakeApi()
    client = FakeWsClient(["bidask::BTC/USDT:USDT"])
    api.clients = {"wss://public": client}
    connector = make_connector(monkeypatch, api)
    await connector.connect()
    connector.update_market_data(MarketDataOptions(stale_after_ms=20, reconnect_after_ms=30), 500)
    connector.subscribe_quotes([BTC])
    await api.updates.put({BTC: {"bid": 101.5, "ask": 101.6}})
    await wait_for(lambda: connector.stream_messages == 1)

    await asyncio.sleep(0.06)  # поток замолчал
    await connector.fetch_tickers(NONE)

    assert len(client.errors) == 1
    messages = [e.message for e in connector._log.latest(20)]
    assert any("переподключаю принудительно" in m for m in messages)
    await connector.aclose()


async def test_reconnect_watchdog_can_be_disabled(monkeypatch):
    api = FakeApi()
    client = FakeWsClient(["bidask::BTC/USDT:USDT"])
    api.clients = {"wss://public": client}
    connector = make_connector(monkeypatch, api)
    await connector.connect()
    connector.update_market_data(MarketDataOptions(stale_after_ms=20, reconnect_after_ms=0), 500)
    connector.subscribe_quotes([BTC])

    await asyncio.sleep(0.06)
    await connector.fetch_tickers(NONE)

    assert client.errors == []
    await connector.aclose()


async def test_keep_alive_period_is_applied_to_ccxt(monkeypatch):
    api = FakeApi()
    api.streaming = {"keepAlive": 18000, "maxPingPongMisses": 2}
    connector = make_connector(monkeypatch, api)

    connector.update_market_data(MarketDataOptions(keep_alive_ms=5000), 500)
    assert api.streaming == {"keepAlive": 5000, "maxPingPongMisses": 2}

    connector.update_market_data(MarketDataOptions(keep_alive_ms=0), 500)  # 0 — не трогаем
    assert api.streaming["keepAlive"] == 5000


async def test_silent_stream_is_not_reset_before_threshold(monkeypatch):
    api = FakeApi()
    client = FakeWsClient(["bidask::BTC/USDT:USDT"])
    api.clients = {"wss://public": client}
    connector = make_connector(monkeypatch, api)
    await connector.connect()
    connector.update_market_data(MarketDataOptions(stale_after_ms=20, reconnect_after_ms=60_000), 500)
    connector.subscribe_quotes([BTC])

    await asyncio.sleep(0.06)
    await connector.fetch_tickers(NONE)

    assert client.errors == []
    await connector.aclose()
