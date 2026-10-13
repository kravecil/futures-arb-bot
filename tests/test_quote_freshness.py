"""
Свежесть котировок потока: время котировки — момент, когда поток прислал именно эту пару.
Раньше его подменяли временем последнего сообщения по любой паре, и застывшая цена (в том числе
пережившая переподключение) выглядела свежей — так рождались фантомные спреды 13.10.2026.
"""

import asyncio
import time
from datetime import timedelta

from futures_arb_bot.core.domain.options import SymbolsOptions
from futures_arb_bot.infrastructure import quote_stream
from futures_arb_bot.infrastructure.quote_stream import QuoteStream

from .fakes import BASE_TIME, D, ManualClock, make_log

A = "A/USDT:USDT"
B = "B/USDT:USDT"


class StreamApi:
    def __init__(self) -> None:
        self.has = {"watchBidsAsks": True}
        self.queue: asyncio.Queue = asyncio.Queue()

    async def watch_bids_asks(self, symbols):
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item


def quote(bid: float, ask: float) -> dict:
    return {"bid": bid, "ask": ask, "last": bid, "timestamp": None}


async def wait_for(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert condition()


async def test_quiet_pair_ages_even_while_other_pairs_update():
    clock = ManualClock(BASE_TIME)
    stream = QuoteStream(StreamApi(), "okx", "OKX", make_log(), clock)

    stream._apply({A: quote(1.0, 1.01)})
    clock.advance(timedelta(seconds=5))
    stream._apply({B: quote(2.0, 2.01)})

    snapshot = stream.snapshot({})
    assert snapshot[A].timestamp == BASE_TIME  # А молчит 5 с — так и показываем
    assert snapshot[B].timestamp == BASE_TIME + timedelta(seconds=5)


async def test_quotes_do_not_survive_a_stream_failure(monkeypatch):
    monkeypatch.setattr(quote_stream, "_BACKOFF_SECONDS", (0.01,))
    api = StreamApi()
    clock = ManualClock(BASE_TIME)
    stream = QuoteStream(api, "bybit", "Bybit", make_log(), clock)
    stream.subscribe([A, B])
    try:
        await api.queue.put({A: quote(1.0, 1.01), B: quote(2.0, 2.01)})
        await wait_for(lambda: len(stream.snapshot({})) == 2)

        # обрыв соединения, затем после переподключения пришла только B
        await api.queue.put(ConnectionError("ping-pong keepalive missing"))
        await wait_for(lambda: len(stream.snapshot({})) == 0)
        await api.queue.put({B: quote(2.1, 2.11)})
        await wait_for(lambda: B in stream.snapshot({}))

        snapshot = stream.snapshot({})
        assert A not in snapshot  # старая цена А не выдаётся за свежую
        assert snapshot[B].bid == D("2.1")
    finally:
        await stream.aclose()


def test_freshness_defaults_are_tight_enough_for_arbitrage():
    options = SymbolsOptions()
    assert options.max_ticker_age_seconds <= 2
    assert options.max_ticker_skew_seconds <= 0.5
    # дробные значения из YAML принимаются
    assert SymbolsOptions.model_validate({"max_ticker_skew_seconds": 0.3}).max_ticker_skew_seconds == 0.3
