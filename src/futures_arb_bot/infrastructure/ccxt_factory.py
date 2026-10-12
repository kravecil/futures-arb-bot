"""
Фабрика коннекторов: создаёт экземпляры CCXT по идентификатору биржи из конфигурации.
Чтобы добавить новую биржу — допишите её в SUPPORTED (и в карту возможностей, см. README).
"""

from __future__ import annotations

from typing import Any

import ccxt.pro as ccxt_async  # асинхронные классы бирж с REST и WebSocket (ccxt.pro)

from futures_arb_bot.core.abstractions import EventLog, ExchangeConnector
from futures_arb_bot.core.clock import Clock
from futures_arb_bot.core.domain.enums import NetworkMode
from futures_arb_bot.core.domain.options import ExchangeConfigEntry

SUPPORTED = (
    "binanceusdm",
    "binance",
    "bybit",
    "okx",
    "gate",
    "mexc",
    "kucoinfutures",
    "bitget",
    "htx",
    "coinex",
    "phemex",
    "krakenfutures",
    "bingx",
)


def instantiate(exchange_id: str, config: dict[str, Any]) -> Any:
    """Создаёт асинхронный экземпляр биржи CCXT (REST + WebSocket)."""
    key = exchange_id.lower()
    if key not in SUPPORTED:
        raise ValueError(f"Неизвестный идентификатор биржи: {exchange_id}")
    return getattr(ccxt_async, key)(config)


class CcxtExchangeFactory:
    def __init__(self, log: EventLog, clock: Clock) -> None:
        self._log = log
        self._clock = clock

    @property
    def supported_ids(self) -> list[str]:
        return sorted(SUPPORTED, key=str.lower)

    def create(self, entry: ExchangeConfigEntry, mode: NetworkMode) -> ExchangeConnector:
        if entry.id.lower() not in SUPPORTED:
            raise ValueError(
                f"Биржа «{entry.id}» не встроена в фабрику. Поддерживаются: {', '.join(self.supported_ids)}. "
                "Как добавить новую — см. раздел «Добавление биржи» в README."
            )

        from futures_arb_bot.infrastructure.ccxt_connector import CcxtExchangeConnector

        return CcxtExchangeConnector(entry, mode, self._log, self._clock)
