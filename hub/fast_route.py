#!/usr/bin/env python3
"""Обёртка «сначала быстрый дом, потом агент».

Подменяет `stream_fn` в `voice_dialog.DialogTurn`: одна фраза проходит через
HomeRoute (команды и показания без LLM), а если маршрут вернул None — уходит
обычному агенту через `hermes_ws.stream`.

Если переменные HA не заданы или HA недоступен, поведение не меняется —
фраза сразу идёт агенту.
"""
from __future__ import annotations

import logging
import contextvars
import os

try:                      # запуск как модуль
    from . import hermes_ws
except ImportError:       # запуск как скрипт
    import hermes_ws

log = logging.getLogger("fast-route")

# Какая колонка сейчас говорит: нужно, чтобы громкость ставилась именно ей.
STATION = contextvars.ContextVar("station", default=None)

# Чем начат ход: wake / barge / dialog / followup / button. Перебивание — это
# разговорный ход, и быстрый дом на нём ловит обсуждение и обрывки ASR
# («а если включить лампу?» — уже команда). Поэтому barge идёт только в агент.
ORIGIN = contextvars.ContextVar("origin", default=None)


def set_station(name: str | None) -> None:
    STATION.set(name)


def set_origin(origin: str | None) -> None:
    ORIGIN.set(origin)
_route = None
_failed = False


def route():
    """Ленивая инициализация: без HASS_URL/HASS_TOKEN модуль выключен.

    Импорт ha_route (и aiohttp) — внутри функции: панель работает на системном
    python без aiohttp, и импорт модуля не должен её ломать.
    """
    global _route, _failed
    if _route is not None or _failed:
        return _route
    url = os.environ.get("HASS_URL", "")
    token = os.environ.get("HASS_TOKEN", "")
    if not url or not token:
        log.info("HASS_URL/HASS_TOKEN не заданы — быстрый дом выключен")
        _failed = True
        return None
    try:
        try:
            from . import ha_route
        except ImportError:
            import ha_route
    except Exception as e:  # noqa: BLE001
        log.warning("ha_route недоступен: %s", e)
        _failed = True
        return None
    volume_source = volume_setter = None
    try:
        from . import voice_hub
    except Exception:  # noqa: BLE001
        try:
            import voice_hub
        except Exception:  # noqa: BLE001
            voice_hub = None
            log.debug("хаб недоступен — громкость читаем из HA")
    if voice_hub is not None:
        volume_source = getattr(voice_hub, "station_volume", None)
        volume_setter = getattr(voice_hub, "station_set_volume", None)
    _route = ha_route.HomeRoute(
        ha_route.HAClient(url, token),
        volume_source=volume_source,
        volume_setter=volume_setter,
    )
    return _route


async def stream(text: str, on_event=None):
    """Поток ответа: быстрый маршрут или обычный агент."""
    # Перебивание — продолжение разговора, а не команда: быстрый дом здесь
    # перехватывает обсуждение и обрывки распознавания. Ведём сразу к агенту.
    fast = None if ORIGIN.get() == "barge" else route()
    if fast is not None:
        try:
            answer = await fast.route(text, station=STATION.get())
        except Exception as e:  # noqa: BLE001
            log.warning("быстрый маршрут упал: %s", e)
            answer = None
        if answer is not None:
            if getattr(fast, "last_kind", None) == "command":
                # Быстрая команда: действие уже выполнено, но вслух не отчитываемся.
                log.info("команда выполнена молча: %r", answer)
                try:
                    from . import voice_dialog as vd
                except ImportError:
                    import voice_dialog as vd
                yield vd.SILENT
                return
            log.info("быстрый ответ: %r", answer)
            yield answer
            return
    if on_event is None:
        async for piece in hermes_ws.stream(text):
            yield piece
    else:
        async for piece in hermes_ws.stream(text, on_event=on_event):
            yield piece
