"""Реестр подключённых бирж: заполняется при запуске, движок и исполнитель берут коннекторы отсюда."""

from __future__ import annotations

from collections.abc import Iterable

from futures_arb_bot.core.abstractions import ExchangeConnector


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: list[ExchangeConnector] = []

    @property
    def connectors(self) -> list[ExchangeConnector]:
        return self._connectors

    def find(self, exchange_id: str) -> ExchangeConnector | None:
        return next((c for c in self._connectors if c.id == exchange_id), None)

    async def replace(self, connectors: Iterable[ExchangeConnector]) -> None:
        old = self._connectors
        self._connectors = list(connectors)
        for connector in old:
            if connector not in self._connectors:
                await connector.aclose()

    async def aclose(self) -> None:
        old, self._connectors = self._connectors, []
        for connector in old:
            try:
                await connector.aclose()
            except Exception:
                pass  # игнорируем ошибки закрытия соединений при завершении
