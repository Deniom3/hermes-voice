#!/usr/bin/env python3
"""MQTT-мостик хаба: сущности Home Assistant на каждую колонку.

Зачем: автоматизациям нужны настоящие сущности HA, а не REST-вызовы.
Мост публикует discovery-конфиги (retained), состояние музыки и принимает
команды. Всё локально, через тот же брокер, что использует HA.

Колонок может быть несколько: каждая станция получает свой набор сущностей и
свой префикс топиков. Имя берётся из `hello` агента (`lite-1`), в сущностях
точки и дефисы заменяются на подчёркивания.

Топики (префикс `MQTT_PREFIX`, по умолчанию hermes/stations):
  <prefix>/<station>/availability   online/offline (по подключению)
  <prefix>/<station>/music          JSON {"state": "playing|paused", "track": "..."} retained
  <prefix>/<station>/say            текст для озвучки (или JSON {"text": ..., "priority": "alarm"})
  <prefix>/<station>/phrase         JSON {"text": "..."} — распознанная фраза без обращения,
                                    без retain: это событие, а не состояние
  <prefix>/<station>/music/set      play / pause (switch.hermes_<station>_music)

Сущности на каждую станцию:
  notify.hermes_<station>         — озвучить текст
  switch.hermes_<station>_music   — музыка: состояние и пауза/возврат в одном
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as mqtt

try:                      # запуск как модуль
    from . import voice_dialog
    from . import ma_control
except ImportError:       # запуск как скрипт
    import voice_dialog
    import ma_control

log = logging.getLogger("mqtt-bridge")

try:                      # запуск как модуль
    from . import config
except ImportError:       # запуск как скрипт
    import config

# Секреты остаются в окружении: в JSON их не пишем.
MQTT_HOST = config.get("mqtt", "host", default=os.environ.get("MQTT_HOST", ""))
MQTT_PORT = config.as_int("mqtt", "port", default=1883)
MQTT_USER = config.get("mqtt", "user", default=os.environ.get("MQTT_USER", ""))
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_PREFIX = config.get("mqtt", "prefix", default="hermes/stations")
DISCOVERY_PREFIX = config.get("mqtt", "discovery", default="homeassistant")

STATE_POLL_S = config.as_float("mqtt", "state_poll_s", default=5.0)
SAY_TTL_S = config.as_float("mqtt", "say_ttl_s", default=120.0)
SAY_DEDUP_S = config.as_float("mqtt", "say_dedup_s", default=60.0)
DIALOG_WAIT_S = config.as_float("mqtt", "dialog_wait_s", default=60.0)
RESUME_DELAY = config.as_float("mqtt", "resume_delay", default=0.5)
CONN_WAIT_S = config.as_float("mqtt", "conn_wait_s", default=20.0)

ORIGIN = {"name": "hermes-voice", "sw_version": "1.0",
          "support_url": "https://github.com/OWNER/hermes-voice"}

ID_RE = re.compile(r"[^a-z0-9_]+")


def entity_slug(name: str) -> str:
    """`lite-1` → `lite_1`: entity_id должен быть латиницей и без дефисов."""
    slug = ID_RE.sub("_", name.strip().lower()).strip("_")
    return slug or "station"


def _connected(client) -> bool:
    """Есть ли живое соединение с брокером.

    У paho это `is_connected()`; у подмены в тестах метода может не быть —
    тогда считаем соединение живым.
    """
    if client is None:
        return False
    check = getattr(client, "is_connected", None)
    if not callable(check):
        return True
    try:
        return bool(check())
    except Exception:  # noqa: BLE001
        return False


@dataclass
class Station:
    """То, что мост знает о колонке: имя, связь с агентом и последний статус."""

    name: str
    conn: Any
    online: bool = False
    last_state: tuple[str, str] | None = None
    volume: int | None = None
    volume_db: float | None = None
    # Последний опубликованный режим музыки: local / cloud / none.
    mode: str | None = None
    # Публикации, которые не удалось отправить (брокер ещё не подключён или
    # publish вернул ошибку). Добираются в следующем круге опроса: иначе
    # retained offline остаётся в брокере, и HA считает колонку недоступной,
    # хотя она подключена.
    avail_pending: bool = False
    discovery_sent: bool = False

    @property
    def slug(self) -> str:
        return entity_slug(self.name)

    @property
    def base(self) -> str:
        return f"{MQTT_PREFIX}/{self.name}"


class MqttBridge:
    """Мост к брокеру. Живёт рядом с хабом, но не внутри речевого цикла."""

    def __init__(self, get_stations):
        self._get_stations = get_stations
        self._client: mqtt.Client | None = None
        self._speech: asyncio.Queue | None = None
        self._tasks: list[asyncio.Task] = []
        self._seen: dict[str, float] = {}
        self._stations: dict[str, Station] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---------- жизненный цикл ----------

    async def start(self) -> None:
        if not MQTT_HOST:
            log.info("MQTT не настроен (MQTT_HOST пуст) — мост выключен")
            return
        self._loop = asyncio.get_running_loop()
        self._speech = asyncio.Queue(maxsize=20)

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hermes-voice-hub")
        if MQTT_USER:
            c.username_pw_set(MQTT_USER, MQTT_PASSWORD)
        c.will_set(f"{MQTT_PREFIX}/hub/availability", "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        try:
            c.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
            c.loop_start()
        except Exception as e:  # noqa: BLE001
            log.warning("MQTT: подключение не удалось: %s", e)
            return
        self._client = c
        self._tasks.append(asyncio.create_task(self._state_loop()))
        self._tasks.append(asyncio.create_task(self._speech_worker()))
        log.info("MQTT: мост запущен (%s:%s, префикс %s)", MQTT_HOST, MQTT_PORT, MQTT_PREFIX)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        if self._client:
            try:
                for st in self._stations.values():
                    self._client.publish(f"{st.base}/availability", "offline", retain=True)
                self._client.publish(f"{MQTT_PREFIX}/hub/availability", "offline", retain=True)
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # ---------- paho callbacks (поток paho) ----------

    def _on_connect(self, client, _userdata, _flags, rc, _properties=None) -> None:
        if str(rc) not in ("Success", "0"):
            log.warning("MQTT: connect rc=%s", rc)
            return
        client.publish(f"{MQTT_PREFIX}/hub/availability", "online", retain=True)
        client.subscribe(f"{MQTT_PREFIX}/+/say", qos=1)
        client.subscribe(f"{MQTT_PREFIX}/+/music/set", qos=1)
        client.subscribe(f"{MQTT_PREFIX}/+/volume/set", qos=1)
        self._resubscribe_known(client)
        log.info("MQTT: подключён")

    def _resubscribe_known(self, client) -> None:
        """Discovery уже известных станций — после реконнекта брокера."""
        for st in self._stations.values():
            self._publish_discovery(client, st)
            st.discovery_sent = True
            self._publish_avail(client, st, st.online)
            st.avail_pending = False

    def _ensure_discovery(self, client, st: Station) -> None:
        """Discovery один раз на станцию; при недоступном брокере — позже."""
        if st.discovery_sent or not _connected(client):
            return
        self._publish_discovery(client, st)
        st.discovery_sent = True

    def _publish_avail(self, client, st: Station, online: bool) -> None:
        """Публикует availability и помнит, если отправить не удалось.

        Публикация до CONNACK у paho молча отбрасывается — именно так после
        рестарта хаба оставался retained offline при живой колонке. Поэтому
        неудачу запоминаем в `avail_pending`, а `_sync_stations` повторяет её
        на следующем круге опроса.
        """
        changed = st.online != online
        st.online = online
        if not (changed or st.avail_pending):
            return
        if not _connected(client):
            st.avail_pending = True
            return
        rc = client.publish(f"{st.base}/availability",
                            "online" if online else "offline", retain=True)
        code = getattr(rc, "rc", 0)
        st.avail_pending = code != 0
        if st.avail_pending:
            log.warning("MQTT: %s availability не отправлена (rc=%s) — повторю",
                        st.name, code)

    def _on_message(self, client, _userdata, msg) -> None:
        # Вызывается из потока paho: только перекладываем в цикл asyncio.
        payload = msg.payload.decode("utf-8", "replace").strip()
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._handle(msg.topic, payload), self._loop)

    # ---------- discovery ----------

    def _publish_discovery(self, c, st: Station) -> None:
        base = st.base
        common = {"availability_topic": f"{base}/availability",
                  "payload_available": "online", "payload_not_available": "offline",
                  "device": {"identifiers": [f"hermes_station_{st.slug}"],
                             "name": f"Гермес ({st.name})",
                             "manufacturer": "Hermes", "model": "voice-hub"},
                  "origin": ORIGIN}

        c.publish(f"{DISCOVERY_PREFIX}/notify/hermes_{st.slug}/config", json.dumps({
            "name": "Гермес — озвучить", "unique_id": f"hermes_{st.slug}_say",
            "default_entity_id": f"notify.hermes_{st.slug}",
            "command_topic": f"{base}/say", **common,
        }, ensure_ascii=False), retain=True)

        # Один переключатель вместо пары кнопок: состояние и команда в одном
        # месте, условия автоматизаций читают то же, чем управляют.
        c.publish(f"{DISCOVERY_PREFIX}/switch/hermes_{st.slug}_music/config", json.dumps({
            "name": "Музыка на станции", "unique_id": f"hermes_{st.slug}_music",
            "default_entity_id": f"switch.hermes_{st.slug}_music",
            "state_topic": f"{base}/music",
            "value_template": "{{ value_json.state }}",
            "state_on": "playing", "state_off": "paused",
            "command_topic": f"{base}/music/set",
            "payload_on": "play", "payload_off": "pause",
            "json_attributes_topic": f"{base}/music",
            "icon": "mdi:music", **common,
        }, ensure_ascii=False), retain=True)

        # Режим музыки: local — своя библиотека (MA), cloud — Яндекс Музыка
        # через Алису. Сенсор нужен автоматизации «какой режим работы» и
        # дашборду; значение — производное от фактического владельца.
        c.publish(f"{DISCOVERY_PREFIX}/sensor/hermes_{st.slug}_mode/config", json.dumps({
            "name": "Режим музыки", "unique_id": f"hermes_{st.slug}_mode",
            "default_entity_id": f"sensor.hermes_{st.slug}_mode",
            "state_topic": f"{base}/mode",
            "value_template": "{{ value_json.mode }}",
            "device_class": "enum",
            "options": ["local", "cloud", "none"],
            "icon": "mdi:music-box-multiple", **common,
        }, ensure_ascii=False), retain=True)

        # Громкость: ALSA-микшер колонки, читается агентом без облака.
        # Сенсор — наблюдение, number — управление; оба смотрят один retained-топик.
        c.publish(f"{DISCOVERY_PREFIX}/sensor/hermes_{st.slug}_volume/config", json.dumps({
            "name": "Громкость станции", "unique_id": f"hermes_{st.slug}_volume",
            "default_entity_id": f"sensor.hermes_{st.slug}_volume",
            "state_topic": f"{base}/volume",
            "value_template": "{{ value_json.level }}",
            "state_class": "measurement",
            "icon": "mdi:volume-high", **common,
        }, ensure_ascii=False), retain=True)

        c.publish(f"{DISCOVERY_PREFIX}/number/hermes_{st.slug}_volume/config", json.dumps({
            "name": "Громкость станции — управление",
            "unique_id": f"hermes_{st.slug}_volume_set",
            "default_entity_id": f"number.hermes_{st.slug}_volume",
            "command_topic": f"{base}/volume/set",
            "state_topic": f"{base}/volume",
            "value_template": "{{ value_json.level }}",
            # Шкала станции: 10 ступеней и мьют. 0 — звук выключен.
            "min": 0, "max": 10, "step": 1,
            "icon": "mdi:volume-high", **common,
        }, ensure_ascii=False), retain=True)

    # ---------- станции и их состояние ----------

    def _sync_stations(self) -> list[Station]:
        """Приводит список станций к тому, что реально подключено к хабу."""
        live: dict[str, object] = {}
        for conn in self._get_stations():
            name = getattr(conn, "name", "") or ""
            if name:
                live[name] = conn
        for name, conn in live.items():
            st = self._stations.get(name)
            if st is None:
                st = Station(name=name, conn=conn)
                self._stations[name] = st
                log.info("MQTT: станция %s появилась", name)
            elif not st.online:
                # Переподключение после обрыва: discovery уже отправлен, но
                # availability осталась offline (retain) — без этой публикации
                # HA видит станцию недоступной до перезапуска хаба.
                log.info("MQTT: станция %s снова на связи", name)
            st.conn = conn
            self._ensure_discovery(self._client, st)
            self._publish_avail(self._client, st, True)
        for name, st in list(self._stations.items()):
            if name not in live and st.online:
                self._publish_avail(self._client, st, False)
                log.info("MQTT: станция %s отключилась", name)
        return list(self._stations.values())

    async def _poll_station(self, st: Station) -> None:
        """Один опрос колонки: музыка и громкость, публикуем только изменения."""
        res = await st.conn.command({"cmd": "state"}, timeout=5)
        playing = bool(res.get("playing"))
        track = str(res.get("track") or "")
        cur = ("playing" if playing else "paused", track)
        if cur != st.last_state:
            st.last_state = cur
            self._publish(f"{st.base}/music",
                          json.dumps({"state": cur[0], "track": track},
                                     ensure_ascii=False), retain=True)
            log.info("MQTT: %s музыка %s", st.name, cur[0])

        lvl = res.get("level")
        if lvl is None and res.get("volume") is not None:
            lvl = max(0, min(10, round(int(res["volume"]) / 10)))
        if lvl is not None and int(lvl) != st.volume:
            st.volume = int(lvl)
            st.volume_db = res.get("volume_db")
            self._publish(f"{st.base}/volume",
                          json.dumps({"level": st.volume,
                                      "percent": res.get("volume"),
                                      "db": st.volume_db}),
                          retain=True)
            log.info("MQTT: %s громкость %d%%", st.name, st.volume)

        # Режим — производное от фактического владельца в хабе. Публикуем
        # retained, чтобы HA видел его сразу после старта, а не после нажатия.
        mode = getattr(st.conn, "mode", "none") or "none"
        if mode != st.mode:
            self.publish_mode(st.name, mode)

    async def _state_loop(self) -> None:
        while True:
            try:
                for st in self._sync_stations():
                    if not st.online:
                        continue
                    await self._poll_station(st)
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                log.debug("MQTT: опрос состояния: %s", e)
            await asyncio.sleep(STATE_POLL_S)

    def _publish(self, topic: str, payload: str, retain: bool = False) -> None:
        if self._client:
            self._client.publish(topic, payload, qos=1, retain=retain)

    def publish_mode(self, station: str, mode: str) -> bool:
        """Опубликовать режим музыки станции (retained).

        Retained, потому что это состояние, а не событие: HA и дашборд
        должны видеть режим сразу после перезапуска, не дожидаясь нажатия.
        """
        if mode not in ("local", "cloud", "none"):
            return False
        st = self._stations.get(station)
        if st is None:
            for candidate in self._sync_stations():
                if candidate.name == station:
                    st = candidate
                    break
        if st is None:
            log.debug("MQTT: режим для неизвестной станции %s — пропущен", station)
            return False
        st.mode = mode
        self._publish(f"{st.base}/mode", json.dumps({"mode": mode}), retain=True)
        return True

    def publish_phrase(self, station: str, text: str) -> bool:
        """Опубликовать распознанную фразу колонки — уже без обращения.

        Топик `{base}/phrase`, без retain: фраза — событие, а не состояние.
        Повтор той же фразы подряд должен срабатывать снова, поэтому метку
        времени и уникальный id не добавляем: их всё равно не сравнить, а
        MQTT-триггер в HA срабатывает на каждое сообщение.

        Возвращает True, если сообщение отдано брокеру. Станция, которой ещё
        нет в словаре (первая фраза сразу после подключения), сначала
        синхронизируется — иначе событие потерялось бы без следа.
        """
        text = (text or "").strip()
        if not text:
            return False
        st = self._stations.get(station)
        if st is None:
            for candidate in self._sync_stations():
                if candidate.name == station:
                    st = candidate
                    break
        if st is None:
            log.debug("MQTT: фраза для неизвестной станции %s — пропущена", station)
            return False
        self._publish(f"{st.base}/phrase",
                      json.dumps({"text": text}, ensure_ascii=False))
        return True

    # ---------- команды и очередь речи ----------

    def _station_from_topic(self, topic: str) -> tuple[Station | None, str]:
        """`hermes/stations/lite-1/say` → (станция, 'say')."""
        rest = topic[len(MQTT_PREFIX) + 1:]
        parts = rest.split("/")
        if len(parts) < 2:
            return None, ""
        name = parts[0]
        cmd = "/".join(parts[1:])
        st = self._stations.get(name)
        if st is None:
            # Команда пришла раньше discovery: ищем по подключённым.
            for candidate in self._sync_stations():
                if candidate.name == name:
                    st = candidate
                    break
        return st, cmd

    async def _handle(self, topic: str, payload: str) -> None:
        st, cmd = self._station_from_topic(topic)
        if cmd == "music/set":
            # switch.hermes_<station>_music: ON → вернуть музыку, OFF → пауза.
            action = payload.strip().lower()
            if action in ("play", "pause"):
                if action == "pause":
                    await self._command(st, {"cmd": "pause"})
                    # Агент ставит на паузу только локальный aplay/Sendspin;
                    # очередь MA живёт отдельно. Без этого switch в HA
                    # останавливал звук, а MA продолжал «играть» и мог вернуть
                    # поток сам.
                    if st is not None:
                        try:
                            await ma_control.pause_if_playing(st.name)
                        except Exception as e:  # noqa: BLE001
                            log.warning("MQTT: MA-пауза не прошла: %s", e)
                else:
                    if st is not None:
                        try:
                            await ma_control.resume_if_paused(st.name)
                        except Exception as e:  # noqa: BLE001
                            log.warning("MQTT: MA-resume не прошёл: %s", e)
                    await self._command(st, {"cmd": "play"})
        elif cmd == "volume/set":
            # number.hermes_<station>_volume: уровень 0..10 (0 — мьют).
            try:
                level = int(float(payload.strip()))
            except ValueError:
                log.warning("MQTT: громкость %r — не число", payload[:20])
                return
            level = max(0, min(10, level))
            await self._command(st, {"cmd": "volume_set", "level": level})
            log.info("MQTT: %s громкость → %d из 10", st.name if st else "?", level)
        elif cmd == "say":
            text, priority = self._parse_say(payload)
            if not text:
                return
            now = time.monotonic()
            self._seen = {t: ts for t, ts in self._seen.items() if now - ts < SAY_DEDUP_S}
            if text in self._seen:
                log.info("MQTT: дубль фразы пропущен: %r", text[:40])
                return
            self._seen[text] = now
            try:
                self._speech.put_nowait((priority, now, text, st.name if st else None))
            except asyncio.QueueFull:
                log.warning("MQTT: очередь речи переполнена, фраза отброшена")

    @staticmethod
    def _parse_say(payload: str) -> tuple[str, str]:
        if payload.startswith("{"):
            try:
                obj = json.loads(payload)
                return str(obj.get("text") or "").strip(), str(obj.get("priority") or "normal")
            except json.JSONDecodeError:
                pass
        return payload.strip(), "normal"

    async def _command(self, st: Station | None, payload: dict) -> None:
        if st is None or not st.online:
            log.warning("MQTT: команда %s — нет такой станции", payload)
            return
        try:
            await st.conn.command(payload, timeout=15)
        except Exception as e:  # noqa: BLE001
            log.warning("MQTT: команда %s не прошла: %s", payload, e)

    async def _speech_worker(self) -> None:
        while True:
            priority, queued_at, text, target = await self._speech.get()
            try:
                if priority != "alarm" and time.monotonic() - queued_at > SAY_TTL_S:
                    log.info("MQTT: фраза устарела, пропуск: %r", text[:40])
                    continue
                if priority != "alarm":
                    await self._wait_dialog_idle(target)
                st = await self._wait_station(target, CONN_WAIT_S)
                if st is None:
                    log.warning("MQTT: озвучка без колонки: %r", text[:40])
                    continue
                # Правило то же, что при обычном ответе: музыка на паузу,
                # после фразы — обратно, и только если она играла.
                was_playing = False
                ma_paused = False
                try:
                    res = await st.conn.command({"cmd": "pause"}, timeout=15) or {}
                    was_playing = bool(res.get("was_playing"))
                except Exception as e:  # noqa: BLE001
                    log.debug("MQTT: пауза перед озвучкой не прошла: %s", e)
                try:
                    ma_paused = await ma_control.pause_if_playing(st.name)
                except Exception as e:  # noqa: BLE001
                    log.debug("MQTT: MA-пауза перед озвучкой не прошла: %s", e)
                try:
                    audio = await asyncio.to_thread(voice_dialog.synthesize, text)
                    await st.conn.command({"cmd": "say",
                                           "audio": base64.b64encode(audio).decode(),
                                           "volume": voice_dialog.SAY_VOLUME}, timeout=120)
                    log.info("MQTT: %s озвучено %r", st.name, text[:40])
                finally:
                    if ma_paused:
                        try:
                            await ma_control.resume_if_paused(st.name)
                        except Exception as e:  # noqa: BLE001
                            log.warning("MQTT: MA не вернул музыку: %s", e)
                    if was_playing:
                        await asyncio.sleep(RESUME_DELAY)
                        try:
                            await st.conn.command({"cmd": "play"}, timeout=15)
                            log.info("MQTT: %s музыка возвращена", st.name)
                        except Exception as e:  # noqa: BLE001
                            log.warning("MQTT: вернуть музыку не удалось: %s", e)
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                log.warning("MQTT: озвучка не удалась: %s", e)
            finally:
                self._speech.task_done()

    async def _wait_station(self, name: str | None, timeout_s: float) -> Station | None:
        """Ждёт нужную станцию (или любую): после рестарта хаба она приходит не сразу."""
        deadline = time.monotonic() + timeout_s
        while True:
            stations = [s for s in self._sync_stations() if s.online]
            if name:
                for st in stations:
                    if st.name == name:
                        return st
            elif stations:
                return stations[0]
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.5)

    async def _wait_dialog_idle(self, target: str | None = None) -> None:
        """Не вклиниваться в диалог: ждём idle не дольше DIALOG_WAIT_S."""
        deadline = time.monotonic() + DIALOG_WAIT_S
        while time.monotonic() < deadline:
            stations = [s for s in self._sync_stations() if s.online]
            if target:
                stations = [s for s in stations if s.name == target]
            states = [getattr(getattr(s.conn, "core", None), "state", None) for s in stations]
            if not states or all(
                    st is None or getattr(st, "value", str(st)) == "idle" for st in states):
                return
            await asyncio.sleep(0.5)
