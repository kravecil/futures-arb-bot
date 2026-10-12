from datetime import UTC, datetime

from futures_arb_bot.core.domain.market_data import ExchangeFees
from futures_arb_bot.core.engine.spread_calculator import DefaultSpreadCalculator

from .fakes import D, FakeConfigProvider, FixedClock, ticker

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def create_calculator(slippage=D("0.03")) -> DefaultSpreadCalculator:
    config = FakeConfigProvider()
    config.current.arbitrage.slippage_buffer_percent = slippage
    config.current.arbitrage.include_fees = True
    return DefaultSpreadCalculator(config, FixedClock(NOW))


def fees(taker, maker=D(0)) -> ExchangeFees:
    return ExchangeFees(D(taker), D(maker))


def test_net_subtracts_fees_and_slippage():
    cheap = ticker("a", "BTC/USDT:USDT", bid=D("99.9"), ask=D(100))
    rich = ticker("b", "BTC/USDT:USDT", bid=D(101), ask=D("101.1"))

    estimate = create_calculator().calculate(cheap, rich, fees("0.1", "0.02"), fees("0.1", "0.02"))

    assert estimate is not None
    # gross = (101 − 100) / 100 × 100 = 1.0000 %
    assert estimate.gross_percent == D(1)
    # net = 1 − (0.1 + 0.1) × 2 (вход и выход обеих ног) − 0.03 = 0.5700 %
    assert estimate.net_percent == D("0.57")
    assert estimate.long_leg.exchange_id == "a"
    assert estimate.short_leg.exchange_id == "b"
    assert estimate.long_leg.price == D(100)
    assert estimate.short_leg.price == D(101)


def test_net_subtracts_fees_of_the_full_round_trip():
    cheap = ticker("a", "BTC/USDT:USDT", bid=D("99.9"), ask=D(100))
    rich = ticker("b", "BTC/USDT:USDT", bid=D(101), ask=D("101.1"))

    # taker 0.1 % на ноге: вход стоит 0.2 %, выход ещё 0.2 % — вместе 0.4 %, буфер выключен
    estimate = create_calculator(slippage=D(0)).calculate(cheap, rich, fees("0.1", "0.02"), fees("0.1", "0.02"))

    assert estimate is not None
    assert estimate.gross_percent == D(1)
    assert estimate.net_percent == D("0.6")


def test_signal_is_rejected_when_round_trip_costs_eat_the_gross_spread():
    cheap = ticker("a", "BTC/USDT:USDT", bid=D("99.9"), ask=D(100))
    rich = ticker("b", "BTC/USDT:USDT", bid=D("100.55"), ask=D("100.6"))

    # gross = 0.55 %, а полный круг стоит 0.6 % — сигнал должен отбрасываться
    assert create_calculator().calculate(cheap, rich, fees("0.15"), fees("0.15")) is None


def test_returns_none_when_prices_do_not_overlap():
    cheap = ticker("a", "BTC/USDT:USDT", D("99.8"), D(100))
    rich = ticker("b", "BTC/USDT:USDT", D("99.9"), D("100.1"))
    assert create_calculator().calculate(cheap, rich, fees("0.1"), fees("0.1")) is None


def test_returns_none_for_same_exchange():
    cheap = ticker("a", "BTC/USDT:USDT", D("99.8"), D(100))
    rich = ticker("a", "BTC/USDT:USDT", D(101), D("101.1"))
    assert create_calculator().calculate(cheap, rich, fees("0.1"), fees("0.1")) is None


def test_net_positive_without_fees_when_include_fees_disabled():
    config = FakeConfigProvider()
    config.current.arbitrage.include_fees = False
    config.current.arbitrage.slippage_buffer_percent = D(0)
    calculator = DefaultSpreadCalculator(config, FixedClock(NOW))

    cheap = ticker("a", "BTC/USDT:USDT", D("99.9"), D(100))
    rich = ticker("b", "BTC/USDT:USDT", D("100.2"), D("100.3"))

    estimate = calculator.calculate(cheap, rich, fees("0.5"), fees("0.5"))

    assert estimate is not None
    assert estimate.net_percent == D("0.2")  # комиссии не вычитались


def test_ignores_absurd_spreads_above_sanity_cap():
    config = FakeConfigProvider()
    config.current.arbitrage.max_spread_percent = D(5)
    calculator = DefaultSpreadCalculator(config, FixedClock(NOW))

    # расхождение 1900 % — типичная аномалия новых листингов, а не арбитраж
    cheap = ticker("a", "BTC/USDT:USDT", D("9.9"), D(10))
    rich = ticker("b", "BTC/USDT:USDT", D(200), D("200.2"))

    assert calculator.calculate(cheap, rich, fees("0.1"), fees("0.1")) is None
