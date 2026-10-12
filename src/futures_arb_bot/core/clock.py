"""Источник времени (аналог TimeProvider): в тестах подменяется управляемыми часами."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Текущее время в UTC (aware datetime)."""
        ...


class SystemClock:
    """Системные часы."""

    def now(self) -> datetime:
        return datetime.now(UTC)
