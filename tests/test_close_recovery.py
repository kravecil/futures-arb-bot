"""
Закрытие позиции, которое «зависло» (SAND 18.10.2026): market-закрытие шорта на Bybit вернуло статус
Unknown, хотя исполнилось; дальше каждая reduce-only заявка отклонялась (позиции уже нет), а бот 2 часа
держал фантомную позицию, занимавшую слоты обеих бирж, и повторял закрытие ~10 раз в секунду.
"""

from datetime import timedelta

import ccxt

from futures_arb_bot.core.domain.enums import AppLogLevel, OrderSide
from futures_arb_bot.core.domain.market_data import OrderResult, PositionSnapshot
from futures_arb_bot.infrastructure.ccxt_connector import exchange_error_detail, explain_error

from .fakes import D, ManualClock, ticker
from .test_trade_executor_limit import NONE, NOW, create_testnet, estimate, set_neutral_tickers, tickers

BTC = "BTC/USDT:USDT"
REJECTED = "ордер отклонён биржей (объём или цена): 110017 current position is zero, cannot fix reduce-only order qty"


async def open_position():
    executor, long_c, short_c, stats = await create_testnet()
    executor._position_settle_delay = 0
    await executor.process_opportunities([estimate(BTC, D(100), D(101))], NONE)
    assert executor.has_open_positions
    clock = ManualClock(NOW + timedelta(minutes=5))
    executor._clock = clock  # паузы между повторами проверяем по ручным часам
    return executor, long_c, short_c, stats, clock


def set_stop_loss_tickers(long_c, short_c) -> None:
    # брутто 3% ≥ стоп-лосса 2.5% — бот начинает закрытие
    long_c.tickers = {BTC: ticker("binanceusdm", BTC, bid=D("99.9"), ask=D(100))}
    short_c.tickers = {BTC: ticker("bybit", BTC, bid=D(103), ask=D("103.1"))}


def reject_reduce_only(request, _index):
    return OrderResult.fail(REJECTED) if request.reduce_only else None


def close_orders(connector):
    return [o for o in connector.placed_orders.values() if o.reduce_only]


async def test_rejected_close_without_exchange_position_completes_the_position():
    executor, long_c, short_c, _, _ = await open_position()
    short_c.place_order_override = reject_reduce_only  # на бирже шорта уже нет (fetch_positions пуст)
    set_stop_loss_tickers(long_c, short_c)

    await executor.manage_open_positions(tickers(long_c, short_c), NONE)

    assert not executor.has_open_positions  # фантом не остаётся и слоты бирж освобождаются
    assert len(close_orders(short_c)) == 1


async def test_rejected_close_with_partial_exchange_position_keeps_only_the_remainder():
    executor, long_c, short_c, _, clock = await open_position()
    size = executor._positions[BTC].short_size
    short_c.place_order_override = reject_reduce_only
    short_c.open_positions = [PositionSnapshot(BTC, OrderSide.SELL, size / 4, D(101))]
    set_stop_loss_tickers(long_c, short_c)

    await executor.manage_open_positions(tickers(long_c, short_c), NONE)

    position = executor._positions[BTC]
    assert position.short_size == size / 4  # в учёте остаётся только то, что реально есть на бирже

    short_c.place_order_override = None
    short_c.open_positions = []
    clock.advance(timedelta(seconds=1))
    await executor.manage_open_positions(tickers(long_c, short_c), NONE)

    assert not executor.has_open_positions
    assert close_orders(short_c)[-1].amount == size / 4


