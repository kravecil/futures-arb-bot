"""Форматирование чисел для консоли и отчётов (те же форматы, что и в прежней версии)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from decimal import Decimal

from futures_arb_bot.core.domain.enums import AppLogLevel
from futures_arb_bot.core.numbers import format_number, format_signed, to_dec

_THOUSAND = Decimal(1_000)
_MILLION = Decimal(1_000_000)
_BILLION = Decimal(1_000_000_000)


def price(value: Decimal | float | int) -> str:
    """Цена с разумным числом знаков в зависимости от порядка величины."""
    d = to_dec(value)
    magnitude = abs(d)
    if magnitude >= _THOUSAND:
        return format_number(d, min_decimals=0, max_decimals=2, group=True)
    if magnitude >= 1:
        return format_number(d, min_decimals=0, max_decimals=4, group=True)
    return format_number(d, min_decimals=0, max_decimals=6)


def usd(amount: Decimal | float | int) -> str:
    """Сумма в долларах со знаком: «+1,234.50 $»."""
    return format_signed(amount, decimals=2, group=True) + " $"


def _invariant(value: Decimal) -> str:
    return format_number(value, min_decimals=0, max_decimals=1)


def volume(value: Decimal | float | int) -> str:
    """Объём в «человекочитаемом» виде: 12.3M, 456K… Разделитель — всегда точка."""
    d = to_dec(value)
    magnitude = abs(d)
    if magnitude >= _BILLION:
        return _invariant(d / _BILLION) + "B"
    if magnitude >= _MILLION:
        return _invariant(d / _MILLION) + "M"
    if magnitude >= _THOUSAND:
        return _invariant(d / _THOUSAND) + "K"
    return _invariant(d)


def pct(percent: Decimal | float | int) -> str:
    """Процент со знаком и тремя знаками после точки."""
    return format_signed(percent, decimals=3) + "%"


def optional_pct(percent: Decimal | None) -> str:
    """Процент или «н/д» для отсутствующего значения (например, фандинг-рейта ноги)."""
    return pct(percent) if percent is not None else "н/д"


def duration(span: timedelta) -> str:
    """Длительность в виде «1д 02:03:04» / «02:03:04»."""
    total = int(span.total_seconds())
    days, rest = divmod(total, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, seconds = divmod(rest, 60)
    clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{days}д {clock}" if days >= 1 else clock


def join(separator: str, parts: Iterable[str | None]) -> str:
    """Склейка непустых частей через разделитель."""
    return separator.join(p for p in parts if p is not None and p.strip())


def pct_markup(value: Decimal) -> str:
    """Красит процент: положительный — зелёным, отрицательный — красным (rich-разметка)."""
    if value > 0:
        return f"[green]{pct(value)}[/]"
    if value < 0:
        return f"[red]{pct(value)}[/]"
    return pct(value)


# Палитра совпадает с прежней (Spectre.Console): grey = цвет 8, silver = цвет 7, white = цвет 15.
GREY = "bright_black"
SILVER = "white"
WHITE = "bright_white"

_LEVEL_COLORS = {
    AppLogLevel.DEBUG: GREY,
    AppLogLevel.INFO: SILVER,
    AppLogLevel.SUCCESS: "green",
    AppLogLevel.WARNING: "yellow",
    AppLogLevel.ERROR: "red",
}


def level_color(level: AppLogLevel) -> str:
    """Цвет уровня события для rich-разметки."""
    return _LEVEL_COLORS.get(level, WHITE)
