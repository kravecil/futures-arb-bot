"""
Откат неудавшегося открытия: исполнение ноги учитывается один раз (раньше цикл отмены продолжал
крутиться после «исполнена» и удваивал объём: MAGIC 13.10.2026, набрано 47 — откат × 94),
а объём отката не превышает фактическую позицию на бирже.
"""

from dataclasses import replace

from futures_arb_bot.core.domain.enums import OrderSide, OrderStatus, OrderType
from futures_arb_bot.core.domain.market_data import OrderResult, PositionSnapshot

from .fakes import D
from .test_trade_executor_limit import NONE, create_testnet, estimate

BTC = "BTC/USDT:USDT"


def reduce_orders(connector):
    return [o for o in connector.placed_orders.values() if o.reduce_only]


async def test_fill_found_during_abort_is_counted_once():
    executor, long_c, short_c, _ = await create_testnet()
    executor._config.current.arbitrage.execution.type = OrderType.LIMIT

    # лонг выставлен и пока не исполнен; снять его не удаётся, а при проверке он уже исполнен целиком
    long_c.place_order_override = lambda request, index: None if request.reduce_only else OrderResult.ok(f"long-{index}", request.price, D(0))
    long_c.cancel_order_override = lambda _id: False

    def short_fails(_request, _index):
        for order_id, state in list(long_c.order_states.items()):
            long_c.order_states[order_id] = replace(state, status=OrderStatus.FILLED, filled_amount=D(1))
        return OrderResult.fail("нет ликвидности")

    short_c.place_order_override = short_fails

    await executor.process_opportunities([estimate(BTC, D(100), D(101))], NONE)

    [rollback] = reduce_orders(long_c)
    assert rollback.amount == D(1)  # не 2 и не 3
    assert not executor.has_open_positions


async def test_rollback_is_capped_by_actual_exchange_position():
    executor, long_c, short_c, _ = await create_testnet()
    short_c.place_order_override = lambda _r, _i: OrderResult.fail("нет ликвидности")
    long_c.open_positions = [PositionSnapshot(BTC, OrderSide.BUY, D("0.4"), D(100))]  # биржа видит только 0.4

    await executor.process_opportunities([estimate(BTC, D(100), D(101))], NONE)

    [rollback] = reduce_orders(long_c)
    assert rollback.amount == D("0.4")


async def test_rejected_rollback_without_exchange_position_does_not_leave_ghost_position():
    executor, long_c, short_c, _ = await create_testnet()
    long_c.place_order_override = lambda request, index: (
        OrderResult.fail("The Reduce Only order can only decrease the position")
        if request.reduce_only
        else OrderResult.ok(f"open-{index}", request.price, request.amount)
    )
    short_c.place_order_override = lambda _r, _i: OrderResult.fail("нет ликвидности")
    # позиции на бирже нет (fetch_positions → пусто): после отказа reduceOnly откатывать нечего

    await executor.process_opportunities([estimate(BTC, D(100), D(101))], NONE)

    assert len(reduce_orders(long_c)) == 1  # одна попытка, без бесконечных повторов
    assert not executor.has_open_positions  # «призрачная» позиция в учёт не попадает
