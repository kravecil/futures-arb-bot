"""Фильтр торговых пар: линейные бессрочные фьючерсы, котируемые валюты, оборот, include/exclude."""

from __future__ import annotations

import re
from decimal import Decimal
from functools import lru_cache

from futures_arb_bot.core.abstractions import ConfigProvider
from futures_arb_bot.core.domain.market_data import MarketInfo
from futures_arb_bot.core.numbers import to_dec


@lru_cache(maxsize=1024)
def _wildcard(pattern: str) -> re.Pattern[str]:
    """Шаблон с * и ? → скомпилированный regex без учёта регистра."""
    source = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.compile(f"^{source}$", re.IGNORECASE)


class DefaultSymbolFilter:
    def __init__(self, config: ConfigProvider) -> None:
        # как и прежде, раздел symbols читается один раз — при создании фильтра
        self._options = config.current.symbols

    def is_allowed(self, market: MarketInfo, quote_volume_24h: Decimal | int = 0) -> bool:
        if not market.is_perpetual or not market.is_linear:
            return False

        quotes = self._options.quote_currencies
        if quotes and market.quote_currency.lower() not in {q.lower() for q in quotes}:
            return False

        volume = to_dec(quote_volume_24h)
        if volume > 0 and volume < self._options.min_quote_volume24h_usd:
            return False

        if self._options.include and not any(_wildcard(p).match(market.symbol) for p in self._options.include):
            return False

        return not any(_wildcard(p).match(market.symbol) for p in self._options.exclude)
