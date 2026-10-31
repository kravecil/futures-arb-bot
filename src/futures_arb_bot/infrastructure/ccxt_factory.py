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
from futures_arb_bot.infrastructure import ccxt_ws_patch

# до создания первой биржи: иначе WebSocket ccxt «засыпает» при потоке котировок пачками (см. модуль)
ccxt_ws_patch.apply()

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
    exchange = getattr(ccxt_async, key)(config)
    _fix_ws_urls(key, exchange)
    return exchange


OKX_WS_LEGACY_PORT = ":8443/"


def _fix_ws_urls(key: str, exchange: Any) -> None:
    """
    OKX выводит из работы порт 8443 у WebSocket («WebSocket port 8443 will be retired soon. Please migrate to port 443»,
    код 64010 — с ним биржа сразу рвёт поток при подключении), а ccxt 4.5.85 всё ещё ходит на 8443. Боевой адрес
    переводим на стандартный 443; демо-контур (urls.test) не трогаем. Тестнет включается позже set_sandbox_mode
    и копирует urls.test в urls.api, так что правка на него не влияет.
    """
    if key != "okx":
        return
    api = getattr(exchange, "urls", {}).get("api")
    if isinstance(api, dict) and isinstance(api.get("ws"), str) and OKX_WS_LEGACY_PORT in api["ws"]:
        api["ws"] = api["ws"].replace(OKX_WS_LEGACY_PORT, "/")


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
