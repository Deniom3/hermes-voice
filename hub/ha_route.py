#!/usr/bin/env python3
"""Быстрый домашний маршрут: команды и показания без LLM.

Источник истины — Home Assistant, а не отдельный конфиг:
  * `homeassistant/expose_entity/list` — что разрешено голосу;
  * `config/entity_registry/list` — имя и алиасы сущности;
  * `config/device_registry/list` — комната устройства;
  * `config/area_registry/list` — имена комнат;
  * `/api/states` — значения.

Порядок разбора: музыка и таймеры — не наши; вопрос о показании — читаем
сенсор; команда — находим сущность и зовём сервис; «включи свет» без имени —
правило комнаты колонки; остальное — None, дальше hassil или агент.

Модуль не знает про микрофон, TTS и состояния ядра: вход — текст, выход —
одна готовая фраза.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import aiohttp

try:                      # запуск как модуль
    from . import ha_morph
except ImportError:       # запуск как скрипт
    import ha_morph

log = logging.getLogger("ha-route")

ON_WORDS = ("включ", "зажги", "запусти", "поставь", "сделай", "установи", "подними")
OFF_WORDS = ("выключ", "погаси", "останови", "стоп", "закрой", "убери")
QUESTION_WORDS = ("сколько", "какая", "какой", "какие", "чему", "температур",
                  "влажност", "вода", "объем", "вес", "горит", "включен",
                  "работает", "состояние")
MUSIC_WORDS = ("музык", "песн", "радио", "трек", "альбом", "плейлист")
# Погоду отдаём hassil: у него готовый ответ «Сейчас плюс 13, облачно»,
# а наши сенсоры погоды дали бы сухое «Погода температура: 13 градусов».
WEATHER_WORDS = ("погод",)
SENSOR_WORDS = ("температур", "влажност", "вода", "объем", "вес", "градус")

ACTUATOR_DOMAINS = {"light", "switch", "climate", "water_heater", "vacuum",
                    "fan", "cover", "media_player", "scene", "humidifier"}

_NUM_RE = re.compile(r"\b(\d{1,3})\b")

_UNIT_WORDS = {
    "L": ("литр", "литра", "литров"),
    "°C": ("градус", "градуса", "градусов"),
    "%": ("процент", "процента", "процентов"),
    "g": ("грамм", "грамма", "граммов"),
    "kg": ("килограмм", "килограмма", "килограммов"),
}


def plural(n: float, forms: tuple[str, str, str]) -> str:
    """Русская форма слова для числа: 1 литр, 2 литра, 5 литров.

    Дробное значение идёт с формой родительного падежа: «0,92 литра».
    """
    if n != int(n):
        return forms[1]
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return forms[2]
    n %= 10
    if n == 1:
        return forms[0]
    if 2 <= n <= 4:
        return forms[1]
    return forms[2]


def fmt_number(value: float, decimals: int) -> str:
    """Целые значения без дробной части: «90», а не «90,0»."""
    if decimals <= 0 or float(value).is_integer():
        return str(int(round(value)))
    return f"{round(value, decimals):.{decimals}f}".replace(".", ",")


def unit_words(unit: str) -> tuple[str, str, str]:
    return _UNIT_WORDS.get(unit, ("", "", ""))


class HAClient:
    """REST + WebSocket-клиент Home Assistant."""

    def __init__(self, url: str, token: str, timeout: float = 1.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": "Bearer " + self.token},
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        return self._session

    async def states(self) -> dict[str, dict]:
        async with (await self._s()).get(self.url + "/api/states") as r:
            if r.status != 200:
                raise RuntimeError(f"states: HTTP {r.status}")
            return {x["entity_id"]: x for x in await r.json()}

    async def state(self, entity_id: str) -> dict | None:
        async with (await self._s()).get(self.url + f"/api/states/{entity_id}") as r:
            if r.status != 200:
                return None
            return await r.json()

    async def call(self, domain: str, service: str, data: dict) -> bool:
        async with (await self._s()).post(
            self.url + f"/api/services/{domain}/{service}", json=data
        ) as r:
            if r.status != 200:
                log.warning("HA %s.%s → HTTP %s", domain, service, r.status)
                return False
            await r.read()
            return True

    async def registry(self) -> dict:
        """Экспозиция и реестры одним WebSocket-сеансом."""
        ws_url = self.url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url += "/api/websocket"
        out: dict = {}
        async with (await self._s()).ws_connect(ws_url) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": self.token})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                raise RuntimeError("HA auth failed")
            commands = (
                ("exposed", "homeassistant/expose_entity/list"),
                ("entities", "config/entity_registry/list"),
                ("devices", "config/device_registry/list"),
                ("areas", "config/area_registry/list"),
            )
            for i, (key, cmd) in enumerate(commands, 1):
                await ws.send_json({"id": i, "type": cmd})
                res = await ws.receive_json()
                out[key] = res.get("result")
        return out

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


def build_inventory(states: dict[str, dict], reg: dict,
                    station_entity: str = "") -> tuple[list[ha_morph.Device], dict, str]:
    """Собрать разрешённые сущности, комнаты и комнату колонки из реестров."""
    exposed = (reg.get("exposed") or {}).get("exposed_entities") or {}
    entities = {e["entity_id"]: e for e in (reg.get("entities") or [])}
    devices = {d["id"]: d for d in (reg.get("devices") or [])}
    areas = {a["area_id"]: a.get("name", a["area_id"]) for a in (reg.get("areas") or [])}

    out: list[ha_morph.Device] = []
    for entity_id, flags in exposed.items():
        if not isinstance(flags, dict) or not flags.get("conversation"):
            continue
        state = states.get(entity_id)
        if state is None:
            continue
        entry = entities.get(entity_id, {})
        if entry.get("entity_category") or entry.get("hidden_by") or entry.get("disabled_by"):
            continue
        domain = entity_id.split(".", 1)[0]
        device = devices.get(entry.get("device_id") or "", {})
        area_id = entry.get("area_id") or device.get("area_id") or ""
        attrs = state.get("attributes") or {}
        name = attrs.get("friendly_name") or entry.get("name") or entity_id.split(".", 1)[1]
        if domain == "weather":
            kind = "weather"
        elif domain in ("sensor", "binary_sensor"):
            kind = "sensor"
        elif domain in ACTUATOR_DOMAINS:
            kind = "actuator"
        else:
            continue
        out.append(ha_morph.Device(
            entity_id=entity_id,
            name=name,
            area=area_id,
            area_name=areas.get(area_id, ""),
            synonyms=tuple(entry.get("aliases") or ()),
            role=domain,
            kind=kind,
            unit=str(attrs.get("unit_of_measurement") or ""),
        ))

    default_area = ""
    station = station_entity or os.environ.get("STATION_ENTITY", "")
    if station:
        entry = entities.get(station, {})
        device = devices.get(entry.get("device_id") or "", {})
        default_area = entry.get("area_id") or device.get("area_id") or ""
    if not default_area:
        for dev in out:
            if dev.role == "media_player" and dev.area:
                default_area = dev.area
                break
    return out, areas, default_area


class HomeRoute:
    """Решение «это быстрая домашняя фраза или нет»."""

    CACHE_S = 60.0

    def __init__(self, client: HAClient, station_entity: str = ""):
        self.client = client
        self.station_entity = station_entity
        self.matcher = ha_morph.Matcher([])
        self.areas: dict[str, str] = {}
        self.area_aliases: dict[str, str] = {}
        self.default_area = ""
        self._cached_at = 0.0

    # ---------- инвентарь ----------

    async def _refresh(self, force: bool = False) -> None:
        loop = asyncio.get_running_loop()
        if not force and loop.time() - self._cached_at < self.CACHE_S:
            return
        states = await self.client.states()
        reg = await self.client.registry()
        devices, areas, default_area = build_inventory(states, reg, self.station_entity)
        self.matcher = ha_morph.Matcher(devices)
        self.areas = areas
        self.area_aliases = {}
        for area in (reg.get("areas") or []):
            for alias in (area.get("aliases") or ()):
                self.area_aliases[alias] = area["area_id"]
        self.default_area = default_area
        self._cached_at = loop.time()

    def area_in_text(self, norm: str) -> str | None:
        """Комната, названная в фразе: «в комнате», «на кухне»."""
        phrase = set(ha_morph.match_stems(norm))
        for area_id, name in self.areas.items():
            need = set(ha_morph.match_stems(name))
            if need and need <= phrase:
                return area_id
        for alias, area_id in self.area_aliases.items():
            need = set(ha_morph.match_stems(alias))
            if need and need <= phrase:
                return area_id
        return None

    # ---------- маршрут ----------

    async def route(self, text: str, area: str | None = None) -> str | None:
        norm = ha_morph.normalize(text)
        if not norm:
            return None
        if any(w in norm for w in MUSIC_WORDS):
            return None
        if any(w in norm for w in WEATHER_WORDS):
            return None
        try:
            await self._refresh()
        except Exception as e:  # noqa: BLE001
            log.warning("инвентарь HA недоступен: %s", e)
            return None
        explicit_area = self.area_in_text(norm)
        self.last_kind = None
        if any(w in norm for w in QUESTION_WORDS):
            answer = await self._read(norm, explicit_area)
            if answer is not None:
                self.last_kind = "reading"
                return answer
            if any(w in norm for w in SENSOR_WORDS):
                return None
        if self._looks_like_command(norm):
            answer = await self._act(norm, explicit_area or area or self.default_area)
            if answer is not None:
                self.last_kind = "command"
            return answer
        return None

    @staticmethod
    def _looks_like_command(norm: str) -> bool:
        # «верни на базу» — тоже команда, хотя не содержит «включи»/«выключи».
        return any(w in norm for w in (
            ON_WORDS + OFF_WORDS + ("яркост", "ярче", "тише", "верн", "домой", "баз")))

    # ---------- показания ----------

    async def _read(self, norm: str, area: str | None) -> str | None:
        match = self.matcher.best(norm, kind="sensor", area=area)
        if match is None and area is None and self.default_area:
            # Комнату не назвали, а датчиков с таким именем несколько
            # («Температура» в комнате, коридоре и на кухне). Берём комнату,
            # где стоит колонка — это контекст спутника, а не догадка.
            match = self.matcher.best(norm, kind="sensor", area=self.default_area)
            if match is not None:
                log.info("уточнил комнатой колонки: %s", self.default_area)
        if match is None:
            return None
        device = match.device
        state = await self.client.state(device.entity_id)
        if state is None or state.get("state") in ("unavailable", "unknown", ""):
            return f"{device.name}: устройство недоступно"
        try:
            value = float(str(state["state"]).replace(",", "."))
        except (TypeError, ValueError):
            return None
        unit = device.unit or str((state.get("attributes") or {}).get("unit_of_measurement") or "")
        words = unit_words(unit)
        decimals = 2 if unit == "L" else (1 if unit == "°C" else 0)
        number = fmt_number(value, decimals)
        # Комнату добавляем только к коротким именам («Температура», «Влажность»):
        # «Объем воды в чайнике (Кухня)» звучит хуже, чем без уточнения.
        short_name = len(ha_morph.match_stems(device.name)) <= 1
        where = f" ({device.area_name})" if short_name and device.area_name else ""
        return f"{device.name}{where}: {number} {plural(round(value, decimals), words)}".strip()

    # ---------- команды ----------

    async def _act(self, norm: str, area: str) -> str | None:
        number = self._number(norm)
        role = self._role(norm, number)
        match = self.matcher.best(norm, role=role, kind="actuator")
        if match is None:
            if "свет" in norm and area:
                return await self._area_light(norm, area)
            return None
        device = match.device
        if device.role == "vacuum":
            return await self._vacuum(norm, device)
        on = not any(w in norm for w in OFF_WORDS)
        if device.role == "light" and "яркост" in norm and number is not None:
            return await self._light_brightness(device, number)
        if number is not None and device.role in ("climate", "water_heater"):
            return await self._set_temperature(device, number)
        return await self._switch(device, on)

    @staticmethod
    def _number(norm: str) -> int | None:
        m = _NUM_RE.search(norm)
        return int(m.group(1)) if m else None

    @staticmethod
    def _role(norm: str, number: int | None) -> str | None:
        if "пылесос" in norm or "робот" in norm:
            return "vacuum"
        if any(w in norm for w in ("кондиционер", "кондей", "сплит", "кондишн")):
            return "climate"
        if "чайник" in norm:
            return "water_heater" if number is not None and "градус" in norm else "switch"
        if any(w in norm for w in ("ламп", "люстр", "свет")):
            return "light"
        return None

    async def _switch(self, device: ha_morph.Device, on: bool) -> str | None:
        service = "turn_on" if on else "turn_off"
        ok = await self.client.call(device.role, service, {"entity_id": device.entity_id})
        if not ok:
            return None
        await asyncio.sleep(0.05)
        state = await self.client.state(device.entity_id)
        want = "on" if on else "off"
        if state is not None and state.get("state") not in (want, "unavailable", "unknown"):
            log.warning("%s: ожидали %s, получили %s", device.entity_id, want, state.get("state"))
        # Имя устройства в подтверждении лишнее: результат видно, а фраза
        # «включил настольная лампа» звучит как отчёт робота.
        return "Включил" if on else "Выключил"

    async def _light_brightness(self, device: ha_morph.Device, pct: int) -> str:
        pct = max(1, min(100, pct))
        await self.client.call("light", "turn_on", {
            "entity_id": device.entity_id, "brightness_pct": pct,
        })
        return f"Яркость {pct} процентов: {device.name}"

    async def _set_temperature(self, device: ha_morph.Device, value: int) -> str | None:
        ok = await self.client.call(device.role, "set_temperature", {
            "entity_id": device.entity_id, "temperature": value,
        })
        if not ok:
            return None
        return f"Поставил {value} {plural(value, ('градус', 'градуса', 'градусов'))}"

    async def _vacuum(self, norm: str, device: ha_morph.Device) -> str | None:
        if any(w in norm for w in ("домой", "баз", "верн")):
            ok = await self.client.call("vacuum", "return_to_base", {"entity_id": device.entity_id})
            return "Отправил пылесос на базу" if ok else None
        if any(w in norm for w in OFF_WORDS):
            ok = await self.client.call("vacuum", "stop", {"entity_id": device.entity_id})
            return "Остановил пылесос" if ok else None
        state = await self.client.state(device.entity_id)
        if state is not None and state.get("state") in ("unavailable", "unknown"):
            return "Пылесос сейчас недоступен"
        ok = await self.client.call("vacuum", "start", {"entity_id": device.entity_id})
        return "Запустил пылесос" if ok else None

    async def _area_light(self, norm: str, area: str) -> str | None:
        on = not any(w in norm for w in OFF_WORDS)
        ok = await self.client.call("light", "turn_on" if on else "turn_off", {"area_id": area})
        if not ok:
            return None
        return "Включил свет" if on else "Выключил свет"
