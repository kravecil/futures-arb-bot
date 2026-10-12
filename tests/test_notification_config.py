"""
Загрузка конфигурации: необязательный notifications.yaml, значения по умолчанию при отсутствии файла,
переопределение секрета переменной окружения, ключи без учёта регистра и перечитка «на лету».
"""

import os
import time
from pathlib import Path

import pytest

from futures_arb_bot.core.domain.enums import MaxSendEndpoint, NetworkMode
from futures_arb_bot.core.domain.options import DEFAULT_MAX_BASE_URL, NotificationOptions
from futures_arb_bot.infrastructure import config as config_module
from futures_arb_bot.infrastructure.config import ConfigError, YamlConfigProvider, resolve_config_directory

from .fakes import D

APP_SETTINGS = "general:\n  network_mode: DryRun\narbitrage:\n  min_spread_percent_up: 0.5\n"
EXCHANGES = "exchanges:\n  items: []\n"
SYMBOLS = "symbols:\n  quote_currencies: [USDT]\n  max_symbols: 400\n"


def write_files(directory: Path, notifications: str | None) -> None:
    (directory / "appsettings.yaml").write_text(APP_SETTINGS, encoding="utf-8")
    (directory / "exchanges.yaml").write_text(EXCHANGES, encoding="utf-8")
    (directory / "symbols.yaml").write_text(SYMBOLS, encoding="utf-8")
    if notifications is not None:
        (directory / "notifications.yaml").write_text(notifications, encoding="utf-8")


def load(directory: Path, environ: dict[str, str] | None = None) -> NotificationOptions:
    return YamlConfigProvider(str(directory), environ or {}).current.notifications


def test_missing_file_means_disabled_section(tmp_path):
    write_files(tmp_path, notifications=None)

    options = load(tmp_path)

    assert not options.enabled
    assert not options.is_usable
    assert options.api_base_url == DEFAULT_MAX_BASE_URL
    assert options.endpoint == MaxSendEndpoint.MESSAGES
    assert options.min_spread_percent is None
    assert options.signal_cooldown_seconds == 120


def test_section_is_bound_from_yaml(tmp_path):
    write_files(
        tmp_path,
        """
notifications:
  enabled: true
  bot_token: yaml.token
  admin_chat_id: 777
  endpoint: ChatsMessage
  min_spread_percent: 0.75
  use_markdown: true
  silent: true
  signal_cooldown_seconds: 30
  max_per_minute: 5
  max_retries: 1
  failure_circuit_limit: 3
  circuit_break_minutes: 15
  text_template: "{symbol} {net}"
""",
    )

    options = load(tmp_path)

    assert options.is_usable
    assert options.bot_token == "yaml.token"
    assert options.admin_chat_id == "777"
    assert options.endpoint == MaxSendEndpoint.CHATS_MESSAGE
    assert options.min_spread_percent == D("0.75")
    assert options.use_markdown
    assert options.silent
    assert options.signal_cooldown_seconds == 30
    assert options.max_per_minute == 5
    assert options.circuit_break_minutes == 15
    assert options.text_template == "{symbol} {net}"


def test_pascal_case_keys_from_previous_version_are_accepted(tmp_path):
    write_files(tmp_path, "Notifications:\n  Enabled: true\n  BotToken: legacy\n  AdminChatId: '1'\n  SignalCooldownSeconds: 7\n")

    options = load(tmp_path)

    assert options.is_usable
    assert options.bot_token == "legacy"
    assert options.signal_cooldown_seconds == 7


@pytest.mark.parametrize("variable", ["ARB_Notifications__BotToken", "ARB_NOTIFICATIONS__BOT_TOKEN"])
def test_environment_variable_overrides_token(tmp_path, variable):
    write_files(tmp_path, "notifications:\n  enabled: true\n  bot_token: from-file\n  admin_chat_id: '1'\n")

    assert load(tmp_path, {variable: "from-env"}).bot_token == "from-env"


def test_environment_overrides_nested_and_list_values(tmp_path):
    write_files(tmp_path, None)
    (tmp_path / "exchanges.yaml").write_text("exchanges:\n  items:\n    - id: okx\n    - id: gate\n", encoding="utf-8")
    environ = {
        "ARB_GENERAL__NETWORK_MODE": "live",
        "ARB_ARBITRAGE__EXECUTION__CHASE__MAX_STEPS": "3",
        "ARB_Exchanges__Items__1__TakerFeePercent": "0.07",
    }

    options = YamlConfigProvider(str(tmp_path), environ).current

    assert options.general.network_mode == NetworkMode.LIVE
    assert options.arbitrage.execution.chase is not None
    assert options.arbitrage.execution.chase.max_steps == 3
    assert options.exchanges.items[1].taker_fee_percent == D("0.07")


def test_changed_file_is_reloaded_and_broken_file_keeps_last_good_version(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "RELOAD_CHECK_SECONDS", 0.0)
    write_files(tmp_path, None)
    provider = YamlConfigProvider(str(tmp_path), {})
    assert provider.current.arbitrage.min_spread_percent_up == D("0.5")

    path = tmp_path / "appsettings.yaml"
    path.write_text(APP_SETTINGS.replace("0.5", "0.9"), encoding="utf-8")
    stamp = time.time() + 5
    os.utime(path, (stamp, stamp))
    assert provider.current.arbitrage.min_spread_percent_up == D("0.9")

    path.write_text("general: [broken", encoding="utf-8")
    os.utime(path, (stamp + 5, stamp + 5))
    assert provider.current.arbitrage.min_spread_percent_up == D("0.9")


def test_missing_required_file_is_reported(tmp_path):
    with pytest.raises(ConfigError):
        resolve_config_directory(str(tmp_path / "nope"))
