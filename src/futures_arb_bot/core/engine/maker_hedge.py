"""
Расчёт заявки для входа «maker + хедж»: одна нога заранее стоит лимиткой post-only по цене, при которой
спред с немедленным хеджем второй ноги (taker) даёт целевое нетто; хедж уходит сразу после исполнения.

Мейкер на продаже (дорогая биржа R, хедж — покупка на дешёвой C по ask):
    цена = ask_C × (1 + нужное_брутто / 100), но не ниже ask_R (не пересекаем стакан, post-only).
Мейкер на покупке (дешёвая C, хедж — продажа на R по bid):
    цена = bid_R / (1 + нужное_брутто / 100), но не выше bid_C.
Нужное брутто = целевое нетто + комиссии полного круга (вход maker + taker, выход taker + taker) + буфер.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal

from futures_arb_bot.core.domain.enums import MakerLeg, OrderSide
from futures_arb_bot.core.domain.market_data import ExchangeFees, TickerSnapshot
from futures_arb_bot.core.numbers import HUNDRED, ZERO, round_away

_TEN_THOUSAND = Decimal(10_000)


@dataclass(frozen=True, slots=True)
class MakerQuotePlan:
    """Где и по какой цене стоять мейкером и чем хеджировать."""

    maker_exchange_id: str
    hedge_exchange_id: str
    maker_side: OrderSide
    """SELL — мейкер на шорт-ноге (дорогая биржа), BUY — на лонг-ноге (дешёвая)."""
    price: Decimal
    """Цена заявки-мейкера."""
    hedge_reference: Decimal
    """Котировка хедж-биржи, от которой посчитана цена (ask дешёвой / bid дорогой)."""
    distance_bps: Decimal
    """Насколько заявка дальше своего края стакана (0 — стоит на лучшей цене)."""
    expected_net_percent: Decimal
    """Нетто сделки, если заявка исполнится и хедж пройдёт по hedge_reference."""

    @property
    def hedge_side(self) -> OrderSide:
        return OrderSide.BUY if self.maker_side == OrderSide.SELL else OrderSide.SELL


def round_trip_fees(maker_fees: ExchangeFees, hedge_fees: ExchangeFees, include_fees: bool) -> Decimal:
    """Комиссии полного круга, %: вход — maker + taker, выход (как сейчас) — taker на обеих ногах."""
    if not include_fees:
        return ZERO
    return maker_fees.maker_percent + hedge_fees.taker_percent + maker_fees.taker_percent + hedge_fees.taker_percent


def plan_short_maker(
    cheap: TickerSnapshot,
    rich: TickerSnapshot,
    rich_fees: ExchangeFees,
    cheap_fees: ExchangeFees,
    target_net: Decimal,
    buffer: Decimal,
    include_fees: bool,
) -> MakerQuotePlan | None:
    """Мейкер — продажа на дорогой бирже, хедж — покупка по ask дешёвой."""
    if not cheap.is_tradable or not rich.is_tradable:
        return None
    fees = round_trip_fees(rich_fees, cheap_fees, include_fees)
    required = target_net + fees + buffer
    price = round_away(cheap.ask * (1 + required / HUNDRED), 8)
    price = max(price, rich.ask)  # не пересекаем стакан: post-only на bid отклонится
    distance = (price - rich.ask) / rich.ask * _TEN_THOUSAND
    net = (price - cheap.ask) / cheap.ask * HUNDRED - fees - buffer
    return MakerQuotePlan(rich.exchange_id, cheap.exchange_id, OrderSide.SELL, price, cheap.ask, distance, net)


def plan_long_maker(
    cheap: TickerSnapshot,
    rich: TickerSnapshot,
    cheap_fees: ExchangeFees,
    rich_fees: ExchangeFees,
    target_net: Decimal,
    buffer: Decimal,
    include_fees: bool,
) -> MakerQuotePlan | None:
    """Мейкер — покупка на дешёвой бирже, хедж — продажа по bid дорогой."""
    if not cheap.is_tradable or not rich.is_tradable:
        return None
    fees = round_trip_fees(cheap_fees, rich_fees, include_fees)
    required = target_net + fees + buffer
    price = round_away(rich.bid / (1 + required / HUNDRED), 8)
    price = min(price, cheap.bid)  # не пересекаем стакан: post-only на ask отклонится
    if price <= ZERO:
        return None
    distance = (cheap.bid - price) / cheap.bid * _TEN_THOUSAND
    net = (rich.bid - price) / price * HUNDRED - fees - buffer
    return MakerQuotePlan(cheap.exchange_id, rich.exchange_id, OrderSide.BUY, price, rich.bid, distance, net)


def plan_quote(
    cheap: TickerSnapshot,
    rich: TickerSnapshot,
    cheap_fees: ExchangeFees,
    rich_fees: ExchangeFees,
    *,
    target_net: Decimal,
    buffer: Decimal,
    include_fees: bool,
    leg: MakerLeg,
    allowed_makers: Collection[str],
) -> MakerQuotePlan | None:
    """
    Лучший план из допустимых: нога по настройке maker_leg (Auto — ближе к своему краю стакана) и только
    на биржах из allowed_makers (идентификаторы в нижнем регистре).
    """
    plans: list[MakerQuotePlan] = []
    if leg in (MakerLeg.AUTO, MakerLeg.SHORT) and rich.exchange_id.lower() in allowed_makers:
        plan = plan_short_maker(cheap, rich, rich_fees, cheap_fees, target_net, buffer, include_fees)
        if plan is not None:
            plans.append(plan)
    if leg in (MakerLeg.AUTO, MakerLeg.LONG) and cheap.exchange_id.lower() in allowed_makers:
        plan = plan_long_maker(cheap, rich, cheap_fees, rich_fees, target_net, buffer, include_fees)
        if plan is not None:
            plans.append(plan)
    if not plans:
        return None
    return min(plans, key=lambda p: p.distance_bps)


def replan(
    plan: MakerQuotePlan,
    maker: TickerSnapshot,
    hedge: TickerSnapshot,
    maker_fees: ExchangeFees,
    hedge_fees: ExchangeFees,
    *,
    target_net: Decimal,
    buffer: Decimal,
    include_fees: bool,
) -> MakerQuotePlan | None:
    """Пересчёт цены той же заявки (та же сторона и биржи) по свежим котировкам."""
    if plan.maker_side == OrderSide.SELL:
        return plan_short_maker(hedge, maker, maker_fees, hedge_fees, target_net, buffer, include_fees)
    return plan_long_maker(maker, hedge, maker_fees, hedge_fees, target_net, buffer, include_fees)


def deviation_bps(current: Decimal, fresh: Decimal) -> Decimal:
    """Расхождение двух цен, bps (по модулю)."""
    if current <= ZERO:
        return Decimal(1_000_000)
    return abs(fresh - current) / current * _TEN_THOUSAND


def simulated_fill(plan_side: OrderSide, price: Decimal, maker: TickerSnapshot) -> bool:
    """
    DryRun: исполнилась бы стоящая заявка. Продажа по P — когда лучший bid биржи дошёл до P (покупатель
    готов платить нашу цену), покупка — когда ask опустился до P. Очередь в стакане не учитывается.
    """
    if not maker.is_tradable:
        return False
    if plan_side == OrderSide.SELL:
        return maker.bid >= price
    return maker.ask <= price
