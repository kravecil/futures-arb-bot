"""
Периодический отчёт о лучших спредах — для калибровки порогов входа. Сканер передаёт сюда каждую
сравнимую пару котировок (обе свежие, на разных биржах); раз в spread_report_seconds в журнал
пишется лучшее, что рынок показывал за период: брутто, нетто по формуле сканера и сколько пар
вообще доходило до заметного расхождения. Так видно, насколько далеко реальный рынок от порога.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from futures_arb_bot.core.abstractions import EventLog
from futures_arb_bot.core.clock import Clock
from futures_arb_bot.core.domain.options import BotOptions
from futures_arb_bot.core.engine import formatting
from futures_arb_bot.core.numbers import ZERO

GROSS_BUCKETS = (Decimal("0.2"), Decimal("0.5"), Decimal(1))
"""Пороги брутто (%), для которых в отчёте считается число пар, хоть раз их достигших."""


@dataclass(slots=True)
class _Best:
    gross: Decimal
    net: Decimal
    long_exchange: str
    short_exchange: str
    ask: Decimal
    bid: Decimal


class SpreadReporter:
    def __init__(self, log: EventLog, clock: Clock) -> None:
        self._log = log
        self._clock = clock
        self._started_at: datetime | None = None
        self._best: dict[str, _Best] = {}
        self._anomalies: set[str] = set()
        self._ticks = 0
        self._max_pairs = 0

    def observe(
        self, symbol: str, long_exchange: str, short_exchange: str, ask: Decimal, bid: Decimal, gross: Decimal, net: Decimal, anomaly: bool
    ) -> None:
        """Пара котировок символа: покупка по ask одной биржи, продажа по bid другой."""
        if anomaly:
            self._anomalies.add(symbol)
            return
        best = self._best.get(symbol)
        if best is None or gross > best.gross:
            self._best[symbol] = _Best(gross, net, long_exchange, short_exchange, ask, bid)

    def tick(self, compared_pairs: int) -> None:
        self._ticks += 1
        self._max_pairs = max(self._max_pairs, compared_pairs)

    def maybe_report(self, options: BotOptions) -> None:
        """Пора — пишет отчёт за период и начинает новый."""
        md = options.general.market_data
        now = self._clock.now()
        if self._started_at is None:
            self._started_at = now
            return
        if md.spread_report_seconds <= 0:
            self._reset(now)
            return
        if now - self._started_at < timedelta(seconds=md.spread_report_seconds):
            return

        seconds = (now - self._started_at).total_seconds()
        self._log.info(self.build_text(options, seconds))
        self._reset(now)

    def build_text(self, options: BotOptions, seconds: float) -> str:
        threshold = options.arbitrage.min_spread_percent_up
        header = f"Спреды за {seconds:.0f} с (тиков {self._ticks}, сравнимых пар до {self._max_pairs}, порог входа нетто {formatting.pct(threshold)})"

        positive = sorted((item for item in self._best.items() if item[1].gross > ZERO), key=lambda item: item[1].gross, reverse=True)
        if not positive:
            body = "положительных спредов не было"
        else:
            top = positive[: max(1, options.general.market_data.spread_report_top)]
            parts = [
                f"{symbol} {b.long_exchange}→{b.short_exchange} брутто {formatting.pct(b.gross)} / нетто {formatting.pct(b.net)}"
                f" ({formatting.price(b.ask)} → {formatting.price(b.bid)})"
                for symbol, b in top
            ]
            buckets = ", ".join(f"≥{formatting.pct(level)}: {sum(1 for _, b in positive if b.gross >= level)}" for level in GROSS_BUCKETS)
            above = sum(1 for _, b in positive if b.net >= threshold)
            body = f"лучшие: {'; '.join(parts)}. Пар с брутто {buckets}; с нетто выше порога: {above}"

        if self._anomalies:
            body += f". Отброшено как аномалия (>{formatting.pct(options.arbitrage.max_spread_percent)}): {len(self._anomalies)}"
        return f"{header}: {body}"

    def _reset(self, now: datetime) -> None:
        self._started_at = now
        self._best.clear()
        self._anomalies.clear()
        self._ticks = 0
        self._max_pairs = 0
