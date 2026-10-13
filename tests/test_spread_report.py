"""Отчёт о лучших спредах за период: сколько рынок реально давал и насколько это далеко от порога."""

from datetime import timedelta

from futures_arb_bot.core.domain.options import BotOptions
from futures_arb_bot.core.engine.spread_report import SpreadReporter

from .fakes import BASE_TIME, D, ManualClock, make_log
from .test_funding_filter import NONE, create


def make_reporter():
    clock = ManualClock(BASE_TIME)
    log = make_log(clock=clock)
    return SpreadReporter(log, clock), clock, log


def infos(log) -> list[str]:
    return [e.message for e in log.latest(50) if e.message.startswith("Спреды за")]


def test_report_lists_best_spread_per_symbol_and_buckets():
    reporter, clock, log = make_reporter()
    options = BotOptions()
    options.arbitrage.min_spread_percent_up = D("0.5")

    reporter.maybe_report(options)  # начало периода
    reporter.observe("S/USDT:USDT", "bybit", "okx", D("0.0700"), D("0.07035"), D("0.5"), D("-0.21"), anomaly=False)
    reporter.observe("S/USDT:USDT", "bybit", "okx", D("0.0700"), D("0.07014"), D("0.2"), D("-0.51"), anomaly=False)
    reporter.observe("W/USDT:USDT", "okx", "bybit", D("0.0190"), D("0.01902"), D("0.1"), D("-0.61"), anomaly=False)
    reporter.observe("NEW/USDT:USDT", "okx", "bybit", D(1), D(2), D(100), D(99), anomaly=True)
    reporter.tick(72)

    clock.advance(timedelta(seconds=61))
    reporter.maybe_report(options)

    [text] = infos(log)
    assert "S/USDT:USDT bybit→okx брутто +0.500%" in text  # лучший за период, а не последний
    assert text.index("S/USDT") < text.index("W/USDT")  # по убыванию брутто
    assert "≥+0.200%: 1" in text and "≥+0.500%: 1" in text and "≥+1.000%: 0" in text
    assert "с нетто выше порога: 0" in text
    assert "аномалия" in text and "NEW/USDT" not in text
    assert "сравнимых пар до 72" in text


def test_period_resets_and_empty_period_is_reported():
    reporter, clock, log = make_reporter()
    options = BotOptions()
    reporter.maybe_report(options)
    reporter.observe("S/USDT:USDT", "bybit", "okx", D(1), D("1.001"), D("0.1"), D("-0.6"), anomaly=False)
    clock.advance(timedelta(seconds=61))
    reporter.maybe_report(options)

    clock.advance(timedelta(seconds=61))
    reporter.maybe_report(options)

    texts = infos(log)
    assert len(texts) == 2
    assert sum("S/USDT" in t for t in texts) == 1  # во втором периоде прошлые спреды не повторяются
    assert sum("положительных спредов не было" in t for t in texts) == 1


def test_report_can_be_disabled():
    reporter, clock, log = make_reporter()
    options = BotOptions()
    options.general.market_data.spread_report_seconds = 0
    reporter.maybe_report(options)
    reporter.observe("S/USDT:USDT", "bybit", "okx", D(1), D("1.001"), D("0.1"), D("-0.6"), anomaly=False)
    clock.advance(timedelta(seconds=600))
    reporter.maybe_report(options)
    assert infos(log) == []


async def test_scanner_feeds_report_with_unprofitable_pairs_too():
    h = await create()
    h.options.general.market_data.spread_report_seconds = 60
    h.options.arbitrage.min_spread_percent_up = D(5)  # сделок не будет, но спред в отчёт попадает

    await h.scanner.scan_once(NONE)  # начало периода
    h.scanner._spread_report._started_at -= timedelta(seconds=61)
    await h.scanner.scan_once(NONE)

    texts = [e.message for e in h.scanner._log.latest(50) if e.message.startswith("Спреды за")]
    assert len(texts) == 1
    # ask 100 → bid 101: брутто 1%, комиссии круга 0.1×4 → нетто 0.6%
    assert "BTC/USDT:USDT binanceusdm→bybit брутто +1.000% / нетто +0.600%" in texts[0]
    assert h.executor.processed == []
