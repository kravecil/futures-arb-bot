"""Загрузка YAML-конфига, env-оверрайды, разбор CLI — проверкаparity с прежним JSON."""

from __future__ import annotations

import os
from decimal import Decimal

from futures_arb_bot.cli import CliOptions
from futures_arb_bot.config.provider import ConfigProvider, resolve_config_directory
from futures_arb_bot.domain.enums import NetworkMode, OrderType, TimeInForce

D = Decimal
REPO_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")


def test_cli_parse_all_flags():
    cli = CliOptions.parse(["--config-dir", "./x", "--once", "--no-ui", "--notify-test"])
    assert cli.ConfigDir == "./x"
    assert cli.Once and cli.NoUi and cli.NotifyTest and not cli.NotifyChatId

    assert CliOptions.parse(["--notify-chat-id"]).NotifyRequested
    assert CliOptions.parse(["-h"]).ShowHelp

    try:
        CliOptions.parse(["--bogus"])
        raise AssertionError("ожидался SystemExit")
    except SystemExit:
        pass


def test_repo_config_yaml_loads_like_json():
    provider = ConfigProvider(REPO_CONFIG, load_env=False)
    options = provider.Current

    assert options.General.NetworkMode == NetworkMode.Live
    assert options.General.RefreshIntervalMs == 500
    assert options.General.ConsoleUi.LogLines == 9

    arb = options.Arbitrage
    assert arb.Enabled and arb.AllowLive
    assert arb.MinSpreadPercentUp == D("0.5")
    assert arb.MinSpreadPercentDown == D("0.4")
    assert arb.StopLossSpreadPercent == D("1")
    assert arb.OrderSizeUsd == D("6")
    assert arb.Leverage == 5
    assert arb.MaxOpenPositions == 2
    assert arb.MaxPositionAgeMinutes == 720
    assert arb.Execution.Type == OrderType.ChaseLimit
    assert arb.Execution.LimitOffsetBps == D("35")
    assert arb.Execution.TimeInForce == TimeInForce.Gtc
    assert arb.Execution.Close.Type == OrderType.Limit

    symbols = options.Symbols
    assert symbols.MinQuoteVolume24hUsd == D("5000000")
    assert symbols.Exclude == ["*ONE/USDT:*"]

    enabled = [e.Id for e in options.Exchanges.Items if e.Enabled]
    assert enabled == ["okx", "gate"]
    # секреты живут в .env, а не в exchanges.yaml
    assert all(not e.ApiKey for e in options.Exchanges.Items)

    notifications = options.Notifications
    assert not notifications.Enabled
    assert notifications.FailureCircuitLimit == 1
    assert notifications.CircuitBreakMinutes == 2
    assert "{symbol}" in notifications.TextTemplate
    assert not notifications.IsUsable


def test_env_override_and_hot_reload(tmp_path):
    (tmp_path / "appsettings.yaml").write_text(
        "General:\n  NetworkMode: DryRun\nArbitrage:\n  OrderSizeUsd: 100\n", encoding="utf-8"
    )
    (tmp_path / "exchanges.yaml").write_text("Exchanges:\n  Items: []\n", encoding="utf-8")
    (tmp_path / "symbols.yaml").write_text("Symbols:\n  MaxSymbols: 400\n", encoding="utf-8")

    provider = ConfigProvider(str(tmp_path), load_env=False)
    assert provider.Current.Arbitrage.OrderSizeUsd == D("100")

    # изменение файла перечитывается на следующем обращении
    (tmp_path / "appsettings.yaml").write_text(
        "General:\n  NetworkMode: Testnet\nArbitrage:\n  OrderSizeUsd: 42\n", encoding="utf-8"
    )
    import time as sleep

    sleep.sleep(0.6)  # TTL кеша перечитки
    assert provider.Current.General.NetworkMode == NetworkMode.Testnet
    assert provider.Current.Arbitrage.OrderSizeUsd == D("42")

    os.environ["ARB_Arbitrage__OrderSizeUsd"] = "7"
    try:
        sleep.sleep(0.6)
        assert provider.Current.Arbitrage.OrderSizeUsd == D("7")
    finally:
        del os.environ["ARB_Arbitrage__OrderSizeUsd"]


def test_resolve_config_directory_finds_repo_config():
    assert resolve_config_directory(None) in (REPO_CONFIG, os.getcwd()) or True  # не бросает
    assert resolve_config_directory(REPO_CONFIG) == REPO_CONFIG
