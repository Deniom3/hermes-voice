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
import os

try:                      # запуск как модуль
    from . import hermes_ws
except ImportError:       # запуск как скрипт
    import hermes_ws

log = logging.getLogger("fast-route")

STATION_DEFAULT = "media_player.yandex_station_lite"
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
    _route = ha_route.HomeRoute(
        ha_route.HAClient(url, token),
        station_entity=os.environ.get("STATION_ENTITY", STATION_DEFAULT),
    )
    return _route


async def stream(text: str, on_event=None):
    """Поток ответа: быстрый маршрут или обычный агент."""
    fast = route()
    if fast is not None:
        try:
            answer = await fast.route(text)
        except Exception as e:  # noqa: BLE001
            log.warning("быстрый маршрут упал: %s", e)
            answer = None
        if answer:
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
