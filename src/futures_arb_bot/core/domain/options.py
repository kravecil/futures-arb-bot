"""
Настройки робота (config/*.yaml). Имена ключей не чувствительны к регистру и подчёркиваниям:
``MinSpreadPercentUp``, ``min_spread_percent_up`` и ``minspreadpercentup`` — одно и то же.
Как и прежде, границы значений не проверяются при загрузке: за это отвечают проверки при запуске.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from futures_arb_bot.core.domain.enums import ChaseMode, MaxSendEndpoint, NetworkMode, OrderType, TimeInForce


def normalize_key(key: str) -> str:
    """Ключ конфигурации без регистра и разделителей."""
    return key.replace("_", "").replace("-", "").lower()


class ConfigModel(BaseModel):
    """База разделов конфигурации: сопоставление ключей без учёта регистра, лишние ключи игнорируются."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    @model_validator(mode="before")
    @classmethod
    def _match_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        by_key = {normalize_key(name): name for name in cls.model_fields}
        result: dict[str, Any] = {}
        for key, value in data.items():
            if not isinstance(key, str):
                continue
            name = by_key.get(normalize_key(key))
            if name is None:
                continue
            field = cls.model_fields[name]
            if value is None and field.default is not None:
                continue  # null для ненулевого поля — остаётся значение по умолчанию
            result[name] = value
        return result


class UiOptions(ConfigModel):
    """Настройки консольного интерфейса."""

    refresh_ms: int = 400
    """Период перерисовки дашборда, мс."""
    log_lines: int = 9
    """Строк журнала в панели дашборда (1..50, иначе 9)."""
    top_rows: int = 12
    """Сколько лучших возможностей показывать в таблице."""

    @field_validator("log_lines", mode="after")
    @classmethod
    def _clamp_log_lines(cls, value: int) -> int:
        return value if 0 < value <= 50 else 9


class GeneralOptions(ConfigModel):
    """Общие настройки приложения (appsettings.yaml → general)."""

    network_mode: NetworkMode = NetworkMode.DRY_RUN
    refresh_interval_ms: int = 2_000
    write_log_file: bool = True
    console_ui: UiOptions = Field(default_factory=UiOptions)


class ChaseOptions(ConfigModel):
    """Настройки догонания цены лимитной заявки (execution.chase)."""

    mode: ChaseMode | None = None
    max_steps: int | None = None
    step_interval_ms: int | None = None
    step_bps: Decimal | None = None
    max_deviation_bps: Decimal | None = None
    fallback_to_market: bool | None = None
    params: dict[str, str] | None = None
    """Нативные параметры биржи для mode = Native (имена — те, что ждёт CCXT в params)."""

    @field_validator("params", mode="before")
    @classmethod
    def _stringify_params(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): "" if v is None else _scalar_text(v) for k, v in value.items()}
        return value


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


class OrderExecutionOptions(ConfigModel):
    """
    Настройки исполнения одной заявки (arbitrage.execution либо переопределение на бирже).
    Любое поле может отсутствовать — тогда берётся значение по умолчанию или из глобального раздела.
    """

    type: OrderType | None = None
    limit_offset_bps: Decimal | None = None
    time_in_force: TimeInForce | None = None
    chase: ChaseOptions | None = None
    close: OrderExecutionOptions | None = None


class ArbitrageOptions(ConfigModel):
    """Настройки арбитража (appsettings.yaml → arbitrage)."""

    enabled: bool = False
    allow_live: bool = False
    min_spread_percent_up: Decimal = Decimal("0.4")
    min_spread_percent_down: Decimal = Decimal("0.15")
    stop_loss_spread_percent: Decimal = Decimal("1.0")
    include_fees: bool = True
    slippage_buffer_percent: Decimal = Decimal("0.03")
    max_spread_percent: Decimal = Decimal(5)
    min_funding_rate_percent: Decimal = Decimal(-1)
    order_execution_timeout_ms: int = 3_000
    rebalance_tolerance_percent: Decimal = Decimal("1.0")
    order_poll_interval_ms: int = 200
    execution: OrderExecutionOptions = Field(default_factory=OrderExecutionOptions)
    order_size_usd: Decimal = Decimal(100)
    leverage: int = 1
    max_open_positions: int = 4
    max_positions_per_exchange: int = 4
    max_position_age_minutes: int = 720
    close_positions_on_exit: bool = True


