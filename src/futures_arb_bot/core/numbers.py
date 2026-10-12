"""Работа с Decimal: преобразования и форматирование, совместимое с прежней версией (.NET decimal)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)


def to_dec(value: object) -> Decimal:
    """Число любого типа → Decimal. float округляется до 15 значащих цифр (как явное приведение в .NET)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(format(value, ".15g"))
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as ex:
        raise ValueError(f"не число: {value!r}") from ex


def dec_str(value: Decimal | int | float) -> str:
    """Decimal без экспоненты, как decimal.ToString() в .NET: сохраняет масштаб (0.50 → «0.50»)."""
    d = to_dec(value)
    if d.is_zero():
        # .NET не печатает «-0», а масштаб сохраняет: 0.00 → «0.00»
        exponent = d.as_tuple().exponent
        places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
        return "0" if places == 0 else "0." + "0" * places
    return format(d, "f")


def round_away(value: Decimal, places: int) -> Decimal:
    """Округление «от нуля» (MidpointRounding.AwayFromZero) до places знаков."""
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _group_thousands(integer_digits: str) -> str:
    """«1234567» → «1,234,567»."""
    parts: list[str] = []
    while len(integer_digits) > 3:
        parts.insert(0, integer_digits[-3:])
        integer_digits = integer_digits[:-3]
    parts.insert(0, integer_digits)
    return ",".join(parts)


def format_number(value: Decimal | float | int, *, min_decimals: int, max_decimals: int, group: bool = False) -> str:
    """
    Аналог пользовательских форматов .NET вида «#,0.00##»: от min_decimals до max_decimals
    знаков после точки (лишние нули отбрасываются), округление от нуля, без знака для нуля.
    Знак «-» ставится, только если после округления число не ноль.
    """
    d = round_away(to_dec(value), max_decimals)
    negative = d < 0 and not d.is_zero()
    d = abs(d)
    text = format(d, "f")
    integer, _, fraction = text.partition(".")
    fraction = fraction.rstrip("0")
    if len(fraction) < min_decimals:
        fraction = fraction.ljust(min_decimals, "0")
    if group:
        integer = _group_thousands(integer)
    result = integer + ("." + fraction if fraction else "")
    return ("-" if negative else "") + result


def format_signed(value: Decimal | float | int, *, decimals: int, group: bool = False) -> str:
    """
    Трёхсекционный формат .NET «+0.000;-0.000;0.000»: плюс для положительных,
    минус для отрицательных, без знака — если после округления получился ноль.
    """
    d = round_away(to_dec(value), decimals)
    body = format_number(abs(d), min_decimals=decimals, max_decimals=decimals, group=group)
    if d.is_zero():
        return body
    return ("+" if d > 0 else "-") + body


def format_n0(value: int | float | Decimal) -> str:
    """Формат «N0» инвариантной культуры: целое с разделителем тысяч «,»."""
    return format_number(value, min_decimals=0, max_decimals=0, group=True)
