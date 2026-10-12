"""
Кооперативная отмена в духе CancellationToken: остановка по Ctrl+C не обрывает
сетевой запрос на середине, а срабатывает в точках проверки и во время пауз.
"""

from __future__ import annotations

import asyncio


class OperationCanceledError(Exception):
    """Операция прервана запросом на остановку (аналог OperationCanceledException)."""


class CancellationToken:
    """Флаг отмены с ожиданием. ``CancellationToken.none()`` — токен, который никогда не срабатывает."""

    __slots__ = ("_event", "_linked")

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._linked: list[CancellationToken] = []

    @staticmethod
    def none() -> CancellationToken:
        return _NONE

    @property
    def is_cancellation_requested(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        if self is _NONE:
            return
        self._event.set()
        for child in self._linked:
            child.cancel()

    def linked(self) -> CancellationToken:
        """Дочерний токен: отменяется вместе с родителем, но может быть отменён и сам по себе."""
        child = CancellationToken()
        if self.is_cancellation_requested:
            child.cancel()
        elif self is not _NONE:
            self._linked.append(child)
        return child

    def throw_if_cancellation_requested(self) -> None:
        if self._event.is_set():
            raise OperationCanceledError

    async def wait(self) -> None:
        await self._event.wait()


_NONE = CancellationToken()


async def delay(seconds: float, ct: CancellationToken | None = None) -> None:
    """Пауза, прерываемая отменой (аналог Task.Delay(ms, ct)): при отмене — OperationCanceledError."""
    if ct is None or ct is _NONE:
        await asyncio.sleep(max(0.0, seconds))
        return
    ct.throw_if_cancellation_requested()
    try:
        await asyncio.wait_for(ct.wait(), timeout=max(0.0, seconds))
    except TimeoutError:
        return
    raise OperationCanceledError
