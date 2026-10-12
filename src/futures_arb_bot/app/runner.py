"""
Оркестратор сеанса: баннер → подключение бирж → проверки безопасности →
сканирование (дашборд или поток событий) → корректная остановка → итоговый отчёт.
"""

from __future__ import annotations

import asyncio
import contextlib
import traceback

from futures_arb_bot.app.bootstrap import Services
from futures_arb_bot.app.console import console, esc
from futures_arb_bot.app.console_feed import SimpleConsoleFeed
from futures_arb_bot.app.ui.banner import print_banner
from futures_arb_bot.app.ui.dashboard import Dashboard
from futures_arb_bot.app.ui.report import SessionReportPrinter
from futures_arb_bot.app.ui.views import opportunities_table
from futures_arb_bot.core.abstractions import ExchangeConnector
from futures_arb_bot.core.cancellation import CancellationToken, OperationCanceledError
from futures_arb_bot.core.domain.enums import CloseReason, MarketDataSource, NetworkMode, TimeInForce
from futures_arb_bot.core.domain.options import BotOptions, NotificationOptions
from futures_arb_bot.core.domain.order_config_validator import OrderConfigValidator
from futures_arb_bot.core.domain.order_execution import ExchangeCapabilityMap, OrderPolicy, OrderPolicyResolver
from futures_arb_bot.core.engine import formatting
from futures_arb_bot.core.errors import message_of
from futures_arb_bot.core.numbers import ZERO, dec_str

_NONE = CancellationToken.none()


