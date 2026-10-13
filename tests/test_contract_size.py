"""
Размер контракта: на OKX/Gate ccxt принимает и возвращает объём в контрактах, а ядро считает в монетах.
Коннектор обязан переводить объём в обе стороны — иначе нога на OKX (ctVal = 10) в 10 раз больше
ноги на Bybit, и «арбитраж» превращается в голую позицию (сделка S/USDT 13.10.2026).
"""

import ccxt

from futures_arb_bot.core.clock import SystemClock
from futures_arb_bot.core.domain.enums import NetworkMode, OrderSide, OrderType
from futures_arb_bot.core.domain.market_data import OrderRequest
from futures_arb_bot.core.domain.options import ExchangeConfigEntry
from futures_arb_bot.infrastructure import ccxt_connector
from futures_arb_bot.infrastructure.ccxt_connector import CcxtExchangeConnector

from .fakes import D, make_log

S = "S/USDT:USDT"
BTC = "BTC/USDT:USDT"


class ContractsApi:
    """Биржа, считающая объём в контрактах: S — 10 монет в контракте, шаг 1 контракт; BTC — 0.01 BTC, шаг 0.1."""

    precisionMode = ccxt.TICK_SIZE

    def __init__(self) -> None:
        self.has: dict = {}
        self.created: list[tuple[str, float]] = []
        self.filled_contracts = 28.0

    async def load_markets(self):
        base = {"swap": True, "linear": True, "active": True, "taker": 0.0005, "maker": 0.0002, "quote": "USDT"}
        return {
            S: {**base, "contractSize": 10.0, "limits": {"amount": {"min": 1.0}}, "precision": {"amount": 1.0}},
            BTC: {**base, "contractSize": 0.01, "limits": {"amount": {"min": 0.1}}, "precision": {"amount": 0.1}},
        }

    def amount_to_precision(self, symbol, amount):
        step = 1.0 if symbol == S else 0.1
        return str(int(amount / step + 1e-9) * step)  # ccxt усекает вниз

    def price_to_precision(self, symbol, price):
        return str(price)

    async def create_order(self, symbol, type_, side, amount, price, params):
        self.created.append((symbol, amount))
        return {"id": "1", "status": "closed", "filled": self.filled_contracts, "average": 0.0707, "symbol": symbol}

    async def fetch_order(self, order_id, symbol):
        return {"id": order_id, "status": "closed", "filled": self.filled_contracts, "average": 0.0707, "symbol": symbol}

    async def fetch_positions(self):
        return [{"symbol": S, "contracts": 28.0, "side": "short", "entryPrice": 0.0707}]

    async def fetch_order_book(self, symbol, limit=None):
        return {"bids": [[0.0706, 5.0], [0.0705, 100.0]], "asks": [[0.0707, 3.0]], "timestamp": None}

    async def close(self):
        pass


def make_connector(monkeypatch, api: ContractsApi) -> CcxtExchangeConnector:
    monkeypatch.setattr(ccxt_connector, "instantiate", lambda _id, _cfg: api)
    return CcxtExchangeConnector(ExchangeConfigEntry(id="okx", name="OKX"), NetworkMode.LIVE, make_log(), SystemClock())


async def test_market_info_limits_are_in_coins(monkeypatch):
    connector = make_connector(monkeypatch, ContractsApi())
    await connector.connect()

    markets = {m.symbol: m for m in connector.perpetual_markets}
    assert markets[S].contract_size == D(10)
    assert markets[S].min_amount == D(10)  # 1 контракт = 10 монет
    assert markets[S].amount_step == D(10)
    assert markets[BTC].min_amount == D("0.001")
    assert markets[BTC].amount_step == D("0.001")


async def test_order_amount_is_sent_in_contracts_and_fill_returned_in_coins(monkeypatch):
    api = ContractsApi()
    connector = make_connector(monkeypatch, api)
    await connector.connect()

    result = await connector.place_order(OrderRequest(S, OrderSide.SELL, D("286.6"), type=OrderType.MARKET))

    # 286.6 монеты = 28.66 контракта → биржа получает 28 контрактов, а не 286
    assert api.created == [(S, 28.0)]
    assert result.success and result.filled_amount == D(280)

    update = await connector.fetch_order("1", S, fresh=True)
    assert update is not None and update.filled_amount == D(280)


async def test_positions_and_order_book_are_in_coins(monkeypatch):
    connector = make_connector(monkeypatch, ContractsApi())
    await connector.connect()

    [position] = await connector.fetch_positions()
    assert position.side == OrderSide.SELL and position.amount == D(280)

    book = await connector.fetch_order_book(S, 20)
    assert book is not None
    assert book.bids[0] == (D("0.0706"), D(50))
    # 100 монет = 5 + 95 из второго уровня → худшая цена набора — второй уровень
    assert book.sweep_price(OrderSide.SELL, D(100)) == D("0.0705")
    # на покупку 31 монеты глубины (30) не хватает
    assert book.sweep_price(OrderSide.BUY, D(31)) is None


async def test_exchange_without_contract_size_keeps_amounts(monkeypatch):
    api = ContractsApi()
    original = api.load_markets

    async def coin_markets():
        markets = await original()
        for market in markets.values():
            market.pop("contractSize")
        return markets

    api.load_markets = coin_markets
    connector = make_connector(monkeypatch, api)
    await connector.connect()

    assert connector.contract_size(S) == D(1)
    await connector.place_order(OrderRequest(S, OrderSide.SELL, D("286.6"), type=OrderType.MARKET))
    assert api.created == [(S, 286.0)]