async def test_failed_close_is_retried_with_backoff_not_every_tick():
    executor, long_c, short_c, _, clock = await open_position()
    size = executor._positions[BTC].short_size
    short_c.place_order_override = reject_reduce_only
    short_c.open_positions = [PositionSnapshot(BTC, OrderSide.SELL, size, D(101))]  # шорт на бирже есть
    set_stop_loss_tickers(long_c, short_c)

    await executor.manage_open_positions(tickers(long_c, short_c), NONE)
    assert len(close_orders(short_c)) == 1

    # следующие тики до истечения паузы (1 с) заявок не выставляют
    for _ in range(5):
        clock.advance(timedelta(milliseconds=100))
        await executor.manage_open_positions(tickers(long_c, short_c), NONE)
    assert len(close_orders(short_c)) == 1

    clock.advance(timedelta(milliseconds=600))
    await executor.manage_open_positions(tickers(long_c, short_c), NONE)
    assert len(close_orders(short_c)) == 2

    # пауза растёт: 2 с после второй неудачи, но не больше 30 с
    clock.advance(timedelta(seconds=1))
    await executor.manage_open_positions(tickers(long_c, short_c), NONE)
    assert len(close_orders(short_c)) == 2
    for _ in range(20):
        clock.advance(timedelta(seconds=31))
        await executor.manage_open_positions(tickers(long_c, short_c), NONE)
    assert executor._close_attempts[BTC].retry_at - clock.now() <= timedelta(seconds=30)

    alerts = [e for e in executor._log.latest(500, AppLogLevel.ERROR) if "ВНИМАНИЕ" in e.message]
    assert len(alerts) == 1  # громкое предупреждение после 10 неудач подряд, без спама
    assert executor.has_open_positions  # шорт реально есть — позицию не теряем


async def test_half_closed_position_keeps_closing_after_spread_normalizes():
    executor, long_c, short_c, _, clock = await open_position()
    size = executor._positions[BTC].short_size
    short_c.place_order_override = reject_reduce_only
    short_c.open_positions = [PositionSnapshot(BTC, OrderSide.SELL, size, D(101))]
    set_stop_loss_tickers(long_c, short_c)
    await executor.manage_open_positions(tickers(long_c, short_c), NONE)
    assert executor._positions[BTC].long_closed and not executor._positions[BTC].short_closed

    # спред вернулся в норму — условие выхода пропало, но голый шорт всё равно надо закрыть
    set_neutral_tickers(long_c, short_c)
    short_c.place_order_override = None
    short_c.open_positions = []
    clock.advance(timedelta(seconds=2))
    await executor.manage_open_positions(tickers(long_c, short_c), NONE)

    assert not executor.has_open_positions


async def test_unknown_status_close_trusts_the_settled_exchange_position():
    executor, long_c, short_c, _, _ = await open_position()
    size = executor._positions[BTC].short_size
    # заявка принята, но статус/объём не подтверждены; позиция на бирже исчезает со второй сверки
    short_c.place_order_override = lambda request, index: OrderResult.ok(f"close-{index}", None, D(0)) if request.reduce_only else None
    snapshots = [[PositionSnapshot(BTC, OrderSide.SELL, size, D(101))], []]

    async def lagging_positions(ct=None):
        return snapshots.pop(0) if snapshots else []

    short_c.fetch_positions = lagging_positions
    set_stop_loss_tickers(long_c, short_c)

    await executor.manage_open_positions(tickers(long_c, short_c), NONE)

    assert not executor.has_open_positions
    assert len(close_orders(short_c)) == 1


async def test_close_start_is_logged_with_reason():
    executor, long_c, short_c, _, _ = await open_position()
    set_stop_loss_tickers(long_c, short_c)

    await executor.manage_open_positions(tickers(long_c, short_c), NONE)

    messages = [e.message for e in executor._log.latest(100)]
    assert any(m.startswith(f"Закрываю арбитраж {BTC} (стоп-лосс)") for m in messages)


def test_invalid_order_error_keeps_exchange_code_and_message():
    bybit = ccxt.InvalidOrder('bybit {"retCode":110017,"retMsg":"current position is zero, cannot fix reduce-only order qty","result":{},"time":1}')
    okx = ccxt.InvalidOrder('okx {"code":"1","data":[{"sCode":"51169","sMsg":"Order failed because you don\'t have any positions"}],"msg":""}')

    assert explain_error(bybit) == "ордер отклонён биржей (объём или цена): 110017 current position is zero, cannot fix reduce-only order qty"
    assert exchange_error_detail(okx) == "51169 Order failed because you don't have any positions"
    assert explain_error(ccxt.InvalidOrder("")) == "ордер отклонён биржей (объём или цена)"