class AppRunner:
    def __init__(self, services: Services) -> None:
        self._services = services
        self._config = services.config
        self._log = services.event_log
        self._factory = services.factory
        self._registry = services.registry
        self._stats = services.statistics  # сеанс отсчитывается с момента сборки приложения
        self._cli = services.cli
        self._last_error: str | None = None

    async def run(self, ct: CancellationToken) -> int:
        problem = _validate_options(self._config.current)
        if problem is not None:
            console.print(f"[red]Конфигурация некорректна:[/] {esc(problem)}")
            return 2

        problem = self._validate_execution(self._config.current)
        if problem is not None:
            console.print(f"[red]Настройки исполнения некорректны:[/] {esc(problem)}")
            return 2

        print_banner(self._config)

        connectors = await self._connect_exchanges(ct)
        if len(connectors) < 2:
            console.print("[red]Подключено меньше двух бирж — арбитраж невозможен. Проверьте exchanges.yaml и сеть.[/]")
            for connector in connectors:
                await connector.aclose()
            return 2

        await self._registry.replace(connectors)

        self._describe_execution(connectors)

        if not await self._check_trading_access(connectors, ct):
            return 2

        self._start_order_streams(connectors)

        options = self._config.current
        trading = "ВКЛЮЧЕНА" if options.arbitrage.enabled else "выключена (мониторинг)"
        self._log.info(
            f"Сеанс запущен: режим {options.general.network_mode}, бирж {len(connectors)}, торговля {trading}, "
            f"котировки {options.general.market_data.source}"
        )
        self._log_notification_state(options.notifications)

        if self._cli.once:
            return await self._run_once(ct)
        return await self._run_interactive(ct)

    # ------------------------- режимы работы -------------------------

    async def _run_interactive(self, ct: CancellationToken) -> int:
        scanner = self._services.scanner

        if self._cli.no_ui:
            feed_ct = ct.linked()
            feed_task = asyncio.create_task(SimpleConsoleFeed(self._log).run(feed_ct))
            try:
                await scanner.run(ct)
            except OperationCanceledError:
                pass

            feed_ct.cancel()
            with contextlib.suppress(Exception):
                await feed_task  # поток событий уже не нужен
        else:
            dashboard = Dashboard(self._log, self._config)
            dashboard_task = asyncio.create_task(dashboard.run(ct, lambda: scanner.snapshot))
            try:
                await scanner.run(ct)
            except OperationCanceledError:
                pass
            except Exception as ex:
                self._last_error = message_of(ex)
                self._log.error(f"Сканер остановлен с ошибкой: {message_of(ex)}")

            with contextlib.suppress(Exception):
                await dashboard_task  # дашборд уже завершился

        await self._shutdown()
        self._print_final_report()
        return 0

    async def _run_once(self, ct: CancellationToken) -> int:
        console.print("[bright_black]Режим --once: одиночное сканирование.[/]")
        scanner = self._services.scanner
        await scanner.scan_once(ct)

        snapshot = scanner.snapshot
        if snapshot is not None:
            console.print(opportunities_table(snapshot, self._config.current.arbitrage))

        await self._shutdown()
        self._print_final_report()
        return 0

    # ------------------------- запуск и остановка -------------------------

    def _describe_execution(self, connectors: list[ExchangeConnector]) -> None:
        """Какие заявки реально уйдут на каждую биржу: понижения unsupported-настроек видны сразу при старте."""
        options = self._config.current.arbitrage
        entries = self._config.current.exchanges.items

        for connector in connectors:
            entry = next((e for e in entries if e.id.lower() == connector.id.lower()), None)
            caps = ExchangeCapabilityMap.for_id(connector.id)
            per_exchange = entry.execution if entry is not None else None
            open_policy = OrderPolicyResolver.resolve_entry(options.execution, per_exchange, caps)
            close_policy = OrderPolicyResolver.resolve_close(options.execution, per_exchange, caps)

            console.print(
                f"[bright_black]·[/] {esc(connector.display_name)}: вход — {esc(_describe_policy(open_policy))}, "
                f"закрытие — {esc(_describe_policy(close_policy))}"
            )

            for note in [*open_policy.notes, *close_policy.notes]:
                console.print(f"[yellow]![/] {esc(connector.display_name)}: {esc(note)}")
                self._log.warning(f"[{connector.id}] {note}")

    async def _connect_exchanges(self, ct: CancellationToken) -> list[ExchangeConnector]:
        entries = [e for e in self._config.current.exchanges.items if e.enabled]
        if not entries:
            console.print("[red]В exchanges.yaml нет ни одной включённой биржи.[/]")
            return []

        mode = self._config.current.general.network_mode
        supported = {i.lower() for i in self._factory.supported_ids}
        unsupported = [e for e in entries if e.id.lower() not in supported]
        for entry in unsupported:
            console.print(f"[red]Биржа «{esc(entry.id)}» не встроена. Поддерживаются:[/] {esc(', '.join(self._factory.supported_ids))}")

        connectors: list[ExchangeConnector] = []
        for entry in entries:
            if entry in unsupported:
                continue
            connector: ExchangeConnector | None = None
            try:
                console.print(f"[cyan]→[/] {esc(entry.name if entry.name.strip() else entry.id)}: подключение…")
                connector = self._factory.create(entry, mode)
                await connector.connect(ct)
                connectors.append(connector)
                console.print(f"[green]✓[/] {esc(connector.display_name)}: фьючерсных рынков {connector.market_count}")
            except OperationCanceledError:
                if connector is not None:
                    await connector.aclose()
                for done in connectors:
                    await done.aclose()
                raise
            except Exception as ex:
                console.print(f"[red]✗[/] {esc(entry.name)}: {esc(message_of(ex))}")
                self._log.error(f"[{entry.id}] не подключилась: {message_of(ex)}")
                # полный текст (тип исключения, цепочка) — в файл журнала: иначе сбой недиагностируем
                self._log.debug(f"[{entry.id}] исключение подключения: {''.join(traceback.format_exception(ex)).rstrip()}")
                if connector is not None:
                    with contextlib.suppress(Exception):
                        await connector.aclose()

        return connectors

    async def _check_trading_access(self, connectors: list[ExchangeConnector], ct: CancellationToken) -> bool:
        options = self._config.current
        mode = options.general.network_mode

        if mode == NetworkMode.LIVE and options.arbitrage.enabled and not options.arbitrage.allow_live:
            console.print("[red]Режим Live требует явного подтверждения: выставьте arbitrage.allow_live = true в appsettings.yaml.[/]")
            return False

        if mode == NetworkMode.DRY_RUN or not options.arbitrage.enabled:
            return True  # торгуем виртуально или только мониторим — ключи не нужны

        # Testnet/Live + торговля: проверяем валидность ключей приватным запросом
        for connector in connectors:
            console.print(f"[cyan]→[/] {esc(connector.display_name)}: проверка торгового доступа…")
            if not await connector.verify_access(ct):
                console.print(
                    "[red]Ключи недействительны или доступ закрыт — включите торговлю нельзя. "
                    "Заполните ARB_<ID>_API_KEY / ARB_<ID>_SECRET в .env (или api_key/secret в exchanges.yaml).[/]"
                )
                return False
            console.print(f"[green]✓[/] {esc(connector.display_name)}: доступ подтверждён")

        return True

    def _start_order_streams(self, connectors: list[ExchangeConnector]) -> None:
        """Потоки собственных заявок (watch_orders) — только когда робот реально торгует."""
        options = self._config.current
        if options.general.network_mode == NetworkMode.DRY_RUN or not options.arbitrage.enabled:
            return
        orders = options.general.orders
        for connector in connectors:
            connector.update_orders(orders)
            if orders.updates != MarketDataSource.WEBSOCKET:
                continue
            if connector.start_order_stream(orders):
                console.print(f"[green]✓[/] {esc(connector.display_name)}: статусы заявок — поток WebSocket")
            else:
                console.print(f"[yellow]![/] {esc(connector.display_name)}: потока заявок в ccxt нет — статусы опросом REST")

    async def _shutdown(self) -> None:
        options = self._config.current
        executor = self._services.executor

        if executor.has_open_positions:
            if options.arbitrage.enabled and options.arbitrage.close_positions_on_exit:
                await executor.close_all(CloseReason.SESSION_END, _NONE)
            else:
                self._log.warning("Открытые позиции сохранены: ClosePositionsOnExit = false — закрыть их вручную на биржах.")

        self._log.info("Сеанс завершён.")

    def _log_notification_state(self, notifications: NotificationOptions) -> None:
        """Строка журнала про канал уведомлений: включён, не настроен или выключен."""
        if notifications.is_usable:
            threshold = (
                notifications.min_spread_percent
                if notifications.min_spread_percent is not None
                else self._config.current.arbitrage.min_spread_percent_up
            )
            self._log.info(
                f"Уведомления в MAX: включены (чат {notifications.admin_chat_id}, порог {formatting.pct(threshold)}, "
                f"не чаще одного на символ в {notifications.signal_cooldown_seconds} с)"
            )
            return

        if notifications.enabled:
            self._log.warning(
                "Уведомления включены, но BotToken или AdminChatId не заданы — сигналы не отправляются. Помощь: --notify-test и --notify-chat-id."
            )
            return

        self._log.debug("Уведомления выключены (Notifications:Enabled = false).")

    def _print_final_report(self) -> None:
        report = self._stats.snapshot(self._services.clock.now())
        SessionReportPrinter(self._log).print(report)

    def _validate_execution(self, options: BotOptions) -> str | None:
        """Ошибки блокируют запуск, предупреждения (в том числе понижение настроек) — в консоль и журнал."""
        report = OrderConfigValidator.validate(options, self._factory.supported_ids)
        for warning in report.warnings:
            console.print(f"[yellow]![/] {esc(warning)}")
            self._log.warning(warning)
        return None if report.is_valid else " ".join(report.errors)

    async def aclose(self) -> None:
        await self._services.aclose()


