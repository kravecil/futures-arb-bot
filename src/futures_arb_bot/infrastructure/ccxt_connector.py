"""
Адаптер одной биржи к интерфейсу ExchangeConnector через асинхронный CCXT:
загрузка рынков (только линейные бессрочные), опрос тикеров, заявки, позиции, фандинг.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import ccxt

from futures_arb_bot.core.abstractions import EventLog
from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.clock import Clock
from futures_arb_bot.core.domain.enums import NetworkMode, OrderSide, OrderStatus, OrderType
from futures_arb_bot.core.domain.market_data import (
    ExchangeFees,
    FetchTickersResult,
    MarketInfo,
    OrderRequest,
    OrderResult,
    OrderUpdate,
    PositionSnapshot,
    TickerSnapshot,
)
from futures_arb_bot.core.domain.options import ExchangeConfigEntry
from futures_arb_bot.core.domain.order_execution import ExchangeCapabilityMap, OrderParamsBuilder
from futures_arb_bot.core.engine.logging_bridge import get_logger
from futures_arb_bot.core.numbers import HUNDRED, ZERO, dec_str, to_dec
from futures_arb_bot.infrastructure.ccxt_factory import instantiate

FUNDING_CACHE_TTL = timedelta(minutes=5)
"""Фандинг-рейты обновляются раз в 4–8 часов, REST-эндпоинт тяжёлый — кэшируем."""

_FALLBACK_FEES = ExchangeFees(Decimal("0.1"), Decimal("0.05"))


def _check(ct: CancellationToken | None) -> None:
    if ct is not None:
        ct.throw_if_cancellation_requested()


class CcxtExchangeConnector:
    def __init__(self, entry: ExchangeConfigEntry, mode: NetworkMode, log: EventLog, clock: Clock) -> None:
        self._entry = entry
        self._mode = mode
        self._log = log
        self._clock = clock
        self._logger = get_logger(f"Ccxt.{entry.id}")
        self._markets: dict[str, MarketInfo] = {}
        self._fees_by_symbol: dict[str, ExchangeFees] = {}
        self._default_fees = _FALLBACK_FEES
        self._funding_cache: dict[str, Decimal] = {}
        self._funding_fetched_at: datetime | None = None
        self._funding_unavailable_logged = False
        self._leverage_warned: set[str] = set()

        config: dict[str, Any] = {"enableRateLimit": True, "timeout": 15000}

        api_key = _first_not_empty(entry.api_key, _env_override(entry.id, "API_KEY"), _env_override(entry.id, "APIKEY"))
        secret = _first_not_empty(entry.secret, _env_override(entry.id, "SECRET"))
        password = _first_not_empty(entry.password, _env_override(entry.id, "PASSWORD"))
        if api_key is not None:
            config["apiKey"] = api_key
        if secret is not None:
            config["secret"] = secret
        if password is not None:
            config["password"] = password

        # биржи, где надо явно указать тип рынка (иначе fetch_tickers вернёт спот)
        options = _default_options(entry.id)
        if entry.options:
            options.update(entry.options)
        if options:
            config["options"] = options

        # какие опции реально ушли в CCXT — без этого «ноль фьючерсных рынков» не диагностируется
        self._ccxt_options = ", ".join(f"{k}={_option_text(v)}" for k, v in options.items()) if options else "нет"
        self._api = instantiate(entry.id, config)

    # ------------------------- свойства -------------------------

    @property
    def id(self) -> str:
        return self._entry.id

    @property
    def display_name(self) -> str:
        return self._entry.name if self._entry.name.strip() else self._entry.id

    @property
    def mode(self) -> NetworkMode:
        return self._mode

    @property
    def default_fees(self) -> ExchangeFees:
        return self._default_fees

    @property
    def market_count(self) -> int:
        return len(self._markets)

    @property
    def perpetual_markets(self) -> list[MarketInfo]:
        return list(self._markets.values())

    # ------------------------- подключение и данные -------------------------

    async def connect(self, ct: CancellationToken | None = None) -> None:
        _check(ct)

        # testnet включается ДО загрузки рынков — у него свои URL и списки инструментов
        if self._mode == NetworkMode.TESTNET:
            self._api.set_sandbox_mode(True)

        markets: Mapping[str, Any] = await self._api.load_markets()
        self._markets.clear()
        self._fees_by_symbol.clear()

        for symbol, market in markets.items():
            if market.get("swap") is not True or market.get("linear") is not True or market.get("active") is False:
                continue

            taker_pct = _to_pct(market.get("taker"))
            maker_pct = _to_pct(market.get("maker"))

            min_amount: float | None = None
            try:
                min_amount = ((market.get("limits") or {}).get("amount") or {}).get("min")
            except Exception:
                pass  # у части бирж limits не заполнен — работаем без ограничений

            self._markets[symbol] = MarketInfo(
                symbol,
                is_perpetual=True,
                is_linear=True,
                quote_currency=market.get("quote") or market.get("settle") or "USDT",
                min_amount=to_dec(min_amount) if isinstance(min_amount, (int, float)) and min_amount > 0 else None,
                taker_percent=taker_pct,
                maker_percent=maker_pct,
            )

            if taker_pct > ZERO:
                self._fees_by_symbol[symbol] = ExchangeFees(taker_pct, maker_pct)

        # комиссия по умолчанию: переопределение из конфига → BTC/USDT:USDT → любой рынок → (0.1 / 0.05)
        reference = self._fees_by_symbol.get("BTC/USDT:USDT") or next(iter(self._fees_by_symbol.values()), ExchangeFees(ZERO, ZERO))
        self._default_fees = ExchangeFees(
            self._entry.taker_fee_percent
            if self._entry.taker_fee_percent is not None
            else (reference.taker_percent if reference.taker_percent > ZERO else Decimal("0.1")),
            self._entry.maker_fee_percent
            if self._entry.maker_fee_percent is not None
            else (reference.maker_percent if reference.maker_percent > ZERO else Decimal("0.05")),
        )

        self._log.debug(
            f"[{self.display_name}] опции CCXT: {self._ccxt_options}; инструментов загружено {len(markets)}, линейных бессрочных {len(self._markets)}"
        )
        if not self._markets:
            self._log.warning(
                f"[{self.display_name}] CCXT не вернул ни одного линейного бессрочного фьючерса — проверьте defaultType в разделе options биржи"
            )

    async def fetch_tickers(self, ct: CancellationToken | None = None) -> FetchTickersResult:
        _check(ct)

        started = time.perf_counter()
        raw: dict[str, Any] = await self._api.fetch_tickers()

        # часть бирж (например, Binance) отдаёт bid/ask отдельным запросом bookTicker — дослияем
        try:
            bids_asks: Mapping[str, Any] = await self._api.fetch_bids_asks()
            for symbol, book_ticker in bids_asks.items():
                ticker = raw.get(symbol)
                if ticker is None:
                    continue
                bid = book_ticker.get("bid")
                if isinstance(bid, (int, float)) and bid > 0:
                    ticker["bid"] = bid
                ask = book_ticker.get("ask")
                if isinstance(ask, (int, float)) and ask > 0:
                    ticker["ask"] = ask
        except (ccxt.NotSupported, ccxt.OperationFailed, ccxt.ExchangeError) as ex:
            self._logger.debug("FetchBidsAsks недоступен — работаем на bid/ask из fetch_tickers", exc_info=ex)

        now = self._clock.now()
        result: dict[str, TickerSnapshot] = {}
        for symbol, ticker in raw.items():
            if symbol not in self._markets:
                continue  # интересуют только наши линейные бессрочные

            bid = ticker.get("bid")
            ask = ticker.get("ask")
            if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)) or bid <= 0 or ask <= 0:
                continue

            ms = ticker.get("timestamp")
            timestamp = datetime.fromtimestamp(ms / 1000, UTC) if isinstance(ms, (int, float)) and ms > 0 else now

            quote_volume = ticker.get("quoteVolume")
            quote_volume = float(quote_volume) if isinstance(quote_volume, (int, float)) else 0.0
            if quote_volume <= 0 and ticker.get("info") is not None:
                # некоторые биржи (например OKX) не парсят объём в тикер — достаём из сырых данных
                quote_volume = _extract_quote_volume(ticker["info"])

            last = ticker.get("last")
            result[symbol] = TickerSnapshot(
                self.id,
                symbol,
                to_dec(bid),
                to_dec(ask),
                to_dec(last if isinstance(last, (int, float)) else bid),
                to_dec(quote_volume),
                timestamp,
            )

        return FetchTickersResult(result, timedelta(seconds=time.perf_counter() - started))

    # ------------------------- заявки -------------------------

    async def place_order(self, request: OrderRequest, ct: CancellationToken | None = None) -> OrderResult:
        try:
            _check(ct)

            amount = float(self._api.amount_to_precision(request.symbol, float(request.amount)))
            if amount <= 0:
                return OrderResult.fail(f"объём после округления равен нулю ({dec_str(request.amount)})")

            # имена и условия параметров биржи (перевод политики заявки) — в OrderParamsBuilder
            parameters = OrderParamsBuilder.build(request, ExchangeCapabilityMap.for_id(self.id))

            price: float | None = None
            # ChaseLimit на бирже — это limit: локальное догонание делает исполнитель, нативное — биржа
            order_type = "market" if request.type == OrderType.MARKET else "limit"
            if request.type != OrderType.MARKET:
                if request.price is None or request.price <= ZERO:
                    return OrderResult.fail("для лимитного ордера не задана цена")
                price = float(self._api.price_to_precision(request.symbol, float(request.price)))

            side = "buy" if request.side == OrderSide.BUY else "sell"
            order = await self._api.create_order(request.symbol, order_type, side, amount, price, parameters)

            filled_raw = order.get("filled")
            filled = to_dec(filled_raw) if isinstance(filled_raw, (int, float)) and filled_raw > 0 else ZERO
            average = _positive(order.get("average"))
            if average is None:
                average = _positive(order.get("price"))

            # рыночный обязан исполниться сразу; лимитный мог ещё не набрать объёма
            status = order.get("status")
            if request.type == OrderType.MARKET and status in ("open", "rejected") and filled <= ZERO:
                return OrderResult.fail(f"рыночный ордер не исполнен (статус: {status or 'unknown'})")

            order_id = order.get("id")
            fee = fee_in_quote(order, request.symbol, average) if filled > ZERO else None
            return OrderResult.ok(str(order_id) if order_id is not None else None, average, filled, fee)
        except Exception as ex:
            if _is_cancel(ex):
                raise
            message = explain_error(ex)
            self._log.error(f"[{self.display_name}] ордер {request.side} {request.symbol} × {dec_str(request.amount)}: {message}")
            self._logger.debug("order failed", exc_info=ex)
            return OrderResult.fail(message)

    async def fetch_order(self, order_id: str, symbol: str, ct: CancellationToken | None = None) -> OrderUpdate | None:
        try:
            _check(ct)
            order = await self._api.fetch_order(order_id, symbol)
            filled_raw = order.get("filled")
            filled = to_dec(filled_raw) if isinstance(filled_raw, (int, float)) and filled_raw > 0 else ZERO
            average = _positive(order.get("average"))
            fee = fee_in_quote(order, symbol, average) if filled > ZERO else None
            return OrderUpdate(order_id, map_status(order.get("status")), filled, average, fee)
        except ccxt.OrderNotFound:
            return None
        except Exception as ex:
            if _is_cancel(ex):
                raise
            self._logger.debug(f"FetchOrder {order_id} {symbol} failed", exc_info=ex)
            # ошибку опроса не считаем смертью ордера: вернём «неизвестно» с нулевым исполнением
            return OrderUpdate(order_id, OrderStatus.UNKNOWN, ZERO, None)

    async def cancel_order(self, order_id: str, symbol: str, ct: CancellationToken | None = None) -> bool:
        try:
            _check(ct)
            await self._api.cancel_order(order_id, symbol)
            return True
        except Exception as ex:
            if _is_cancel(ex):
                raise
            self._logger.debug(f"CancelOrder {order_id} {symbol} failed", exc_info=ex)
            return False

    async def fetch_positions(self, ct: CancellationToken | None = None) -> list[PositionSnapshot]:
        # исключения не глотаем: вызывающий код сам решает, что делать при недоступности сверки
        _check(ct)
        positions = await self._api.fetch_positions()

        result: list[PositionSnapshot] = []
        for position in positions:
            symbol = position.get("symbol")
            if not symbol:
                continue
            contracts = position.get("contracts")
            contracts = float(contracts) if isinstance(contracts, (int, float)) else 0.0
            if contracts <= 0:
                continue  # закрытые позиции биржи отдают нулевыми — экспозиции в них нет

            side = OrderSide.SELL if position.get("side") == "short" else OrderSide.BUY
            result.append(PositionSnapshot(symbol, side, to_dec(contracts), _positive(position.get("entryPrice"))))
        return result

    async def fetch_funding_rates_percent(self, ct: CancellationToken | None = None) -> Mapping[str, Decimal]:
        now = self._clock.now()
        if self._funding_fetched_at is not None and now - self._funding_fetched_at < FUNDING_CACHE_TTL:
            return self._funding_cache

        self._funding_fetched_at = now
        try:
            _check(ct)
            response: Mapping[str, Any] = await self._api.fetch_funding_rates()
            rates: dict[str, Decimal] = {}
            for symbol, funding in response.items():
                rate = funding.get("fundingRate") if isinstance(funding, Mapping) else None
                if isinstance(rate, (int, float)) and symbol in self._markets:
                    rates[symbol] = to_dec(rate) * HUNDRED  # дробь CCXT (0.0001) → проценты (0.01 %)
            self._funding_cache = rates
            self._funding_unavailable_logged = False
        except Exception as ex:
            if _is_cancel(ex):
                raise
            # fail-open: без данных фандинга сделки не блокируются — предупредим один раз до первого успеха
            if not self._funding_unavailable_logged:
                self._funding_unavailable_logged = True
                self._log.error(f"[{self.display_name}] фандинг-рейты недоступны: {explain_error(ex)} — фильтр по фандингу будет пропускать сделки")
                self._logger.debug(f"FetchFundingRates failed for {self.id}", exc_info=ex)

        return self._funding_cache

    async def set_leverage(self, leverage: int, symbol: str, ct: CancellationToken | None = None) -> None:
        if leverage <= 1:
            return
        try:
            # BingX требует side (LONG|SHORT|BOTH): счёт робота — в one-way режиме, поэтому BOTH
            extra = {"side": "BOTH"} if self.id.lower() == "bingx" else {}
            await self._api.set_leverage(leverage, symbol, extra)
        except Exception as ex:
            if _is_cancel(ex):
                raise
            self._logger.debug(f"SetLeverage {symbol} x{leverage} — биржа могла не применить плечо", exc_info=ex)
            if symbol not in self._leverage_warned:
                self._leverage_warned.add(symbol)
                self._log.warning(
                    f"[{self.display_name}] плечо ×{leverage} для {symbol} не применено: {explain_error(ex)} — используется значение по умолчанию биржи"
                )

    async def verify_access(self, ct: CancellationToken | None = None) -> bool:
        try:
            await self._api.fetch_balance()
            return True
        except Exception as ex:
            if _is_cancel(ex):
                raise
            self._log.error(f"[{self.display_name}] проверка торгового доступа не прошла: {explain_error(ex)}")
            return False

    def try_get_fees(self, symbol: str) -> tuple[bool, ExchangeFees]:
        found = self._fees_by_symbol.get(symbol)
        if found is not None and found.taker_percent > ZERO:
            return True, found
        return False, self._default_fees

    async def aclose(self) -> None:
        await self._api.close()


# ------------------------- вспомогательное -------------------------


def map_status(status: str | None) -> OrderStatus:
    match status:
        case "open" | "pending" | "unfilled" | "partially_filled" | "partially-closed":
            return OrderStatus.OPEN
        case "closed" | "canceled_filled" | "filled":
            return OrderStatus.FILLED
        case "canceled":
            return OrderStatus.CANCELED
        case "expired":
            return OrderStatus.EXPIRED
        case "rejected":
            return OrderStatus.REJECTED
        case _:
            return OrderStatus.UNKNOWN


def fee_in_quote(order: Mapping[str, Any], symbol: str, average: Decimal | None) -> Decimal | None:
    """
    Комиссия заявки из ответа CCXT (поля fee/fees), пересчитанная в валюту расчётов (USDT).
    Комиссия в базовой монете переводится по средней цене исполнения. Если биржа не сообщила
    комиссию или списала её в третьей валюте (например, BNB) — None: исполнитель оценит её по taker.
    Отрицательная комиссия (ребейт мейкеру) учитывается как есть.
    """
    entries = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
    base, _, rest = symbol.partition("/")
    quote, _, settle = rest.partition(":")
    total = ZERO
    found = False
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        cost = entry.get("cost")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            continue
        currency = entry.get("currency")
        if currency in (quote, settle or quote):
            total += to_dec(cost)
        elif currency == base and average is not None:
            total += to_dec(cost) * average
        else:
            return None  # валюта неизвестна или посторонняя — точной суммы в USDT нет
        found = True
    return total if found else None


def explain_error(ex: BaseException) -> str:
    if isinstance(ex, ccxt.InsufficientFunds):
        return "недостаточно средств на счете"
    if isinstance(ex, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
        return "превышен лимит запросов к бирже"
    if isinstance(ex, ccxt.RequestTimeout):
        return "таймаут запроса"
    if isinstance(ex, (ccxt.ExchangeNotAvailable, ccxt.ExchangeClosedByUser, ccxt.OnMaintenance)):
        return "биржа недоступна или на обслуживании"
    if isinstance(ex, ccxt.AuthenticationError):
        return "ошибка аутентификации (проверьте API-ключи)"
    if isinstance(ex, ccxt.InvalidOrder):
        return "ордер отклонён биржей (объём или цена)"
    if isinstance(ex, ccxt.BadSymbol):
        return "неизвестный инструмент"
    if isinstance(ex, ccxt.NetworkError):
        return "сетевая ошибка"
    return f"{type(ex).__name__}: {ex}"


def _is_cancel(ex: BaseException) -> bool:
    from futures_arb_bot.core.cancellation import OperationCanceledError

    return isinstance(ex, OperationCanceledError)


def _default_options(exchange_id: str) -> dict[str, Any]:
    match exchange_id.lower():
        case "bybit":
            return {"defaultType": "swap", "defaultSubType": "linear"}
        case "okx" | "gate" | "mexc" | "bitget" | "htx" | "coinex" | "phemex" | "bingx":
            return {"defaultType": "swap"}
        case _:
            return {}


def _option_text(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def _to_pct(fraction: Any) -> Decimal:
    return to_dec(fraction) * HUNDRED if isinstance(fraction, (int, float)) and fraction > 0 else ZERO


def _positive(value: Any) -> Decimal | None:
    return to_dec(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _extract_quote_volume(info: Any) -> float:
    if not isinstance(info, Mapping):
        return 0.0
    for key in ("volCcy24h", "turnover24h", "quoteVolume", "volValue24h"):
        if key not in info:
            continue
        try:
            volume = float(info[key])
            if volume > 0:
                return volume
        except TypeError, ValueError:
            pass  # нечисловое поле — пропускаем
    return 0.0


def _first_not_empty(*candidates: str | None) -> str | None:
    for candidate in candidates:
        if candidate is not None and candidate.strip():
            return candidate
    return None


def _env_override(exchange_id: str, suffix: str) -> str | None:
    value = os.environ.get(f"ARB_{exchange_id.replace('-', '_').upper()}_{suffix}")
    return value if value else None
