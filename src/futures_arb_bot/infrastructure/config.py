"""
Загрузка конфигурации из YAML-файлов каталога config/:
appsettings.yaml — общие настройки и арбитраж, exchanges.yaml — биржи,
symbols.yaml — фильтры пар, notifications.yaml — (необязательный) раздел уведомлений.
Поверх файлов накладываются переменные окружения ARB_<РАЗДЕЛ>__<КЛЮЧ> (в том числе из .env).
Изменения файлов подхватываются «на лету».
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from futures_arb_bot.core.domain.options import BotOptions, normalize_key

FILES = ("appsettings.yaml", "exchanges.yaml", "symbols.yaml")
"""Обязательные файлы."""

OPTIONAL_FILES = ("notifications.yaml",)
"""Файлы, которых может не быть: разделы берутся из значений по умолчанию."""

ENV_PREFIX = "ARB_"
RELOAD_CHECK_SECONDS = 1.0
"""Как часто проверять, не изменились ли файлы конфигурации."""
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ConfigError(Exception):
    """Конфигурация не найдена или не читается."""


def load_env_files(config_dir: str | None = None) -> list[str]:
    """
    Подгрузить .env: из текущего каталога, из каталога config/ и над ним, из корня проекта.
    Уже заданные переменные окружения не перезаписываются.
    """
    candidates: list[Path] = [Path.cwd() / ".env"]
    if config_dir:
        resolved = Path(config_dir).resolve()
        candidates.extend([resolved / ".env", resolved.parent / ".env"])
    candidates.append(_PROJECT_ROOT / ".env")

    loaded: list[str] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if path.is_file():
            load_dotenv(path, override=False, encoding="utf-8")
            loaded.append(str(path))
    return loaded


def resolve_config_directory(configured: str | None) -> str:
    if configured and configured.strip():
        if not os.path.isdir(configured):
            raise ConfigError(f"Каталог конфигурации не найден: {configured}")
        return configured

    def complete(directory: Path) -> bool:
        return all((directory / f).is_file() for f in FILES)

    # порядок: текущий каталог → config/ вверх по дереву → рядом с пакетом (корень проекта)
    cwd = Path.cwd()
    candidates = [cwd, *(d / "config" for d in (cwd, *cwd.parents)), _PROJECT_ROOT, _PROJECT_ROOT / "config"]
    for candidate in candidates:
        if complete(candidate):
            return str(candidate)

    raise ConfigError(
        f"Не найдены файлы конфигурации: {', '.join(FILES)}. Запустите робота из каталога проекта или укажите каталог через --config-dir."
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: ожидается словарь разделов верхнего уровня")
    return data


def _merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    """Глубокое слияние словарей с сопоставлением ключей без учёта регистра."""
    index = {normalize_key(k): k for k in target if isinstance(k, str)}
    for key, value in source.items():
        existing = index.get(normalize_key(key)) if isinstance(key, str) else None
        if existing is not None and isinstance(target[existing], dict) and isinstance(value, Mapping):
            _merge(target[existing], value)
        elif existing is not None:
            target[existing] = value
        else:
            target[key] = value
            if isinstance(key, str):
                index[normalize_key(key)] = key


def _apply_env(data: dict[str, Any], environ: Mapping[str, str]) -> None:
    """ARB_General__NetworkMode=Live → data['general']['network_mode'] = 'Live' (индексы списков — числа)."""
    for name, value in environ.items():
        if not name.upper().startswith(ENV_PREFIX) or "__" not in name:
            continue
        path = [p for p in name[len(ENV_PREFIX) :].split("__") if p]
        if path:
            _set_path(data, path, value)


def _set_path(node: Any, path: list[str], value: str) -> None:
    head, rest = path[0], path[1:]
    if isinstance(node, list):
        if not head.isdigit():
            return
        index = int(head)
        while len(node) <= index:
            node.append({})
        if rest:
            if not isinstance(node[index], (dict, list)):
                node[index] = {}
            _set_path(node[index], rest, value)
        else:
            node[index] = value
        return

    if not isinstance(node, dict):
        return
    key = next((k for k in node if isinstance(k, str) and normalize_key(k) == normalize_key(head)), head)
    if rest:
        child = node.get(key)
        if not isinstance(child, (dict, list)):
            child = [] if rest[0].isdigit() else {}
            node[key] = child
        _set_path(child, rest, value)
    else:
        node[key] = value


class YamlConfigProvider:
    """Текущие настройки: файлы перечитываются, как только меняется время их изменения."""

    def __init__(self, directory: str, environ: Mapping[str, str] | None = None) -> None:
        self._directory = directory
        self._environ = dict(environ if environ is not None else os.environ)
        self._gate = threading.Lock()
        self._stamp: tuple[tuple[str, float | None], ...] | None = None
        self._current = self._load()
        self._next_check = time.monotonic() + RELOAD_CHECK_SECONDS

    @property
    def config_directory(self) -> str:
        return self._directory

    @property
    def current(self) -> BotOptions:
        # файлы проверяются не чаще раза в RELOAD_CHECK_SECONDS: настройки читаются очень часто
        now = time.monotonic()
        if now < self._next_check:
            return self._current
        self._next_check = now + RELOAD_CHECK_SECONDS

        stamp = self._make_stamp()
        if stamp != self._stamp:
            with self._gate:
                if stamp != self._stamp:
                    try:
                        self._current = self._load()
                    except Exception:
                        # битый файл при правке «на лету» — продолжаем на последней корректной версии
                        self._stamp = stamp
        return self._current

    def _make_stamp(self) -> tuple[tuple[str, float | None], ...]:
        result: list[tuple[str, float | None]] = []
        for name in (*FILES, *OPTIONAL_FILES):
            try:
                result.append((name, os.stat(os.path.join(self._directory, name)).st_mtime))
            except OSError:
                result.append((name, None))
        return tuple(result)

    def _load(self) -> BotOptions:
        stamp = self._make_stamp()
        data: dict[str, Any] = {}
        base = Path(self._directory)
        for name in FILES:
            path = base / name
            if not path.is_file():
                raise ConfigError(f"Не найден файл конфигурации: {path}")
            _merge(data, _read_yaml(path))
        for name in OPTIONAL_FILES:
            path = base / name
            if path.is_file():
                _merge(data, _read_yaml(path))

        _apply_env(data, self._environ)
        options = BotOptions.model_validate(data)
        self._stamp = stamp
        return options


def load(configured_directory: str | None) -> YamlConfigProvider:
    """Найти каталог конфигурации, подгрузить .env и прочитать YAML."""
    load_env_files(configured_directory)
    directory = resolve_config_directory(configured_directory)
    load_env_files(directory)
    return YamlConfigProvider(directory)
