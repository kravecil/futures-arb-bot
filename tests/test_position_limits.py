"""Порт PositionLimitsEnforcementTests.cs — лимиты по фактической экспозиции, честное закрытие, strays."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

from fakes import NOW, make_ticker
from testutil import create_harness, estimate

from futures_arb_bot.domain.enums import CloseReason, NetworkMode, OrderSide, OrderStatus
from futures_arb_bot.domain.market_data import OrderResult, PositionSnapshot

D = Decimal

BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"


def create_testnet(max_open_positions: int = 4, max_per_exchange: int = 4):
    harness = create_harness(mode=NetworkMode.Testnet, timeout_ms=250, poll_ms=50)
    harness.Config.Current.Arbitrage.MaxOpenPositions = max_open_positions
    harness.Config.Current.Arbitrage.MaxPositionsPerExchange = max_per_exchange
    harness.Config.Current.Arbitrage.RebalanceTolerancePercent = D("1.0")
    return harness


def est(symbol: str, long_price: Decimal, short_price: Decimal):
    return estimate(long_price, short_price, symbol=symbol)


def tickers_map(harness) -> dict:
    return {harness.Long.Id: harness.Long.Tickers, harness.Short.Id: harness.Short.Tickers}


def run_open(harness, *estimates):
    asyncio.run(harness.Executor.ProcessOpportunitiesAsync(list(estimates), None))


def test_max_open_positions_1_blocks_the_second_arbitrage():
    harness = create_testnet(max_open_positions=1)

    run_open(harness, est(BTC, D("100"), D("101")), est(ETH, D("50"), D("51")))

    assert harness.Executor.HasOpenPositions
    assert len(harness.Long.PlacedOrders) == 1  # открыт только первый кандидат
    assert len(harness.Short.PlacedOrders) == 1


def test_max_positions_per_exchange_1_blocks_the_second_arbitrage():
    harness = create_testnet(max_open_positions=4, max_per_exchange=1)

    run_open(harness, est(BTC, D("100"), D("101")), est(ETH, D("50"), D("51")))

    # обе биржи участвуют в каждом арбитраже: после BTC слоты исчерпаны
    assert len(harness.Long.PlacedOrders) == 1
    assert len(harness.Short.PlacedOrders) == 1


def test_positions_left_on_exchanges_after_restart_consume_max_open_positions():
    harness = create_testnet(max_open_positions=1)

    # «прошлый сеанс» оставил ETH-арбитраж на обеих биржах — этот процесс о нём не знает
    harness.Long.OpenPositions.append(PositionSnapshot(ETH, OrderSide.Buy, D("2"), D("50")))
    harness.Short.OpenPositions.append(PositionSnapshot(ETH, OrderSide.Sell, D("2"), D("51")))

    run_open(harness, est(BTC, D("100"), D("101")))

    # лимит 1 уже занят чужой парой позиций — нового арбитража быть не должно
    assert not harness.Executor.HasOpenPositions
    assert not harness.Long.PlacedOrders
    assert not harness.Short.PlacedOrders


def test_foreign_leg_on_one_exchange_consumes_its_slot():
    harness = create_testnet(max_open_positions=4, max_per_exchange=1)

    # на bybit живёт «чужой» шорт: арбитража (2+ биржи) нет, но слот биржи занят
    harness.Short.OpenPositions.append(PositionSnapshot(ETH, OrderSide.Sell, D("2"), D("51")))

    run_open(harness, est(BTC, D("100"), D("101")))

    assert not harness.Long.PlacedOrders
    assert not harness.Short.PlacedOrders


def test_session_positions_are_not_double_counted():
    harness = create_testnet(max_open_positions=4, max_per_exchange=2)

    # биржи на запросе сверки отдают нашу же BTC-позицию — вторым слотом она не считается
    harness.Long.OpenPositions.append(PositionSnapshot(BTC, OrderSide.Buy, D("1"), D("100")))
    harness.Short.OpenPositions.append(PositionSnapshot(BTC, OrderSide.Sell, D("1"), D("101")))

    run_open(harness, est(BTC, D("100"), D("101")))
    assert harness.Executor.HasOpenPositions

    run_open(harness, est(ETH, D("50"), D("51")))

    # ETH открылся: 1 сеансовая позиция + 0 внешних (BTC — наша же, дедуплицирована)
    assert len(harness.Long.PlacedOrders) == 2
    assert len(harness.Short.PlacedOrders) == 2


def test_reconcile_failure_does_not_block_trading():
    harness = create_testnet(max_open_positions=1)
    harness.Long.FailFetchPositions = True
    harness.Short.FailFetchPositions = True

    run_open(harness, est(BTC, D("100"), D("101")))

    # сверка упала — работаем по позициям сеанса, торговля не встала
    assert harness.Executor.HasOpenPositions
    assert len(harness.Long.PlacedOrders) == 1


def test_market_close_without_confirmation_keeps_position_in_tracking():
    harness = create_testnet(max_open_positions=1)

    run_open(harness, est(BTC, D("100"), D("101")))
    assert harness.Executor.HasOpenPositions

    # стоп-лосс: спред расширился выше порога
    harness.Long.Tickers = {BTC: make_ticker("binanceusdm", BTC, D("104.9"), D("105"))}
    harness.Short.Tickers = {BTC: make_ticker("bybit", BTC, D("111"), D("111.2"))}

    # закрытие лонга «принято, но без объёма», и на бирже лонг по-прежнему живёт
    def long_override(request, index):
        if request.ReduceOnly:
            return OrderResult.Ok(f"unfilled-{index}", None, D("0"))
        return None

    harness.Long.PlaceOrderOverride = long_override
    harness.Long.OpenPositions.append(PositionSnapshot(BTC, OrderSide.Buy, D("1"), D("100")))

    async def manage():
        await harness.Executor.ManageOpenPositionsAsync(tickers_map(harness), None)

    asyncio.run(manage())

    # прежнее поведение считало такую ногу закрытой целиком и снимало позицию с учёта,
    # освобождая слот лимита при живой экспозиции на бирже
    assert harness.Executor.HasOpenPositions
    assert not harness.Stats.Snapshot(NOW + timedelta(minutes=1)).ClosedPositions

    # биржа подтвердила отсутствие позиции — закрытие довершается на следующем тике
    harness.Long.OpenPositions.clear()
    asyncio.run(manage())

    assert not harness.Executor.HasOpenPositions
    closed = harness.Stats.Snapshot(NOW + timedelta(minutes=1)).ClosedPositions
    assert len(closed) == 1
    assert closed[0].Reason == CloseReason.StopLoss


def test_uncancelled_leg_is_not_forgotten_and_keeps_being_cancelled():
    harness = create_testnet()

    # лонг выставлен и живёт в стакане, шорт отклонён → откат открытия,
    # но отмена лонга первые попытки не проходит
    def long_override(request, index):
        if request.ReduceOnly:
            return None
        return OrderResult.Ok(f"stuck-{index}", request.Price, D("0"))

    harness.Long.PlaceOrderOverride = long_override
    harness.Short.PlaceOrderOverride = lambda request, index: OrderResult.Fail("нет ликвидности")
    harness.Long.CancelOrderOverride = lambda order_id: False

    run_open(harness, est(BTC, D("100"), D("101")))

    # позиция не открыта — но и заявка не «забыта»: она всё ещё в стакане
    assert not harness.Executor.HasOpenPositions
    assert harness.Long.OrderStates["stuck-1"].Status == OrderStatus.Open
    assert not harness.Long.CancelledOrders

    async def manage():
        await harness.Executor.ManageOpenPositionsAsync(tickers_map(harness), None)

    asyncio.run(manage())
    asyncio.run(manage())

    # цикл ведения не бросает заявку: пока отмена не проходит, она помнится и опрашивается
    assert harness.Long.OrderStates["stuck-1"].Status == OrderStatus.Open
    assert not harness.Long.CancelledOrders

    # отмена прошла — заявка снята, цикл ведения её отпускает
    harness.Long.CancelOrderOverride = None
    asyncio.run(manage())

    assert harness.Long.OrderStates["stuck-1"].Status == OrderStatus.Canceled
    assert len(harness.Long.CancelledOrders) == 1