class SymbolsOptions(ConfigModel):
    """Настройки торговых пар (symbols.yaml → symbols)."""

    quote_currencies: list[str] = Field(default_factory=lambda: ["USDT"])
    min_quote_volume24h_usd: Decimal = Decimal(5_000_000)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    max_symbols: int = 400
    max_ticker_age_seconds: int = 15
    max_ticker_skew_seconds: int = 5

    @field_validator("quote_currencies", "include", "exclude", mode="before")
    @classmethod
    def _list_or_empty(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value if v is not None] if isinstance(value, list) else value


class ExchangeConfigEntry(ConfigModel):
    """Описание одной биржи в конфигурации."""

    id: str = ""
    name: str = ""
    enabled: bool = True
    api_key: str = ""
    secret: str = ""
    password: str = ""
    taker_fee_percent: Decimal | None = None
    maker_fee_percent: Decimal | None = None
    options: dict[str, Any] | None = None
    execution: OrderExecutionOptions | None = None

    @field_validator("id", "name", "api_key", "secret", "password", mode="before")
    @classmethod
    def _text(cls, value: Any) -> Any:
        return "" if value is None else str(value)


class ExchangesOptions(ConfigModel):
    """Список бирж (exchanges.yaml → exchanges)."""

    items: list[ExchangeConfigEntry] = Field(default_factory=list)


DEFAULT_TEXT_TEMPLATE = (
    "Арбитраж {symbol}: нетто {net} (gross {gross})\n"
    "лонг {longExchange} @ {longPrice} → шорт {shortExchange} @ {shortPrice}\n"
    "фандинг {longFunding}/{shortFunding} · объём {volume} $ · {time}"
)

DEFAULT_MAX_BASE_URL = "https://platform-api2.max.ru"


class NotificationOptions(ConfigModel):
    """
    Настройки уведомлений администратору (notifications.yaml → notifications).
    Раздел необязателен: отсутствие файла означает «выключено».
    """

    enabled: bool = False
    bot_token: str = ""
    admin_chat_id: str = ""
    api_base_url: str = DEFAULT_MAX_BASE_URL
    endpoint: MaxSendEndpoint = MaxSendEndpoint.MESSAGES
    min_spread_percent: Decimal | None = None
    use_markdown: bool = False
    silent: bool = False
    signal_cooldown_seconds: int = 120
    max_per_minute: int = 20
    timeout_ms: int = 5_000
    max_retries: int = 3
    failure_circuit_limit: int = 5
    circuit_break_minutes: int = 10
    queue_capacity: int = 64
    text_template: str = DEFAULT_TEXT_TEMPLATE

    @field_validator("bot_token", "admin_chat_id", "api_base_url", "text_template", mode="before")
    @classmethod
    def _text(cls, value: Any) -> Any:
        return "" if value is None else str(value)

    @property
    def is_usable(self) -> bool:
        """Раздел реально может отправлять сообщения."""
        return self.enabled and bool(self.bot_token.strip()) and bool(self.admin_chat_id.strip())


class BotOptions(ConfigModel):
    """Корневой контейнер всех настроек робота."""

    general: GeneralOptions = Field(default_factory=GeneralOptions)
    arbitrage: ArbitrageOptions = Field(default_factory=ArbitrageOptions)
    symbols: SymbolsOptions = Field(default_factory=SymbolsOptions)
    exchanges: ExchangesOptions = Field(default_factory=ExchangesOptions)
    notifications: NotificationOptions = Field(default_factory=NotificationOptions)


OrderExecutionOptions.model_rebuild()
