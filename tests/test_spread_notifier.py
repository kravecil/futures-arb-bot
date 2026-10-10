"""Порт SpreadNotifierTests.cs + NotificationFormatterTests.cs."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

from fakes import NOW, FakeConfigProvider, FakeTime
from testutil import estimate

from futures_arb_bot.domain.arbitrage import OpportunityLeg
from futures_arb_bot.domain.delivery import NotificationDelivery
from futures_arb_bot.engine.event_log import EventLog
from futures_arb_bot.engine.notification_formatter import Format
from futures_arb_bot.engine.spread_notifier import SpreadNotifier

D = Decimal


class FakeTransportRec:
    def __init__(self, outcome: NotificationDelivery | None = None) -> None:
        self.Sent: list[str] = []
        self.Outcome = outcome or NotificationDelivery.Delivered
        self.CallCount = 0

    async def SendAsync(self, text, ct=None):
        self.CallCount += 1
        self.Sent.append(text)
        return self.Outcome


def build_notifier(transport=None, time=None, **notification_changes):
    config = FakeConfigProvider()
    notifications = config.Current.Notifications
    notifications.Enabled = True
    notifications.BotToken = "t0ken-abc"
    notifications.AdminChatId = "chat-1"
    notifications.SignalCooldownSeconds = 120
    notifications.MaxPerMinute = 20
    for key, value in notification_changes.items():
        setattr(notifications, key, value)
    time = time or FakeTime(NOW)
    log = EventLog(config, time)
    transport = transport or FakeTransportRec()
    notifier = SpreadNotifier(config, transport, log, time)
    return notifier, transport, config, log, time


def test_below_threshold_not_sent():
    notifier, transport, config, _, _ = build_notifier()
    config.Current.Notifications.MinSpreadPercent = D("0.8")

    notifier.Notify(estimate(net=D("0.5")))
    assert transport.CallCount == 0
    assert notifier.PendingCount == 0


def test_cooldown_suppresses_second_signal_for_same_symbol():
    time = FakeTime(NOW)
    notifier, transport, _, _, _ = build_notifier(time=time)

    async def scenario():
        notifier.Notify(estimate(net=D("0.9")))
        notifier.Notify(estimate(net=D("0.9")))

        await asyncio.sleep(0.05)
        assert transport.CallCount == 1
        assert notifier.SuppressedCount == 1

        time.Advance(timedelta(seconds=121))
        notifier.Notify(estimate(net=D("0.9")))
        await asyncio.sleep(0.05)
        assert transport.CallCount == 2

    asyncio.run(scenario())


def test_rate_limit_per_minute():
    notifier, transport, _, _, _ = build_notifier(SignalCooldownSeconds=0, MaxPerMinute=3)

    for i in range(5):
        notifier.Notify(estimate(symbol=f"S{i}/USDT:USDT", net=D("0.9")))

    assert notifier.PendingCount + transport.CallCount == 3
    assert notifier.SuppressedCount == 2


def test_permanent_failure_opens_circuit_immediately():
    time = FakeTime(NOW)
    transport = FakeTransportRec(NotificationDelivery.Permanent("401 bad token"))
    notifier, transport, _, log, _ = build_notifier(transport=transport, time=time)

    async def scenario():
        notifier.Notify(estimate(symbol="A/USDT:USDT", net=D("0.9")))
        await asyncio.sleep(0.05)

        assert any("Уведомления приостановлены" in e.Message for e in log.Latest(50))

        # канал закрыт: новый сигнал подавляется на время CircuitBreak
        notifier.Notify(estimate(symbol="B/USDT:USDT", net=D("0.9")))
        assert notifier.SuppressedCount == 1

        # после CircuitBreakMinutes канал снова работает
        time.Advance(timedelta(minutes=11))
        notifier.Notify(estimate(symbol="C/USDT:USDT", net=D("0.9")))
        await asyncio.sleep(0.05)
        assert transport.CallCount == 2

    asyncio.run(scenario())


def test_failure_circuit_after_transient_series():
    transport = FakeTransportRec(NotificationDelivery.Transient("5xx"))
    notifier, transport, _, log, _ = build_notifier(transport=transport, FailureCircuitLimit=2, CircuitBreakMinutes=5)

    async def scenario():
        for i in range(3):
            notifier.Notify(estimate(symbol=f"S{i}/USDT:USDT", net=D("0.9")))
        await asyncio.sleep(0.1)
        assert any("Уведомления приостановлены на 5 мин" in e.Message for e in log.Latest(50))

    asyncio.run(scenario())


# ------------------------- NotificationFormatter -------------------------


def test_formatter_substitutes_all_tokens():
    est = estimate(D("100"), D("101"), gross=D("1.001"), net=D("0.57"), volume=D("12345678"))
    est = est.with_(
        LongLeg=OpportunityLeg("binanceusdm", D("100"), D("0.1"), D("0.01")),
        ShortLeg=OpportunityLeg("bybit", D("101"), D("0.1"), D("0.02")),
    )
    options = FakeConfigProvider().Current.Notifications

    text = Format(est, options)

    assert "Арбитраж BTC/USDT:USDT" in text
    assert "+0.570%" in text
    assert "+1.001%" in text
    assert "лонг binanceusdm @ 100 → шорт bybit @ 101" in text
    assert "фандинг +0.010%/+0.020%" in text
    assert "12.3M $" in text
    assert "время UTC" in text
    assert "{" not in text  # ни один токен не остался неразвёрнутым


def test_formatter_respects_custom_template():
    options = FakeConfigProvider().Current.Notifications
    options.TextTemplate = "{symbol}|{net}"
    text = Format(estimate(net=D("0.5")), options)
    assert text == "BTC/USDT:USDT|+0.500%"
