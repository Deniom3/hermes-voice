#!/usr/bin/env python3
"""Единый конфиг голосового хаба.

Источник значений — `/etc/hermes-voice/hub.json` (путь переопределяется
`HUB_CONFIG`). Секреты в конфиг не пишем: токен HA и пароль MQTT остаются в
`/etc/hermes-voice/ha.env` и читаются из окружения.

Почему один файл, а не россыпь `os.environ.get()`:

* настройки хаба, колонок, MQTT и реестр фраз лежат рядом — видно всю картину;
* фразы-триггеры перестают быть зашитыми в код;
* новый параметр не требует правки systemd-юнита и перезапуска с новым env.

Окружение всё ещё переопределяет конфиг (`HUB_<СЕКЦИЯ>_<КЛЮЧ>`), но это
только для разовой отладки: постоянные значения должны жить в JSON.
"""
from __future__ import annotations

import json
import logging
import os
import threading

log = logging.getLogger("config")

PATH = os.environ.get("HUB_CONFIG", "/etc/hermes-voice/hub.json")

# Кэш разобранного файла по mtime: чтение на каждую фразу не нужно, но и
# держать вечный кэш нельзя — правку конфига хочется видеть без рестарта.
_lock = threading.Lock()
_mtime: float | None = None
_data: dict = {}
_error: str = ""
_missing_logged = False


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("корень конфига должен быть объектом")
    return data


def reload() -> dict:
    """Прочитать файл заново, минуя кэш."""
    global _mtime, _data, _error, _missing_logged
    try:
        mtime = os.path.getmtime(PATH)
    except OSError:
        with _lock:
            _mtime, _data = None, {}
            _error = "файл не найден"
        # Один раз на процесс: иначе каждый get() пишет в журнал строку.
        if not _missing_logged:
            _missing_logged = True
            log.warning("конфиг %s не найден — работают значения по умолчанию", PATH)
        return {}

    if _mtime == mtime:
        return _data

    try:
        data = _read(PATH)
    except Exception as e:  # noqa: BLE001
        with _lock:
            _mtime, _error = mtime, str(e)
        # Битый конфиг не должен ронять хаб: остаёмся на прошлом удачном
        # снимке, а в журнал пишем один раз на изменение mtime.
        log.error("конфиг %s не разобран (%s) — оставляю прошлые значения",
                  PATH, e)
        return _data

    with _lock:
        _mtime, _data, _error = mtime, data, ""
    log.info("конфиг загружен: %s", PATH)
    return _data


def _env_key(keys: tuple[str, ...]) -> str:
    return "HUB_" + "_".join(k.upper() for k in keys)


def get(*keys: str, default=None):
    """Значение по пути `get("mqtt", "prefix", default="hermes/stations")`.

    Окружение переопределяет: `HUB_MQTT_PREFIX`. Пустая строка в env
    считается «не задано», иначе отладка нечаянно гасила бы настройку.
    """
    if not keys:
        raise ValueError("нужен хотя бы один ключ")
    env = os.environ.get(_env_key(keys))
    if env not in (None, ""):
        return env

    data = reload()
    node = data
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def as_int(*keys: str, default: int) -> int:
    try:
        return int(get(*keys, default=default))
    except (TypeError, ValueError):
        log.warning("конфиг: %s не число — беру %s", ".".join(keys), default)
        return default


def as_float(*keys: str, default: float) -> float:
    try:
        return float(get(*keys, default=default))
    except (TypeError, ValueError):
        log.warning("конфиг: %s не число — беру %s", ".".join(keys), default)
        return default


def as_list(*keys: str, default=()) -> list[str]:
    """Список строк из JSON-массива или строки через запятую."""
    value = get(*keys, default=None)
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return [str(x) for x in (default or ())]


def as_bool(*keys: str, default: bool) -> bool:
    """Булево из JSON или env: true/1/yes/on против false/0/no/off."""
    value = get(*keys, default=None)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    log.warning("конфиг: %s = %r не булево — беру %s", ".".join(keys), value, default)
    return default


def section(name: str) -> dict:
    """Целиком секция конфига (или пустой словарь)."""
    value = get(name, default={})
    return value if isinstance(value, dict) else {}


def intents() -> dict[str, dict]:
    """Реестр фраз-триггеров: каноническая фраза → условия совпадения."""
    value = get("intents", default={})
    return value if isinstance(value, dict) else {}

