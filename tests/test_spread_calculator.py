"""Порт SpreadCalculatorTests.cs."""

from decimal import Decimal

from fakes import NOW, FakeConfigProvider, FakeTime, make_ticker

from futures_arb_bot.domain.market_data import ExchangeFees
from futures_arb_bot.engine.spread_calculator import SpreadCalculator

D = Decimal


def create_calculator(slippage: Decimal = D("0.03")) -> SpreadCalculator:
    config = FakeConfigProvider()
    config.Current.Arbitrage.SlippageBufferPercent = slippage
    config.Current.Arbitrage.IncludeFees = True
    return SpreadCalculator(config, FakeTime(NOW))


def test_net_subtracts_fees_and_slippage():
    cheap = make_ticker("a", "BTC/USDT:USDT", bid=D("99.9"), ask=D("100"))
    rich = make_ticker("b", "BTC/USDT:USDT", bid=D("101"), ask=D("101.1"))

    estimate = create_calculator().Calculate(cheap, rich, ExchangeFees(D("0.1"), D("0.02")), ExchangeFees(D("0.1"), D("0.02")))

    assert estimate is not None
    # gross = (101 − 100) / 100 × 100 = 1.0000 %
    assert estimate.GrossPercent == D("1")
    # net = 1 − (0.1 + 0.1) × 2 (вход и выход обеих ног) − 0.03 = 0.5700 %
    assert estimate.NetPercent == D("0.57")
    assert estimate.LongLeg.ExchangeId == "a"
    assert estimate.ShortLeg.ExchangeId == "b"
    assert estimate.LongLeg.Price == D("100")
    assert estimate.ShortLeg.Price == D("101")


def test_net_subtracts_fees_of_the_full_round_trip():
    cheap = make_ticker("a", "BTC/USDT:USDT", bid=D("99.9"), ask=D("100"))
    rich = make_ticker("b", "BTC/USDT:USDT", bid=D("101"), ask=D("101.1"))

    # taker 0.1 % на ноге: вход стоит 0.2 %, выход ещё 0.2 % — вместе 0.4 %, буфер выключен
    estimate = create_calculator(slippage=D("0")).Calculate(
        cheap, rich, ExchangeFees(D("0.1"), D("0.02")), ExchangeFees(D("0.1"), D("0.02"))
    )

    assert estimate is not None
    assert estimate.GrossPercent == D("1")
    # было бы 0.8 % при учёте только входа — этого edge на самом деле не существует
    assert estimate.NetPercent == D("0.6")


def test_signal_rejected_when_round_trip_costs_eat_the_gross_spread():
    cheap = make_ticker("a", "BTC/USDT:USDT", bid=D("99.9"), ask=D("100"))
    rich = make_ticker("b", "BTC/USDT:USDT", bid=D("100.55"), ask=D("100.6"))

    # gross = 0.55 %: на одном входе хватило бы и комиссий (0.3 %), и буфера (0.03 %),
    # но полный круг стоит 0.6 % — сигнал должен отбрасываться, а не открываться в минус
    estimate = create_calculator().Calculate(cheap, rich, ExchangeFees(D("0.15"), D("0")), ExchangeFees(D("0.15"), D("0")))

    assert estimate is None


def test_returns_null_when_prices_do_not_overlap():
    cheap = make_ticker("a", "BTC/USDT:USDT", D("99.8"), D("100"))
    rich = make_ticker("b", "BTC/USDT:USDT", D("99.9"), D("100.1"))

    assert create_calculator().Calculate(cheap, rich, ExchangeFees(D("0.1"), D("0")), ExchangeFees(D("0.1"), D("0"))) is None


def test_returns_null_for_same_exchange():
    cheap = make_ticker("a", "BTC/USDT:USDT", D("99.8"), D("100"))
    rich = make_ticker("a", "BTC/USDT:USDT", D("101"), D("101.1"))

    assert create_calculator().Calculate(cheap, rich, ExchangeFees(D("0.1"), D("0")), ExchangeFees(D("0.1"), D("0"))) is None


def test_net_positive_without_fees_when_include_fees_disabled():
    config = FakeConfigProvider()
    config.Current.Arbitrage.IncludeFees = False
    config.Current.Arbitrage.SlippageBufferPercent = D("0")
    calculator = SpreadCalculator(config, FakeTime(NOW))

    cheap = make_ticker("a", "BTC/USDT:USDT", D("99.9"), D("100"))
    rich = make_ticker("b", "BTC/USDT:USDT", D("100.2"), D("100.3"))

    estimate = calculator.Calculate(cheap, rich, ExchangeFees(D("0.5"), D("0")), ExchangeFees(D("0.5"), D("0")))

    assert estimate is not None
    assert estimate.NetPercent == D("0.2")  # комиссии не вычитались


def test_ignores_absurd_spreads_above_sanity_cap():
    config = FakeConfigProvider()
    config.Current.Arbitrage.MaxSpreadPercent = D("5")
    calculator = SpreadCalculator(config, FakeTime(NOW))

    # расхождение 1900 % — типичная аномалия новых листингов, а не арбитраж
    cheap = make_ticker("a", "BTC/USDT:USDT", D("9.9"), D("10"))
    rich = make_ticker("b", "BTC/USDT:USDT", D("200"), D("200.2"))

    assert calculator.Calculate(cheap, rich, ExchangeFees(D("0.1"), D("0")), ExchangeFees(D("0.1"), D("0"))) is None
