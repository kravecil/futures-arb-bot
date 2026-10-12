"""Сборка текста уведомления из шаблона notifications.text_template и оценки спреда."""

from __future__ import annotations

from datetime import UTC, datetime

from futures_arb_bot.core.domain.arbitrage import SpreadEstimate
from futures_arb_bot.core.domain.enums import NetworkMode
from futures_arb_bot.core.domain.options import DEFAULT_TEXT_TEMPLATE, NotificationOptions
from futures_arb_bot.core.engine import formatting


def format_notification(estimate: SpreadEstimate, options: NotificationOptions) -> str:
    """Развернуть шаблон в готовый текст сообщения (поля подставляются как есть)."""
    template = options.text_template if options.text_template.strip() else DEFAULT_TEXT_TEMPLATE

    substitutions = (
        ("{symbol}", estimate.symbol),
        ("{longExchange}", estimate.long_leg.exchange_id),
        ("{longPrice}", formatting.price(estimate.long_leg.price)),
        ("{shortExchange}", estimate.short_leg.exchange_id),
        ("{shortPrice}", formatting.price(estimate.short_leg.price)),
        ("{net}", formatting.pct(estimate.net_percent)),
        ("{gross}", formatting.pct(estimate.gross_percent)),
        ("{longFee}", formatting.pct(estimate.long_leg.fee_percent)),
        ("{shortFee}", formatting.pct(estimate.short_leg.fee_percent)),
        ("{longFunding}", formatting.optional_pct(estimate.long_leg.funding_percent)),
        ("{shortFunding}", formatting.optional_pct(estimate.short_leg.funding_percent)),
        ("{volume}", formatting.volume(estimate.quote_volume_usd)),
        ("{time}", _time(estimate.detected_at)),
    )

    for token, value in substitutions:
        template = template.replace(token, value)
    return template


def test_message(now: datetime, mode: NetworkMode) -> str:
    """Текст служебного сообщения для проверки канала (--notify-test)."""
    return f"FuturesArbBot: канал уведомлений настроен верно. Тестовое сообщение отправлено {_time(now)}, режим {mode}."


test_message.__test__ = False  # type: ignore[attr-defined]  # не тест для pytest


def _time(moment: datetime) -> str:
    """Момент обнаружения в UTC: «2026-09-17 12:34:56, время UTC»."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") + ", время UTC"
