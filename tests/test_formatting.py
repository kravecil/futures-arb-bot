"""Форматы чисел должны совпадать с прежней версией (.NET custom format strings)."""

from datetime import timedelta

import pytest

from futures_arb_bot.core.engine import formatting
from futures_arb_bot.core.numbers import dec_str, format_n0

from .fakes import D


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (D("65432.125"), "65,432.13"),
        (D("1234"), "1,234"),
        (D("1.23456"), "1.2346"),
        (D("100.10"), "100.1"),
        (D("0.000123456"), "0.000123"),
        (D("0.5"), "0.5"),
        (D(0), "0"),
    ],
)
def test_price(value, expected):
    assert formatting.price(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(D("1234.5"), "+1,234.50 $"), (D("-12.345"), "-12.35 $"), (D(0), "0.00 $"), (D("-0.004"), "0.00 $"), (D("0.004"), "0.00 $")],
)
def test_usd(value, expected):
    assert formatting.usd(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(D("0.8"), "+0.800%"), (D("-0.1234"), "-0.123%"), (D("0.0005"), "+0.001%"), (D(0), "0.000%"), (D("-0.0004"), "0.000%")],
)
def test_pct(value, expected):
    assert formatting.pct(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(D(12_345_678), "12.3M"), (D(999), "999"), (D(1500), "1.5K"), (D(2_000_000_000), "2B"), (D("0.25"), "0.3")],
)
def test_volume(value, expected):
    assert formatting.volume(value) == expected


def test_duration():
    assert formatting.duration(timedelta(hours=2, minutes=3, seconds=4.9)) == "02:03:04"
    assert formatting.duration(timedelta(days=1, hours=2, minutes=3, seconds=4)) == "1д 02:03:04"


def test_decimal_text_keeps_scale_without_exponent():
    assert dec_str(D("35")) == "35"
    assert dec_str(D("0.50")) == "0.50"
    assert dec_str(D("1E-7")) == "0.0000001"
    assert format_n0(1234567) == "1,234,567"
