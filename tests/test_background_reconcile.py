"""
Сверка позиций вне пути входа.

02–03.11.2026: раз в минуту сверка (fetch_positions по каждой бирже, по очереди) шла прямо перед
входом, и стакан проверялся через 1.4–2.4 с после сигнала вместо ~0.5 с — короткий спред к этому
моменту исчезал. Теперь сверка обновляется в фоне (биржи параллельно), вход ждёт только первую
сверку сеанса, а дальше берёт последний результат.
"""

import asyncio
from datetime import timedelta

from futures_arb_bot.core.domain.enums import OrderSide
from futures_arb_bot.core.domain.market_data import PositionSnapshot

from .fakes import D
from .test_position_limits_enforcement import BTC, ETH, NONE, NOW, create_testnet, estimate


def block_fetch_positions(*connectors):
    """Сделать fetch_positions «висящим», пока тест не откроет ворота; возвращает (ворота, счётчик стартов)."""
    gate = asyncio.Event()
    started: list[str] = []
    for connector in connectors:
        original = connector.fetch_positions

        async def blocked(ct=None, _original=original, _id=connector.id):
            started.append(_id)
            await gate.wait()
            return await _original(ct)

        connector.fetch_positions = blocked
    return gate, started


async def wait_until(condition, timeout: float = 1.0) -> None:
    async def poll():
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


async def test_stale_reconcile_does_not_delay_entry():
    executor, long_c, short_c, _ = await create_testnet()
    await executor.process_opportunities([estimate(BTC, D(100), D(101))], NONE)  # первая сверка — с ожиданием
    assert executor.has_open_positions

    gate, started = block_fetch_positions(long_c, short_c)
    executor._last_reconcile_at = NOW - timedelta(minutes=5)  # сверка устарела

    # вход не ждёт «висящую» сверку: ETH открывается, пока fetch_positions ещё не ответил
    await asyncio.wait_for(executor.process_opportunities([estimate(ETH, D(50), D(51))], NONE), 2.0)
    assert len(long_c.placed_orders) == 2
    await wait_until(lambda: len(started) == 2)  # сверка при этом запущена в фоне и ещё висит
    assert not executor._reconcile_task.done()

    gate.set()
    await wait_until(lambda: executor._reconcile_task is not None and executor._reconcile_task.done())


async def test_first_reconcile_is_awaited_and_queries_exchanges_in_parallel():
    executor, long_c, short_c, _ = await create_testnet(max_open_positions=1)
    long_c.open_positions.append(PositionSnapshot(ETH, OrderSide.BUY, D(2), D(50)))
    short_c.open_positions.append(PositionSnapshot(ETH, OrderSide.SELL, D(2), D(51)))
    gate, started = block_fetch_positions(long_c, short_c)

    entry = asyncio.create_task(executor.process_opportunities([estimate(BTC, D(100), D(101))], NONE))
    await wait_until(lambda: len(started) == 2)  # обе биржи спрошены, ни одна ещё не ответила
    assert not entry.done()  # до первой сверки вход ждёт: после рестарта лимиты обязаны знать живые позиции

    gate.set()
    await asyncio.wait_for(entry, 2.0)
    assert not executor.has_open_positions  # слот занят чужой парой, найденной сверкой
    assert long_c.placed_orders == {}


async def test_reconcile_refreshes_in_background_without_signals():
    executor, long_c, short_c, _ = await create_testnet(max_open_positions=1)
    await executor.process_opportunities([], NONE)
    await wait_until(lambda: executor._reconcile_task is not None and executor._reconcile_task.done())

    # «чужая» пара появилась на биржах; сверка устарела, сигналов нет — фон всё равно её обновит
    long_c.open_positions.append(PositionSnapshot(ETH, OrderSide.BUY, D(2), D(50)))
    short_c.open_positions.append(PositionSnapshot(ETH, OrderSide.SELL, D(2), D(51)))
    executor._last_reconcile_at = NOW - timedelta(minutes=5)
    await executor.process_opportunities([], NONE)
    await wait_until(lambda: executor._reconcile_task.done())

    await executor.process_opportunities([estimate(BTC, D(100), D(101))], NONE)
    assert not executor.has_open_positions


async def test_no_second_reconcile_while_one_is_running():
    executor, long_c, short_c, _ = await create_testnet()
    await executor.process_opportunities([], NONE)
    await wait_until(lambda: executor._reconcile_task.done())

    gate, started = block_fetch_positions(long_c, short_c)
    executor._last_reconcile_at = NOW - timedelta(minutes=5)
    await executor.process_opportunities([], NONE)
    await wait_until(lambda: len(started) == 2)
    executor._last_reconcile_at = NOW - timedelta(minutes=10)
    await executor.process_opportunities([], NONE)
    await asyncio.sleep(0.02)
    assert len(started) == 2

    gate.set()
    await wait_until(lambda: executor._reconcile_task.done())
