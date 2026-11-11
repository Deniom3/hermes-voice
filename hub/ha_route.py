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
import re
import time

import aiohttp

try:                      # запуск как модуль
    from . import config
    from . import ha_morph
except ImportError:       # запуск как скрипт
    import config
    import ha_morph

log = logging.getLogger("ha-route")

def _reply(key: str) -> str:
    """Текст произносимой реплики — только из hub.json.

    Раньше ответы жили строками в коде: правка «Включил» → «Готово» требовала
    коммита и пересборки, а перевод/стиль нельзя было поменять на месте.
    Отсутствие ключа — внятная ошибка: молча говорить пустоту хуже, чем упасть
    на старте.
    """
    text = config.get("replies", key, default=None)
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError(
            f"hub.json: replies.{key} не задан — нечего произносить")
    return text


def _fmt(key: str, **kw) -> str:
    """Реплика с подстановкой значений (громкость, яркость, имя устройства)."""
    return _reply(key).format(**kw)


def _words(key: str) -> tuple[str, ...]:
    """Список речевых форм из `phrases.<key>` в hub.json.

    Отсутствие ключа — отказ, а не пустой список: с пустым списком команда
    молча уходит в LLM, и это выглядит как «перестало понимать», а не как
    ошибка конфига.
    """
    raw = config.as_list("phrases", key, default=None)
    if not raw:
        raise RuntimeError(
            f"hub.json: phrases.{key} не задан — быстрый маршрут без него не работает")
    return tuple(str(x) for x in raw)


def _mapping(key: str) -> dict:
    raw = config.get("phrases", key, default=None)
    if not isinstance(raw, dict) or not raw:
        raise RuntimeError(
            f"hub.json: phrases.{key} не задан — быстрый маршрут без него не работает")
    return dict(raw)


ON_WORDS = _words("on")
OFF_WORDS = _words("off")
QUESTION_WORDS = _words("question")
MUSIC_WORDS = _words("music")
# Погоду отдаём hassil: у него готовый ответ «Сейчас плюс 13, облачно»,
# а наши сенсоры погоды дали бы сухое «Погода температура: 13 градусов».
WEATHER_WORDS = _words("weather")
SENSOR_WORDS = _words("sensor")

# Громкость станции: у колонки 10 уровней плюс мьют, поэтому шаг — 10%.
# Шкалы доменов разные и не пересекаются: громкость станции — 1..10,
# яркость — 1..100. Разбор числа общий, а допустимый диапазон — свой.
VOLUME_MIN, VOLUME_MAX = 1, 10
# Что сказать, когда комнаты у колонки нет, а без неё команду не выполнить:
# текст живёт в hub.json → replies.no_area (раньше был строкой здесь).

VOLUME_CACHE_S = 20.0  # сколько верим собственному последнему значению
BRIGHTNESS_MIN, BRIGHTNESS_MAX = 1, 100

VOLUME_WORDS = _words("volume")
MUTE_WORDS = _words("mute")
# Группы слов, без которых маршрут не работает — тоже из hub.json.
COMMAND_EXTRA_WORDS = _words("command_extra")
VOLUME_QUERY_WORDS = _words("volume_query")
VOLUME_MAX_WORDS = _words("volume_max")
ROLE_VACUUM_WORDS = _words("role_vacuum")
ROLE_CLIMATE_WORDS = _words("role_climate")
ROLE_WATER_WORDS = _words("role_water")
ROLE_LIGHT_WORDS = _words("role_light")
UP_WORDS = _words("up")
DOWN_WORDS = _words("down")

ACTUATOR_DOMAINS = set(_words("actuator_domains"))

_NUM_RE = re.compile(r"\b(\d{1,3})\b")

# ASR часто отдаёт число словом: «Громкость два». Без этого фраза уходила в LLM.
WORD_NUMBERS = _mapping("word_numbers")

_UNIT_WORDS = {str(k): tuple(v) for k, v in _mapping("units").items()}


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


