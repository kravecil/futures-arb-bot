"""Сборка текста уведомления из шаблона: подстановки, значения по умолчанию, пустой/неполный шаблон."""

from datetime import UTC, datetime

from futures_arb_bot.core.domain.arbitrage import OpportunityLeg, SpreadEstimate
from futures_arb_bot.core.domain.enums import NetworkMode
from futures_arb_bot.core.domain.options import NotificationOptions
from futures_arb_bot.core.engine.notification_formatter import format_notification, test_message

from .fakes import D

DETECTED = datetime(2026, 1, 1, 12, 34, 56, tzinfo=UTC)


def estimate(net=D("0.8"), long_funding=None, short_funding=None) -> SpreadEstimate:
    return SpreadEstimate(
        "BTC/USDT:USDT",
        OpportunityLeg("binanceusdm", D("100.1234"), D("0.1"), long_funding),
        OpportunityLeg("bybit", D("101.5"), D("0.05"), short_funding),
        gross_percent=D("1.38"),
        net_percent=net,
        quote_volume_usd=D(12_345_678),
        detected_at=DETECTED,
    )


def test_default_template_renders_symbol_prices_and_spreads():
    text = format_notification(estimate(), NotificationOptions())

    assert "BTC/USDT:USDT" in text
    assert "binanceusdm @ 100.1234" in text
    assert "bybit @ 101.5" in text
    assert "+0.800%" in text
    assert "+1.380%" in text
    assert "12.3M" in text
    assert "2026-01-01 12:34:56" in text


def test_missing_funding_renders_as_unknown():
    assert "фандинг н/д/н/д" in format_notification(estimate(), NotificationOptions())


def test_funding_of_both_legs_is_shown():
    text = format_notification(estimate(long_funding=D("-0.5"), short_funding=D("0.25")), NotificationOptions())
    assert "-0.500%/+0.250%" in text


def test_empty_template_falls_back_to_default():
    assert format_notification(estimate(), NotificationOptions(text_template="   ")) == format_notification(estimate(), NotificationOptions())


def test_custom_template_replaces_known_tokens():
    options = NotificationOptions(
        text_template="{symbol}|{net}|{gross}|{longExchange}|{longPrice}|{shortExchange}|{shortPrice}|{longFee}|{shortFee}|{volume}"
    )
    assert format_notification(estimate(), options) == "BTC/USDT:USDT|+0.800%|+1.380%|binanceusdm|100.1234|bybit|101.5|+0.100%|+0.050%|12.3M"


def test_unknown_tokens_are_left_as_is():
    assert format_notification(estimate(), NotificationOptions(text_template="спред {net} {oops}")) == "спред +0.800% {oops}"


def test_test_message_mentions_mode_and_time():
    text = test_message(DETECTED, NetworkMode.TESTNET)
    assert "Testnet" in text
    assert "2026-01-01 12:34:56" in text
