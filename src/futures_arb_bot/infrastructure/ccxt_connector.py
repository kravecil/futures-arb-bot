"""
Адаптер одной биржи к интерфейсу ExchangeConnector через асинхронный CCXT:
загрузка рынков (только линейные бессрочные), опрос тикеров, заявки, позиции, фандинг.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
import uuid
from collections.abc import Collection, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import ccxt

from futures_arb_bot.core.abstractions import EventLog
from futures_arb_bot.core.cancellation import CancellationToken
from futures_arb_bot.core.clock import Clock
from futures_arb_bot.core.domain.enums import MarketDataSource, NetworkMode, OrderSide, OrderStatus, OrderType
from futures_arb_bot.core.domain.market_data import (
    ExchangeFees,
    FetchTickersResult,
    MarketInfo,
    OrderBookSnapshot,
    OrderRequest,
    OrderResult,
    OrderUpdate,
    PositionSnapshot,
    TickerSnapshot,
)
from futures_arb_bot.core.domain.options import ExchangeConfigEntry, MarketDataOptions, OrdersOptions
from futures_arb_bot.core.domain.order_execution import ExchangeCapabilityMap, OrderParamsBuilder
from futures_arb_bot.core.engine.logging_bridge import get_logger
from futures_arb_bot.core.numbers import HUNDRED, ONE, ZERO, dec_str, to_dec
from futures_arb_bot.infrastructure.ccxt_factory import instantiate
from futures_arb_bot.infrastructure.order_stream import OrderStream
from futures_arb_bot.infrastructure.quote_stream import QuoteStream

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
        self._contract_sizes: dict[str, Decimal] = {}
        """Размер контракта по символу (монет в контракте). Ядро считает в монетах, биржа — в контрактах."""
        self._default_fees = _FALLBACK_FEES
        self._funding_cache: dict[str, Decimal] = {}
        self._funding_fetched_at: datetime | None = None
        self._funding_unavailable_logged = False
        self._leverage_warned: set[str] = set()

        # --- котировки: последний снимок REST (обороты, запасной источник) и поток WebSocket ---
        self._rest_result: FetchTickersResult | None = None
        self._rest_at: float | None = None
        self._volumes: dict[str, Decimal] = {}
        self._market_data = MarketDataOptions()
        self._refresh_interval_ms = 2_000
        self._volume_task: asyncio.Task[None] | None = None
        self._stream_down_logged = False
        self._stream: QuoteStream | None = None
        self._orders = OrdersOptions()
        self._order_stream: OrderStream | None = None

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
        self._stream = QuoteStream(self._api, entry.id, self.display_name, log, clock)
        self._order_stream = OrderStream(self._api, entry.id, self.display_name, log, lambda order: self._parse_update(order))

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
        self._contract_sizes.clear()
        tick_size_precision = getattr(self._api, "precisionMode", None) == getattr(ccxt, "TICK_SIZE", 4)

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

            contract_size = _contract_size_of(market)
            self._contract_sizes[symbol] = contract_size
            step = _amount_step_of(market, tick_size_precision)

            # объёмы в MarketInfo — в базовой монете: лимиты и шаг биржи заданы в контрактах
            self._markets[symbol] = MarketInfo(
                symbol,
                is_perpetual=True,
                is_linear=True,
                quote_currency=market.get("quote") or market.get("settle") or "USDT",
                min_amount=to_dec(min_amount) * contract_size if isinstance(min_amount, (int, float)) and min_amount > 0 else None,
                taker_percent=taker_pct,
                maker_percent=maker_pct,
                contract_size=contract_size,
                amount_step=step * contract_size if step is not None else None,
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

    # ------------------------- котировки: WebSocket с откатом на REST -------------------------

    @property
    def supports_streaming(self) -> bool:
        """Есть ли у биржи в ccxt поток лучших цен (watch_bids_asks) или тикеров (watch_tickers)."""
        return self._stream is not None and self._stream.supported

    @property
    def streamed_symbols(self) -> set[str]:
        return self._stream.symbols if self._stream is not None else set()

    @property
    def stream_messages(self) -> int:
        return self._stream.messages if self._stream is not None else 0

    @property
    def rest_tickers(self) -> Mapping[str, TickerSnapshot]:
        """Последний снимок тикеров по REST (с оборотами за 24 ч) — по нему выбираются подписки."""
        return self._rest_result.tickers if self._rest_result is not None else {}

    def update_market_data(self, market_data: MarketDataOptions, refresh_interval_ms: int) -> None:
        """Актуальные настройки источника котировок (перечитываются сканером каждый тик)."""
        self._market_data = market_data
        self._refresh_interval_ms = refresh_interval_ms
        self._apply_keep_alive(market_data.keep_alive_ms)

    def _apply_keep_alive(self, keep_alive_ms: int) -> None:
        """Период ping-pong для новых WebSocket-соединений ccxt (действующие получат его при переподключении)."""
        if keep_alive_ms <= 0:
            return
        streaming = getattr(self._api, "streaming", None)
        if isinstance(streaming, dict) and streaming.get("keepAlive") != keep_alive_ms:
            self._api.streaming = {**streaming, "keepAlive": keep_alive_ms}

    def subscribe_quotes(self, symbols: Collection[str]) -> list[str]:
        """Подписаться на лучшие цены пар (добавляются только новые). Возвращает новые пары."""
        if self._stream is None or not self._stream.supported:
            return []
        return self._stream.subscribe(symbols)

    async def fetch_tickers(self, ct: CancellationToken | None = None) -> FetchTickersResult:
        """
        Котировки биржи. В режиме WebSocket — из потока, пока он жив; оборот за 24 ч обновляется
        по REST в фоне раз в volume_refresh_seconds. Поток молчит дольше stale_after_ms — снимок REST,
        но не чаще refresh_interval_ms (между запросами отдаётся последний снимок).
        """
        _check(ct)
        md = self._market_data
        stream = self._stream
        if md.source == MarketDataSource.WEBSOCKET and stream is not None and stream.started:
            if stream.healthy(md.stale_after_ms / 1000):
                if self._stream_down_logged:
                    self._stream_down_logged = False
                    self._log.info(f"[{self.display_name}] котировки снова из потока WebSocket")
                self._schedule_volume_refresh(md)
                merged: dict[str, TickerSnapshot] = dict(self.rest_tickers)
                merged.update(stream.snapshot(self._volumes))
                return FetchTickersResult(merged, timedelta(0), "ws")

            silent = stream.silent_for()
            if not self._stream_down_logged and silent is not None and silent * 1000 >= md.stale_after_ms:
                self._stream_down_logged = True
                self._log.warning(f"[{self.display_name}] поток котировок молчит {silent:.0f} с — котировки по REST до его восстановления")

            if md.reconnect_after_ms > 0 and silent is not None and silent * 1000 >= md.reconnect_after_ms:
                reset = stream.force_reconnect(silent, md.reconnect_after_ms / 1000)
                if reset > 0:
                    self._log.warning(f"[{self.display_name}] поток котировок молчит {silent:.0f} с — переподключаю принудительно")

            recent = self._rest_at is not None and (time.monotonic() - self._rest_at) * 1000 < self._refresh_interval_ms
            if self._rest_result is not None and recent:
                return FetchTickersResult(self._rest_result.tickers, timedelta(0), "cache")

        return await self._fetch_tickers_rest()

    def _schedule_volume_refresh(self, md: MarketDataOptions) -> None:
        if self._volume_task is not None and not self._volume_task.done():
            return
        if self._rest_at is not None and time.monotonic() - self._rest_at < md.volume_refresh_seconds:
            return
        self._volume_task = asyncio.get_running_loop().create_task(self._refresh_volumes())

    async def _refresh_volumes(self) -> None:
        try:
            await self._fetch_tickers_rest()
        except Exception as ex:
            self._logger.debug("фоновое обновление оборотов не удалось", exc_info=ex)
            self._rest_at = time.monotonic()  # не долбим биржу каждый тик — повторим через интервал

    async def _fetch_tickers_rest(self) -> FetchTickersResult:
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

        fetched = FetchTickersResult(result, timedelta(seconds=time.perf_counter() - started), "rest")
        self._rest_result = fetched
        self._rest_at = time.monotonic()
        self._volumes = {symbol: t.quote_volume_24h for symbol, t in result.items()}
        return fetched

    # ------------------------- заявки -------------------------

    # ------------------------- заявки: REST и WebSocket -------------------------

    @property
    def order_stream_running(self) -> bool:
        return self._order_stream is not None and self._order_stream.running

    @property
    def supports_order_stream(self) -> bool:
        return self._order_stream is not None and self._order_stream.supported

    @property
    def supports_ws_orders(self) -> bool:
        """Умеет ли биржа выставлять заявки через WebSocket (create_order_ws)."""
        return bool((getattr(self._api, "has", {}) or {}).get("createOrderWs"))

    def update_orders(self, orders: OrdersOptions) -> None:
        """Актуальные настройки работы с заявками (перечитываются каждый тик)."""
        self._orders = orders

    def start_order_stream(self, orders: OrdersOptions) -> bool:
        """Запустить поток собственных заявок (watch_orders). False — не нужен или биржа не умеет."""
        self._orders = orders
        if orders.updates != MarketDataSource.WEBSOCKET or self._order_stream is None:
            return False
        return self._order_stream.start()

    async def wait_order_update(self, timeout: float) -> bool:
        """Подождать события по заявкам из потока (или просто таймаут, если потока нет)."""
        if self._order_stream is not None and self._orders.updates == MarketDataSource.WEBSOCKET:
            return await self._order_stream.wait_update(timeout)
        if timeout > 0:
            await asyncio.sleep(timeout)
        return False

    async def place_order(self, request: OrderRequest, ct: CancellationToken | None = None) -> OrderResult:
        client_id: str | None = None
        use_ws = False
        try:
            _check(ct)

            # ядро считает в монетах, биржа (OKX, Gate…) ждёт контракты
            contracts = request.amount / self.contract_size(request.symbol)
            amount = float(self._api.amount_to_precision(request.symbol, float(contracts)))
            if amount <= 0:
                return OrderResult.fail(f"объём после округления равен нулю ({dec_str(request.amount)})")

            # имена и условия параметров биржи (перевод политики заявки) — в OrderParamsBuilder
            parameters = OrderParamsBuilder.build(request, ExchangeCapabilityMap.for_id(self.id))
            if self.id.lower() in CLIENT_ORDER_ID_EXCHANGES and "clientOrderId" not in parameters:
                # свой id заявки: по нему находим заявку, если ответ на запрос потерялся в сети
                client_id = new_client_order_id()
                parameters["clientOrderId"] = client_id

            price: float | None = None
            # ChaseLimit на бирже — это limit: локальное догонание делает исполнитель, нативное — биржа
            order_type = "market" if request.type == OrderType.MARKET else "limit"
            if request.type != OrderType.MARKET:
                if request.price is None or request.price <= ZERO:
                    return OrderResult.fail("для лимитного ордера не задана цена")
                price = float(self._api.price_to_precision(request.symbol, float(request.price)))

            side = "buy" if request.side == OrderSide.BUY else "sell"
            use_ws = self._orders.transport == MarketDataSource.WEBSOCKET and self.supports_ws_orders
            create = self._api.create_order_ws if use_ws else self._api.create_order
            order = await create(request.symbol, order_type, side, amount, price, parameters)
            return self._order_result(request, order, client_id)
        except Exception as ex:
            if _is_cancel(ex):
                raise
            if client_id is not None and isinstance(ex, (ccxt.NetworkError, ccxt.RequestTimeout)):
                # ответ потерян: заявка могла дойти до биржи — ищем её по нашему clientOrderId
                found = await self._find_order_by_client_id(request.symbol, client_id)
                if found is not None:
                    self._log.warning(
                        f"[{self.display_name}] ответ на заявку {request.side} {request.symbol} потерян, заявка найдена по clientOrderId"
                    )
                    return self._order_result(request, found, client_id)
            message = explain_error(ex)
            via = " (WS)" if use_ws else ""
            self._log.error(f"[{self.display_name}] ордер{via} {request.side} {request.symbol} × {dec_str(request.amount)}: {message}")
            self._logger.debug("order failed", exc_info=ex)
            return OrderResult.fail(message)

    def _order_result(self, request: OrderRequest, order: Mapping[str, Any], client_id: str | None) -> OrderResult:
        filled_raw = order.get("filled")
        filled = to_dec(filled_raw) * self.contract_size(request.symbol) if isinstance(filled_raw, (int, float)) and filled_raw > 0 else ZERO
        average = _positive(order.get("average"))
        if average is None:
            average = _positive(order.get("price"))

        # рыночный обязан исполниться сразу; лимитный мог ещё не набрать объёма
        status = order.get("status")
        if request.type == OrderType.MARKET and status in ("open", "rejected") and filled <= ZERO:
            return OrderResult.fail(f"рыночный ордер не исполнен (статус: {status or 'unknown'})")

        order_id = order.get("id")
        fee = fee_in_quote(order, request.symbol, average) if filled > ZERO else None
        if order_id is not None and self._order_stream is not None:
            # исходное состояние заявки — в кэш потока: дальше статусы приходят событиями
            update = self._parse_update(order, request.symbol)
            if update is not None:
                self._order_stream.remember(update, client_id)
        return OrderResult.ok(str(order_id) if order_id is not None else None, average, filled, fee)

    async def _find_order_by_client_id(self, symbol: str, client_id: str) -> Mapping[str, Any] | None:
        """Заявка с нашим clientOrderId: из потока заявок, иначе среди открытых и недавно закрытых."""
        stream = self._order_stream
        if stream is not None and stream.healthy:
            for _ in range(20):  # до ~2 с ждём события по заявке
                order_id = stream.find_by_client_id(client_id)
                if order_id is not None:
                    with contextlib.suppress(Exception):
                        return await self._api.fetch_order(order_id, symbol)
                    break
                await stream.wait_update(0.1)
        for method in ("fetch_open_orders", "fetch_closed_orders"):
            try:
                orders = await getattr(self._api, method)(symbol, None, 20)
            except Exception as ex:
                self._logger.debug(f"{method} при поиске по clientOrderId не удался", exc_info=ex)
                continue
            for order in orders or []:
                cid = str(order.get("clientOrderId") or "")
                if cid and (cid == client_id or cid.endswith(client_id)):
                    return order
        return None

    async def fetch_order(self, order_id: str, symbol: str, ct: CancellationToken | None = None, fresh: bool = False) -> OrderUpdate | None:
        """
        Состояние заявки. С потоком заявок — из его кэша (без запроса), если статус обновлялся не позже
        max_cache_age_ms назад; fresh=True — всегда запрос REST (финальные проверки после отмены).
        """
        stream = self._order_stream
        if not fresh and stream is not None and self._orders.updates == MarketDataSource.WEBSOCKET:
            cached = stream.get(order_id, self._orders.max_cache_age_ms / 1000)
            if cached is not None:
                return cached
        try:
            _check(ct)
            order = await self._api.fetch_order(order_id, symbol)
            update = self._parse_update(order, symbol, order_id)
            if update is not None and stream is not None:
                stream.remember(update)
            return update
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
            if self._orders.transport == MarketDataSource.WEBSOCKET and (getattr(self._api, "has", {}) or {}).get("cancelOrderWs"):
                try:
                    await self._api.cancel_order_ws(order_id, symbol)
                    return True
                except ccxt.NetworkError as ex:
                    # отмена идемпотентна — при сбое соединения повторяем по REST
                    self._logger.debug("cancel_order_ws failed, retry via REST", exc_info=ex)
            await self._api.cancel_order(order_id, symbol)
            return True
        except Exception as ex:
            if _is_cancel(ex):
                raise
            self._logger.debug(f"CancelOrder {order_id} {symbol} failed", exc_info=ex)
            return False
        finally:
            if self._order_stream is not None:
                self._order_stream.forget(order_id)  # после отмены — только свежие данные

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
            result.append(PositionSnapshot(symbol, side, to_dec(contracts) * self.contract_size(symbol), _positive(position.get("entryPrice"))))
        return result

    async def fetch_order_book(self, symbol: str, depth: int, ct: CancellationToken | None = None) -> OrderBookSnapshot | None:
        """Верх стакана запросом REST; объёмы уровней — в базовой монете. None — стакан недоступен."""
        try:
            _check(ct)
            book = await self._api.fetch_order_book(symbol, _book_limit(self.id, depth))
        except Exception as ex:
            if _is_cancel(ex):
                raise
            self._logger.debug(f"FetchOrderBook {symbol} failed", exc_info=ex)
            return None

        size = self.contract_size(symbol)

        def levels(raw: Any) -> tuple[tuple[Decimal, Decimal], ...]:
            result: list[tuple[Decimal, Decimal]] = []
            for level in raw or []:
                if not isinstance(level, (list, tuple)) or len(level) < 2:
                    continue
                price, amount = level[0], level[1]
                if isinstance(price, (int, float)) and isinstance(amount, (int, float)) and price > 0 and amount > 0:
                    result.append((to_dec(price), to_dec(amount) * size))
            return tuple(result)

        ms = book.get("timestamp") if isinstance(book, Mapping) else None
        stamp = datetime.fromtimestamp(ms / 1000, UTC) if isinstance(ms, (int, float)) and ms > 0 else self._clock.now()
        return OrderBookSnapshot(self.id, symbol, levels(book.get("bids")), levels(book.get("asks")), stamp)

    def contract_size(self, symbol: str) -> Decimal:
        """Монет в одном контракте биржи (1, если биржа считает объём в монетах или размер неизвестен)."""
        return self._contract_sizes.get(symbol, ONE)

    def _parse_update(self, order: Mapping[str, Any], symbol: str | None = None, order_id: str | None = None) -> OrderUpdate | None:
        """Заявка ccxt → OrderUpdate с исполнением в базовой монете (а не в контрактах)."""
        update = parse_order_update(order, symbol, order_id)
        if update is None:
            return None
        sym = symbol or order.get("symbol") or ""
        size = self.contract_size(sym)
        if size == ONE or update.filled_amount <= ZERO:
            return update
        return replace(update, filled_amount=update.filled_amount * size)

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
        if self._volume_task is not None and not self._volume_task.done():
            self._volume_task.cancel()
            with contextlib.suppress(BaseException):
                await self._volume_task
        if self._stream is not None:
            await self._stream.aclose()
        if self._order_stream is not None:
            await self._order_stream.aclose()
        await self._api.close()


# ------------------------- вспомогательное -------------------------


CLIENT_ORDER_ID_EXCHANGES = frozenset({"okx", "gate", "bybit", "binanceusdm", "binance", "bitget", "kucoinfutures", "bingx"})
"""Биржи, где единый параметр ccxt clientOrderId проверенно поддерживается."""


def new_client_order_id() -> str:
    """Свой id заявки: латиница и цифры, 23 символа — в пределах ограничений всех бирж списка."""
    return "arb" + uuid.uuid4().hex[:20]


def parse_order_update(order: Mapping[str, Any], symbol: str | None = None, order_id: str | None = None) -> OrderUpdate | None:
    """Заявка ccxt (ответ REST/WS или событие потока) → OrderUpdate."""
    oid = order.get("id") if order_id is None else order_id
    if oid is None:
        return None
    sym = symbol or order.get("symbol") or ""
    filled_raw = order.get("filled")
    filled = to_dec(filled_raw) if isinstance(filled_raw, (int, float)) and filled_raw > 0 else ZERO
    average = _positive(order.get("average"))
    fee = fee_in_quote(order, sym, average) if filled > ZERO and sym else None
    return OrderUpdate(str(oid), map_status(order.get("status")), filled, average, fee)


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
        # исходный ответ биржи (код и текст) нужен, чтобы отличить «нет позиции под reduce-only» от лота/цены
        detail = exchange_error_detail(ex)
        return f"ордер отклонён биржей (объём или цена): {detail}" if detail else "ордер отклонён биржей (объём или цена)"
    if isinstance(ex, ccxt.BadSymbol):
        return "неизвестный инструмент"
    if isinstance(ex, ccxt.NetworkError):
        return "сетевая ошибка"
    return f"{type(ex).__name__}: {ex}"


_ERROR_CODE = re.compile(r'"(?:retCode|code|sCode|label)"\s*:\s*"?(-?[\w.]+)')
_ERROR_TEXT = re.compile(r'"(?:retMsg|msg|sMsg|message)"\s*:\s*"([^"]+)"')


def exchange_error_detail(ex: BaseException, limit: int = 200) -> str:
    """
    Короткая выжимка ответа биржи из исключения ccxt: «110017 current position is zero…» для JSON-ответов
    (Bybit retCode/retMsg, OKX code/msg и sCode/sMsg), иначе начало текста исключения.
    """
    text = str(ex).strip()
    codes = [c for c in _ERROR_CODE.findall(text) if c not in ("0", "")]
    messages = [m for m in _ERROR_TEXT.findall(text) if m.strip()]
    if codes or messages:
        detail = " ".join(part for part in (codes[-1] if codes else None, messages[-1] if messages else None) if part)
    else:
        detail = text
    return detail if len(detail) <= limit else detail[: limit - 1] + "…"


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


def _contract_size_of(market: Mapping[str, Any]) -> Decimal:
    """Размер контракта рынка ccxt; у бирж, считающих объём в монетах, он 1 или не задан."""
    raw = market.get("contractSize")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return to_dec(raw)
    return ONE


def _amount_step_of(market: Mapping[str, Any], tick_size_precision: bool) -> Decimal | None:
    """Шаг объёма в единицах биржи: precision.amount — сам шаг (TICK_SIZE) или число знаков (DECIMAL_PLACES)."""
    raw = (market.get("precision") or {}).get("amount")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    if tick_size_precision:
        return to_dec(raw) if raw > 0 else None
    return Decimal(1).scaleb(-int(raw)) if raw >= 0 else None


_BOOK_LIMITS: dict[str, tuple[int, ...]] = {
    # биржи, которые принимают только фиксированные глубины стакана
    "bingx": (5, 10, 20, 50, 100),
    "binanceusdm": (5, 10, 20, 50, 100),
    "kucoinfutures": (20, 100),
}


def _book_limit(exchange_id: str, depth: int) -> int:
    allowed = _BOOK_LIMITS.get(exchange_id.lower())
    if allowed is None:
        return depth
    return next((v for v in allowed if v >= depth), allowed[-1])


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
