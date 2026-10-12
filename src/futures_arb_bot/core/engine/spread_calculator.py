"""
Расчёт спреда между двумя биржами. Брутто = (bid богатых − ask дешёвой) / ask дешёвой.
Нетто = брутто − комиссии taker полного круга (открытие и закрытие обеих ног) − буфер на проскальзывание.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from futures_arb_bot.core.abstractions import ConfigProvider
from futures_arb_bot.core.clock import Clock
from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.core.domain.market_data import ExchangeFees, TickerSnapshot
from futures_arb_bot.core.numbers import HUNDRED, ZERO

_ROUND_TRIP_MULTIPLIER = Decimal(2)
_FOUR_PLACES = Decimal("0.0001")


def _round4(value: Decimal) -> Decimal:
    # Math.Round(decimal, 4) в .NET — банковское округление
    return value.quantize(_FOUR_PLACES, rounding=ROUND_HALF_EVEN)


class DefaultSpreadCalculator:
    def __init__(self, config: ConfigProvider, clock: Clock) -> None:
        self._config = config
        self._clock = clock

    def calculate(self, cheaper: TickerSnapshot, richer: TickerSnapshot, cheap_fees: ExchangeFees, rich_fees: ExchangeFees) -> SpreadEstimate | None:
        if cheaper.exchange_id == richer.exchange_id or not cheaper.is_tradable or not richer.is_tradable:
            return None

        if richer.bid <= cheaper.ask:
            return None  # перекрытия нет — возможности нет

        options = self._config.current.arbitrage
        gross = (richer.bid - cheaper.ask) / cheaper.ask * HUNDRED

        if gross > options.max_spread_percent:
            # «слишком хорошо, чтобы быть правдой»: новые листинги/разные индексы — не арбитраж
            return None

        entry_fees = cheap_fees.taker_percent + rich_fees.taker_percent if options.include_fees else ZERO
        fee_cost = entry_fees * _ROUND_TRIP_MULTIPLIER
        net = gross - fee_cost - options.slippage_buffer_percent

        if net <= ZERO:
            return None

        return SpreadEstimate(
            cheaper.symbol,
            OpportunityLeg(cheaper.exchange_id, cheaper.ask, cheap_fees.taker_percent),
            OpportunityLeg(richer.exchange_id, richer.bid, rich_fees.taker_percent),
            _round4(gross),
            _round4(net),
            min(cheaper.quote_volume_24h, richer.quote_volume_24h),
            self._clock.now(),
        )
