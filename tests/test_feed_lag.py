"""
Отставание потока котировок по биржевому времени сообщений.

02.11.2026: поток OKX отдавал старые цены (на 0.5–1.1% от стакана), а возраст котировки по моменту
получения был 2–490 мс — 22 фантомных сигнала подряд, все отклонены стаканом. Теперь отставание
меряется как (получение − биржевое время) − минимум этой разницы по бирже, и отставшая котировка
в расчёт не идёт.
"""

from dataclasses import replace

from futures_arb_bot.core.domain.options import MarketDataOptions
from futures_arb_bot.infrastructure.quote_stream import FeedLagMeter, QuoteStream

from .fakes import BASE_TIME, ManualClock, make_log
from .test_funding_filter import NONE, SYMBOL, create

CLOCK_OFFSET_MS = 28_000.0
"""Часы сервера отстают от биржи на 28 с (как 02.11.2026) — отставание от этого зависеть не должно."""


def exchange_ms(sent_at_s: float) -> float:
    """Биржевое время сообщения, отправленного в момент sent_at_s по монотонным часам сервера."""
    return sent_at_s * 1000 + CLOCK_OFFSET_MS


def test_clock_offset_and_network_floor_are_subtracted():
    meter = FeedLagMeter()
    # сеть 40 мс: базовая задержка, а не отставание
    assert meter.measure(exchange_ms(100.0), 100.040) == 0.0
    assert meter.measure(exchange_ms(101.0), 101.045) == 5.0


def test_late_message_shows_its_lag():
    meter = FeedLagMeter()
    meter.measure(exchange_ms(100.0), 100.040)
    # сообщение отправлено биржей в 101.0, дошло в 102.24 — опоздало на 1.2 с сверх обычных 40 мс
    assert meter.measure(exchange_ms(101.0), 102.240) == 1200.0


def test_base_follows_the_window_and_forgets_old_minimum():
    meter = FeedLagMeter(window_seconds=30)
    meter.measure(exchange_ms(0.0), 0.010)  # очень быстрое сообщение — база 10 мс
    meter.measure(exchange_ms(31.0), 31.100)  # новое окно: прошлое ещё в базе
    assert meter.measure(exchange_ms(32.0), 32.100) == 90.0
    meter.measure(exchange_ms(61.0), 61.100)  # окно с базой 10 мс ушло — база 100 мс
    assert meter.measure(exchange_ms(62.0), 62.100) == 0.0


def test_without_exchange_time_lag_is_unknown():
    meter = FeedLagMeter()
    assert meter.measure(None, 1.0) is None
    assert meter.measure(0, 1.0) is None
    assert meter.measure(True, 1.0) is None


def test_stream_snapshot_carries_lag(monkeypatch):
    from futures_arb_bot.infrastructure import quote_stream

    stream = QuoteStream(object(), "okx", "OKX", make_log(), ManualClock(BASE_TIME))
    now = [100.040]
    monkeypatch.setattr(quote_stream.time, "monotonic", lambda: now[0])
    stream._apply({"A/USDT:USDT": {"bid": 1.0, "ask": 1.01, "timestamp": exchange_ms(100.0)}})
    now[0] = 102.240
    stream._apply({"B/USDT:USDT": {"bid": 2.0, "ask": 2.01, "timestamp": exchange_ms(101.0)}})
    stream._apply({"C/USDT:USDT": {"bid": 3.0, "ask": 3.01, "timestamp": None}})

    snapshot = stream.snapshot({})
    assert snapshot["A/USDT:USDT"].feed_lag_ms == 0.0
    assert snapshot["B/USDT:USDT"].feed_lag_ms == 1200.0
    assert snapshot["C/USDT:USDT"].feed_lag_ms is None


def test_default_limit():
    assert MarketDataOptions().max_feed_lag_ms == 300


async def test_lagging_leg_does_not_produce_a_signal():
    h = await create()
    h.long.tickers[SYMBOL] = replace(h.long.tickers[SYMBOL], feed_lag_ms=900.0)

    await h.scanner.scan_once(NONE)

    assert h.executor.processed == []


async def test_lag_within_limit_passes_and_is_on_legs():
    h = await create()
    h.long.tickers[SYMBOL] = replace(h.long.tickers[SYMBOL], feed_lag_ms=250.0)
    h.short.tickers[SYMBOL] = replace(h.short.tickers[SYMBOL], feed_lag_ms=5.0)

    await h.scanner.scan_once(NONE)

    [candidate] = h.executor.processed
    assert candidate.long_leg.feed_lag_ms == 250.0
    assert candidate.short_leg.feed_lag_ms == 5.0


async def test_zero_limit_disables_the_check():
    h = await create()
    h.options.general.market_data.max_feed_lag_ms = 0
    h.long.tickers[SYMBOL] = replace(h.long.tickers[SYMBOL], feed_lag_ms=5_000.0)

    await h.scanner.scan_once(NONE)

    assert len(h.executor.processed) == 1


async def test_unknown_lag_is_not_a_reason_to_skip():
    h = await create()  # REST и биржи без времени сообщения: feed_lag_ms = None

    await h.scanner.scan_once(NONE)

    assert len(h.executor.processed) == 1