def _describe_policy(policy: OrderPolicy) -> str:
    """Описание политики заявки для строки запуска."""
    parts: list[str] = []

    if policy.requires_price and policy.limit_offset_bps != ZERO:
        parts.append(f"смещение {dec_str(policy.limit_offset_bps)} bps")

    chase = policy.chase
    if chase is not None:
        if policy.chases_natively:
            keys = ", ".join(policy.exchange_params.keys()) if policy.exchange_params else ""
            parts.insert(0, f"ChaseLimit (нативный chase биржи, параметры: {keys or '—'})")
        else:
            tail = "остаток — market" if chase.fallback_to_market else "без market-добивки"
            parts.insert(
                0,
                f"ChaseLimit (локальные перестановки: до {chase.max_steps} шагов по {dec_str(chase.step_bps)} bps, "
                f"не дальше {dec_str(chase.max_deviation_bps)} bps от старта, {tail})",
            )
    else:
        parts.insert(0, str(policy.type))
        if policy.requires_price and policy.time_in_force != TimeInForce.GTC:
            parts.insert(1, str(policy.time_in_force))

    return ", ".join(parts)


def _validate_options(options: BotOptions) -> str | None:
    arbitrage = options.arbitrage

    if arbitrage.min_spread_percent_down >= arbitrage.min_spread_percent_up:
        return "MinSpreadPercentDown должен быть меньше MinSpreadPercentUp (порог закрытия ниже порога входа)."

    if arbitrage.stop_loss_spread_percent <= arbitrage.min_spread_percent_up:
        return "StopLossSpreadPercent должен быть больше MinSpreadPercentUp."

    if arbitrage.order_size_usd <= ZERO:
        return "OrderSizeUsd должен быть положительным."

    if options.general.refresh_interval_ms < 500:
        return "RefreshIntervalMs не может быть меньше 500 мс (бан со стороны бирж)."

    market_data = options.general.market_data
    if market_data.scan_interval_ms < 20:
        return "market_data.scan_interval_ms не может быть меньше 20 мс."

    if market_data.stale_after_ms < 500:
        return "market_data.stale_after_ms не может быть меньше 500 мс."

    if market_data.volume_refresh_seconds < 10:
        return "market_data.volume_refresh_seconds не может быть меньше 10 с."

    return None