def build_inventory(states: dict[str, dict], reg: dict) -> tuple[list[ha_morph.Device], dict]:
    """Собрать разрешённые сущности и комнаты из реестров."""
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

    return out, areas


def station_area_for(name: str | None) -> str:
    """Комната колонки: `wake.stations.<имя>.area` в hub.json.

    Пишется прямо в конфиге, без вычислений из HA: у каждой станции своя
    комната, и её видно в одном файле. Значение — `area_id` из реестра HA
    («spalnia», «kukhnia»); если такого нет, это ошибка конфига, а не повод
    угадывать.
    """
    if not name:
        return ""
    return str(config.get("wake", "stations", name, "area", default="") or "")


class HomeRoute:
    """Решение «это быстрая домашняя фраза или нет»."""

    CACHE_S = 60.0

    def __init__(self, client: HAClient, volume_source=None, volume_setter=None):
        self.client = client
        # Колбэк «фактическая громкость станции» (агент через хаб). Без него
        # читаем number.*_volume из HA — там значение отстаёт до 5 с.
        self.volume_source = volume_source
        # Единый путь установки: ядро → агент станции. Облако обновляет сам
        # maind, отдельная синхронизация через HA больше не нужна.
        self.volume_setter = volume_setter
        self.matcher = ha_morph.Matcher([])
        self.areas: dict[str, str] = {}
        self.area_aliases: dict[str, str] = {}
        self.default_area = ""
        self._cached_at = 0.0
        self._vol_entity = ""

    # ---------- инвентарь ----------

    async def _refresh(self, force: bool = False) -> None:
        loop = asyncio.get_running_loop()
        if not force and loop.time() - self._cached_at < self.CACHE_S:
            return
        states = await self.client.states()
        reg = await self.client.registry()
        devices, areas = build_inventory(states, reg)
        self.matcher = ha_morph.Matcher(devices)
        self.areas = areas
        self.area_aliases = {}
        for area in (reg.get("areas") or []):
            for alias in (area.get("aliases") or ()):
                self.area_aliases[alias] = area["area_id"]
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

    async def route(self, text: str, area: str | None = None,
                    station: str | None = None) -> str | None:
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
        self.default_area = station_area_for(station)
        if self.default_area and self.default_area not in self.areas:
            log.warning("wake.stations.%s.area=%r нет в реестре HA — комната не применяется",
                        station, self.default_area)
            self.default_area = ""
        explicit_area = self.area_in_text(norm)
        self.last_kind = None
        self._vol_kind = None
        volume = await self._volume(norm, station)
        if volume is not None:
            self.last_kind = self._vol_kind or "command"
            self._vol_kind = None
            return volume
        if any(w in norm for w in QUESTION_WORDS):
            answer = await self._read(norm, explicit_area)
            if answer is not None:
                self.last_kind = "reading"
                return answer
            if any(w in norm for w in SENSOR_WORDS):
                return self._room_hint(norm, kind="sensor")
        if self._looks_like_command(norm):
            answer = await self._act(norm, explicit_area or area or self.default_area)
            if answer is not None:
                self.last_kind = "command"
                return answer
            return self._room_hint(norm)
        return None

    def _room_hint(self, norm: str, kind: str | None = None) -> str | None:
        """Подсказка вместо молчания, когда не хватает комнаты.

        Условие строгое: комнаты у колонки нет, а подходящие устройства
        нашлись в двух разных комнатах — значит именно комната и есть
        неизвестное. Если устройство одно или все в одной комнате, подсказка
        не нужна: маршрут либо сработал, либо это не наша фраза.
        """
        if self.default_area:
            return None
        found = [m for m in self.matcher.find(norm, kind=kind)
                 if m.cover >= self.matcher.MIN_COVER]
        areas = {m.device.area for m in found if m.device.area}
        if len(areas) < 2:
            return None
        log.info("нужна комната: %r подходит в %d комнатах, у станции area не задана",
                 norm, len(areas))
        return _reply("no_area")

    @staticmethod
    def _looks_like_command(norm: str) -> bool:
        # «верни на базу» — тоже команда, хотя не содержит «включи»/«выключи».
        return any(w in norm for w in (ON_WORDS + OFF_WORDS + COMMAND_EXTRA_WORDS))

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
            return _fmt("unavailable", device=device.name)
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

    # ---------- громкость ----------

    async def _volume_entity(self, station: str | None = None) -> str | None:
        """Сущность громкости станции: number.hermes_<station>_volume.

        Имя станции обязательно: иначе при двух колонках бралась первая
        hermes_*_volume и «какая громкость» отвечала про чужую колонку.
        """
        try:
            states = await self.client.states()
        except Exception as e:  # noqa: BLE001
            # HA недоступен — это не повод не менять громкость: уровень
            # читается у агента, а HA нужен только для учёта в облаке.
            log.debug("громкость: HA недоступен (%s) — читаю у агента", e)
            return None
        if station:
            slug = station.replace("-", "_")
            cand = f"number.hermes_{slug}_volume"
            if cand in states:
                return cand
            return None
        if self._vol_entity:
            return self._vol_entity
        cands = [e for e in states if e.startswith("number.") and e.endswith("_volume")]
        if not cands:
            return None
        # Своя сущность приоритетнее любой чужой *_volume
        cands.sort(key=lambda e: (0 if "hermes_" in e else 1, e))
        self._vol_entity = cands[0]
        return self._vol_entity

    async def _set_volume(self, entity: str | None, pct: int,
                          station: str | None = None) -> str | None:
        """Поставить громкость станции: решение и разбор — здесь, исполнение — агент.

        Единый путь для голоса, MQTT и панели: уровень уходит агенту, тот
        пишет директиву `VolumeSet` в `maind`, а `maind` сам обновляет облако.
        Отдельной синхронизации через HA больше нет: она дублировала то, что
        `maind` делает сам, и расходилась с ним при ошибке.
        """
        level = max(0, min(10, int(pct)))
        if not station:
            # Без имени колонки менять громкость нельзя: при двух станциях это
            # означало бы «какой-нибудь», и уже приводило к чужой колонке.
            log.warning("громкость: станция не определена — не меняю")
            return None
        if self.volume_setter is None:
            # Второй путь через `number.hermes_*_volume` не берём: эта сущность —
            # наш же MQTT-мост, команда возвращается в station_set_volume, то
            # есть в того же агента. Без агента она не ставит ничего, а с ним
            # добавляет круг HA → MQTT → хаб и врёт, что громкость изменена.
            log.warning("громкость: агент не подключён — не меняю")
            return None
        ok = await self.volume_setter(level, station)
        if not ok:
            return None
        # Агент и HA узнают новое значение с задержкой (опрос раз в 5 с),
        # поэтому «какая громкость» сразу после команды отвечала старым числом.
        self._vol_last = (level, time.monotonic())
        # Контракт один, как у Алисы: уровни 1–10 и мьют. Процентов в речи нет.
        if level == 0:
            return _reply("muted")
        return _fmt("volume", level=level)

    async def _volume(self, norm: str, station: str | None = None) -> str | None:
        """«громкость 4», «прибавь», «тише на 2», «выключи звук» — без LLM."""
        if not any(w in norm for w in VOLUME_WORDS):
            return None
        entity = None
        cur = None
        # Сначала факт от агента: Алиса и кнопки меняют микшер сами, и любое
        # кэшированное значение может быть устаревшим. Кэш — только резерв,
        # если агент не ответил (раньше он был приоритетом 20 с, и после
        # убавления голосом Алисы ответ оставался старым).
        if self.volume_source is not None:
            try:
                cur = await self.volume_source(station)
            except Exception as e:  # noqa: BLE001
                log.debug("громкость от агента: %s", e)
                cur = None
        if cur is None:
            last = getattr(self, "_vol_last", None)
            if last and time.monotonic() - last[1] < VOLUME_CACHE_S:
                cur = last[0]
        if cur is None:
            # Резерв только на чтение: агент не ответил, берём число из HA.
            # На изменение это не влияет — оно всегда идёт агенту.
            entity = await self._volume_entity(station)
            state = await self.client.state(entity) if entity else None
            if state is None:
                log.warning("громкость: %s недоступна в HA — фраза уйдёт агенту", entity)
                return None
            try:
                cur = max(0, min(10, int(round(float(state["state"])))))
            except (TypeError, ValueError):
                cur = 0

        if any(w in norm for w in VOLUME_QUERY_WORDS):
            # «какая громкость сейчас» — быстрый ответ, без LLM и без изменений.
            # Это чтение, а не команда: иначе хаб гасит ответ как «сделано молча».
            self._vol_kind = "reading"
            if cur == 0:
                return _reply("muted")
            return _fmt("volume", level=cur)

        if any(w in norm for w in MUTE_WORDS):
            return await self._set_volume(entity, 0, station)
        if any(w in norm for w in VOLUME_MAX_WORDS):
            return await self._set_volume(entity, 10, station)

        number = self._number(norm)
        up = any(w in norm for w in UP_WORDS)
        down = any(w in norm for w in DOWN_WORDS)
        if number is not None and not VOLUME_MIN <= number <= VOLUME_MAX:
            # Недопустимый уровень: не применяем и молчим. Пустая строка +
            # last_kind=command — это тишина, а не уход в LLM.
            return ""
        if number is not None:
            target = cur + number if up else (cur - number if down else number)
        elif up:
            target = cur + 1
        elif down:
            target = cur - 1
        else:
            return None
        return await self._set_volume(entity, target, station)

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
        if m:
            return int(m.group(1))
        for word in norm.split():
            word = word.strip(".,!?;:")
            if word in WORD_NUMBERS:
                return WORD_NUMBERS[word]
        return None

    @staticmethod
    def _role(norm: str, number: int | None) -> str | None:
        if any(w in norm for w in ROLE_VACUUM_WORDS):
            return "vacuum"
        if any(w in norm for w in ROLE_CLIMATE_WORDS):
            return "climate"
        if any(w in norm for w in ROLE_WATER_WORDS):
            return "water_heater" if number is not None and "градус" in norm else "switch"
        if any(w in norm for w in ROLE_LIGHT_WORDS):
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
        return _reply("switch_on") if on else _reply("switch_off")

    async def _light_brightness(self, device: ha_morph.Device, pct: int) -> str:
        pct = max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, pct))
        await self.client.call("light", "turn_on", {
            "entity_id": device.entity_id, "brightness_pct": pct,
        })
        return _fmt("brightness", pct=pct, device=device.name)

    async def _set_temperature(self, device: ha_morph.Device, value: int) -> str | None:
        ok = await self.client.call(device.role, "set_temperature", {
            "entity_id": device.entity_id, "temperature": value,
        })
        if not ok:
            return None
        unit = plural(value, ("градус", "градуса", "градусов"))
        return _fmt("temperature", value=value, unit=unit)

    async def _vacuum(self, norm: str, device: ha_morph.Device) -> str | None:
        if any(w in norm for w in ("домой", "баз", "верн")):
            ok = await self.client.call("vacuum", "return_to_base", {"entity_id": device.entity_id})
            return _reply("vacuum_home") if ok else None
        if any(w in norm for w in OFF_WORDS):
            ok = await self.client.call("vacuum", "stop", {"entity_id": device.entity_id})
            return _reply("vacuum_stop") if ok else None
        state = await self.client.state(device.entity_id)
        if state is not None and state.get("state") in ("unavailable", "unknown"):
            return _reply("vacuum_unavailable")
        ok = await self.client.call("vacuum", "start", {"entity_id": device.entity_id})
        return _reply("vacuum_start") if ok else None

    async def _area_light(self, norm: str, area: str) -> str | None:
        on = not any(w in norm for w in OFF_WORDS)
        ok = await self.client.call("light", "turn_on" if on else "turn_off", {"area_id": area})
        if not ok:
            return None
        return _reply("light_on") if on else _reply("light_off")
