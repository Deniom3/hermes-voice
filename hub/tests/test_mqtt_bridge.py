"""MQTT-мост: discovery на каждую колонку, очередь речи и правило паузы музыки.

Главные дефекты, которые эти тесты закрепляют:
1) озвучка из HA не ставила музыку на паузу и не возвращала её;
2) колонка не одна — сущности и топики должны быть отдельными на станцию.
"""
import asyncio
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mqtt_bridge  # noqa: E402


class _FakeMA:
    """Тесты моста не должны поднимать живой MCP Music Assistant."""

    async def pause_if_playing(self, _station):
        return False

    async def resume_if_paused(self, _station):
        return False


_OLD_MA = mqtt_bridge.ma_control


def setUpModule():
    mqtt_bridge.ma_control = _FakeMA()


def tearDownModule():
    mqtt_bridge.ma_control = _OLD_MA


class FakeClient:
    """Подмена paho: запоминает publish/subscribe без сети."""

    def __init__(self):
        self.published = []
        self.subscribed = []

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)


class FakeConn:
    """Колонка: отвечает на команды и пишет их порядок."""

    def __init__(self, name="lite-1", was_playing=False, state="idle"):
        self.name = name
        self.role = "station"
        self.calls = []
        self.was_playing = was_playing
        self.playing = False
        self.track = ""
        self.core = SimpleNamespace(state=SimpleNamespace(value=state))

    volume = 5        # уровень 0..10, единый контракт
    volume_db = -25.4

    async def command(self, payload, timeout=10):
        self.calls.append(payload.get("cmd"))
        if payload.get("cmd") == "pause":
            return {"ok": True, "was_playing": self.was_playing}
        if payload.get("cmd") == "state":
            return {"ok": True, "playing": self.playing, "track": self.track,
                    "volume": self.volume * 10, "volume_db": self.volume_db,
                    "level": self.volume}
        if payload.get("cmd") == "volume_set":
            self.volume = int(payload.get("level", 0))
            return {"ok": True, "volume": self.volume * 10, "volume_db": self.volume_db,
                    "level": self.volume}
        return {"ok": True}


def make_bridge(*conns):
    bridge = mqtt_bridge.MqttBridge(lambda: list(conns))
    bridge._client = FakeClient()
    bridge._speech = asyncio.Queue(maxsize=20)
    return bridge


async def run_worker(bridge, item, delay=0.0):
    """Один проход воркера: кладём фразу, ждём обработки, гасим цикл."""
    with patch.object(mqtt_bridge, "RESUME_DELAY", delay):
        task = asyncio.create_task(bridge._speech_worker())
        try:
            bridge._speech.put_nowait(item)
            await asyncio.wait_for(bridge._speech.join(), timeout=5)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def phrase(text, priority="normal", target="lite-1"):
    return (priority, mqtt_bridge.time.monotonic(), text, target)


class SlugTest(unittest.TestCase):
    def test_dash_and_dot_become_underscore(self):
        self.assertEqual(mqtt_bridge.entity_slug("lite-1"), "lite_1")
        self.assertEqual(mqtt_bridge.entity_slug("Kitchen 2.0"), "kitchen_2_0")

    def test_empty_falls_back_to_station(self):
        self.assertEqual(mqtt_bridge.entity_slug("  "), "station")


class ParseSayTest(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(mqtt_bridge.MqttBridge._parse_say("Привет"), ("Привет", "normal"))

    def test_json_with_priority(self):
        payload = json.dumps({"text": "Тревога", "priority": "alarm"}, ensure_ascii=False)
        self.assertEqual(mqtt_bridge.MqttBridge._parse_say(payload), ("Тревога", "alarm"))

    def test_json_without_priority_is_normal(self):
        payload = json.dumps({"text": "Текст"}, ensure_ascii=False)
        self.assertEqual(mqtt_bridge.MqttBridge._parse_say(payload), ("Текст", "normal"))

    def test_broken_json_treated_as_text(self):
        payload = '{"text": "обрыв'
        self.assertEqual(mqtt_bridge.MqttBridge._parse_say(payload), (payload, "normal"))

    def test_spaces_are_stripped(self):
        self.assertEqual(mqtt_bridge.MqttBridge._parse_say("  Привет  "), ("Привет", "normal"))


class SpeechQueueTest(unittest.IsolatedAsyncioTestCase):
    async def test_say_puts_phrase_into_queue(self):
        bridge = make_bridge(FakeConn())
        await bridge._handle("hermes/stations/lite-1/say", "Привет")
        self.assertEqual(bridge._speech.qsize(), 1)

    async def test_phrase_keeps_station_name(self):
        bridge = make_bridge(FakeConn())
        await bridge._handle("hermes/stations/lite-1/say", "Привет")
        self.assertEqual(bridge._speech.get_nowait()[3], "lite-1")

    async def test_duplicate_is_dropped(self):
        bridge = make_bridge(FakeConn())
        await bridge._handle("hermes/stations/lite-1/say", "Привет")
        await bridge._handle("hermes/stations/lite-1/say", "Привет")
        self.assertEqual(bridge._speech.qsize(), 1)

    async def test_different_text_is_not_a_duplicate(self):
        bridge = make_bridge(FakeConn())
        await bridge._handle("hermes/stations/lite-1/say", "Привет")
        await bridge._handle("hermes/stations/lite-1/say", "Пока")
        self.assertEqual(bridge._speech.qsize(), 2)

    async def test_empty_text_is_ignored(self):
        bridge = make_bridge(FakeConn())
        await bridge._handle("hermes/stations/lite-1/say", "   ")
        self.assertEqual(bridge._speech.qsize(), 0)


class MusicPauseRuleTest(unittest.IsolatedAsyncioTestCase):
    """Главное правило: музыка возвращается только если играла."""

    async def asyncSetUp(self):
        self._pause = AsyncMock(return_value=False)
        self._resume = AsyncMock(return_value=False)
        self._p_pause = patch.object(mqtt_bridge.ma_control, "pause_if_playing", self._pause)
        self._p_resume = patch.object(mqtt_bridge.ma_control, "resume_if_paused", self._resume)
        self._p_pause.start()
        self._p_resume.start()

    async def asyncTearDown(self):
        self._p_pause.stop()
        self._p_resume.stop()

    async def test_playing_music_is_paused_and_resumed(self):
        conn = FakeConn(was_playing=True)
        bridge = make_bridge(conn)
        with patch.object(mqtt_bridge.voice_dialog, "synthesize", return_value=b"mp3"):
            await run_worker(bridge, phrase("Проверка"))
        self.assertEqual(conn.calls, ["pause", "say", "play"])

    async def test_no_music_is_not_started(self):
        conn = FakeConn(was_playing=False)
        bridge = make_bridge(conn)
        with patch.object(mqtt_bridge.voice_dialog, "synthesize", return_value=b"mp3"):
            await run_worker(bridge, phrase("Проверка"))
        self.assertEqual(conn.calls, ["pause", "say"])

    async def test_music_resumes_even_if_say_fails(self):
        conn = FakeConn(was_playing=True)
        bridge = make_bridge(conn)

        def boom(_):
            raise RuntimeError("TTS лёг")

        with patch.object(mqtt_bridge.voice_dialog, "synthesize", side_effect=boom):
            await run_worker(bridge, phrase("Проверка"))
        self.assertEqual(conn.calls, ["pause", "play"])

    async def test_alarm_does_not_wait_for_dialog(self):
        """Тревога играет сразу: ожидание idle не должно её тормозить."""
        conn = FakeConn(was_playing=True, state="listening")
        bridge = make_bridge(conn)
        with patch.object(mqtt_bridge.voice_dialog, "synthesize", return_value=b"mp3"), \
                patch.object(mqtt_bridge.MqttBridge, "_wait_dialog_idle") as wait:
            await run_worker(bridge, phrase("Тревога", priority="alarm"))
        wait.assert_not_called()
        self.assertEqual(conn.calls, ["pause", "say", "play"])


class MultiStationTest(unittest.IsolatedAsyncioTestCase):
    """Колонок может быть несколько: команда идёт только в свою."""

    async def test_phrase_goes_to_named_station(self):
        kitchen = FakeConn(name="kitchen")
        lite = FakeConn(name="lite-1")
        bridge = make_bridge(kitchen, lite)
        with patch.object(mqtt_bridge.voice_dialog, "synthesize", return_value=b"mp3"):
            await run_worker(bridge, phrase("Только на кухню", target="kitchen"))
        self.assertEqual(kitchen.calls, ["pause", "say"])
        self.assertEqual(lite.calls, [])

    async def test_switch_command_goes_to_its_station(self):
        kitchen = FakeConn(name="kitchen")
        lite = FakeConn(name="lite-1")
        bridge = make_bridge(kitchen, lite)
        bridge._sync_stations()
        with patch.object(mqtt_bridge.ma_control, "pause_if_playing", AsyncMock(return_value=False)):
            await bridge._handle("hermes/stations/kitchen/music/set", "pause")
        self.assertEqual(kitchen.calls, ["pause"])
        self.assertEqual(lite.calls, [])

    async def test_each_station_gets_its_own_entities(self):
        bridge = make_bridge(FakeConn(name="kitchen"), FakeConn(name="lite-1"))
        bridge._sync_stations()
        topics = {t for t, _, _ in bridge._client.published}
        self.assertIn("homeassistant/notify/hermes_kitchen/config", topics)
        self.assertIn("homeassistant/notify/hermes_lite_1/config", topics)
        self.assertIn("homeassistant/switch/hermes_kitchen_music/config", topics)
        self.assertIn("homeassistant/switch/hermes_lite_1_music/config", topics)

    async def test_station_goes_offline_when_disconnected(self):
        conn = FakeConn(name="lite-1")
        bridge = make_bridge(conn)
        bridge._sync_stations()
        bridge._get_stations = lambda: []
        bridge._sync_stations()
        self.assertIn(("hermes/stations/lite-1/availability", "offline", True),
                      bridge._client.published)
        self.assertFalse(bridge._stations["lite-1"].online)

    async def test_station_goes_back_online_after_reconnect(self):
        """После обрыва и нового подключения availability обязана стать online.

        Дефект 4–5 ноября: offline (retain) публиковался, а обратный online —
        только для новой станции. HA показывал колонку недоступной 17 часов,
        хотя хаб её уже видел.
        """
        conn = FakeConn(name="lite-1")
        bridge = make_bridge(conn)
        bridge._sync_stations()
        bridge._get_stations = lambda: []
        bridge._sync_stations()
        bridge._get_stations = lambda: [conn]
        bridge._sync_stations()
        client = bridge._client
        self.assertIsNotNone(client)
        assert client is not None
        self.assertIn(("hermes/stations/lite-1/availability", "online", True),
                      client.published)
        self.assertTrue(bridge._stations["lite-1"].online)

    async def test_availability_retried_when_broker_not_ready(self):
        """Публикация до CONNACK не теряется.

        Дефект: после рестарта хаба станция появлялась раньше, чем paho
        получал подтверждение подключения; publish молча отбрасывался, а
        st.online уже стоял True — retained offline висел часами, и HA
        показывал живую колонку недоступной.
        """
        bridge = make_bridge(FakeConn(name="lite-1"))
        client = bridge._client
        assert client is not None
        client.is_connected = lambda: False
        bridge._sync_stations()
        self.assertNotIn(("hermes/stations/lite-1/availability", "online", True),
                         client.published)
        self.assertTrue(bridge._stations["lite-1"].avail_pending)

        client.is_connected = lambda: True
        bridge._sync_stations()
        self.assertIn(("hermes/stations/lite-1/availability", "online", True),
                      client.published)
        self.assertFalse(bridge._stations["lite-1"].avail_pending)

    async def test_discovery_retried_when_broker_not_ready(self):
        bridge = make_bridge(FakeConn(name="lite-1"))
        client = bridge._client
        assert client is not None
        client.is_connected = lambda: False
        bridge._sync_stations()
        self.assertNotIn("homeassistant/notify/hermes_lite_1/config",
                         {t for t, _, _ in client.published})

        client.is_connected = lambda: True
        bridge._sync_stations()
        self.assertIn("homeassistant/notify/hermes_lite_1/config",
                      {t for t, _, _ in client.published})

    async def test_availability_is_not_republished_every_poll(self):
        bridge = make_bridge(FakeConn(name="lite-1"))
        client = bridge._client
        assert client is not None
        bridge._sync_stations()
        first = [p for p in client.published
                 if p[0].endswith("/availability")]
        bridge._sync_stations()
        second = [p for p in client.published
                  if p[0].endswith("/availability")]
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)


class ExpiredPhraseTest(unittest.IsolatedAsyncioTestCase):
    async def test_expired_normal_phrase_is_skipped(self):
        conn = FakeConn()
        bridge = make_bridge(conn)
        stale = mqtt_bridge.time.monotonic() - mqtt_bridge.SAY_TTL_S - 1
        await run_worker(bridge, ("normal", stale, "Старое", "lite-1"))
        self.assertEqual(conn.calls, [])

    async def test_expired_alarm_still_plays(self):
        conn = FakeConn()
        bridge = make_bridge(conn)
        stale = mqtt_bridge.time.monotonic() - mqtt_bridge.SAY_TTL_S - 1
        with patch.object(mqtt_bridge.voice_dialog, "synthesize", return_value=b"mp3"):
            await run_worker(bridge, ("alarm", stale, "Тревога", "lite-1"))
        self.assertEqual(conn.calls, ["pause", "say"])


class WaitStationTest(unittest.IsolatedAsyncioTestCase):
    async def test_waits_until_station_connects(self):
        """После рестарта хаба колонка приходит не сразу — фразу не теряем."""
        conn = FakeConn()
        calls = {"n": 0}

        def get_stations():
            calls["n"] += 1
            return [conn] if calls["n"] >= 3 else []

        bridge = mqtt_bridge.MqttBridge(get_stations)
        bridge._client = FakeClient()
        with patch.object(mqtt_bridge.asyncio, "sleep", return_value=None):
            got = await bridge._wait_station("lite-1", 1.0)
        self.assertIsNotNone(got)
        self.assertEqual(got.name, "lite-1")
        self.assertIs(got.conn, conn)

    async def test_unknown_station_is_none(self):
        bridge = make_bridge(FakeConn(name="lite-1"))
        self.assertIsNone(await bridge._wait_station("kitchen", 0.0))


class WaitIdleTest(unittest.IsolatedAsyncioTestCase):
    async def test_waits_while_dialog_is_active(self):
        conn = FakeConn()
        conn.core.state = SimpleNamespace(value="listening")
        bridge = make_bridge(conn)

        async def no_sleep(_):
            conn.core.state = SimpleNamespace(value="idle")

        with patch.object(mqtt_bridge.asyncio, "sleep", side_effect=no_sleep):
            await bridge._wait_dialog_idle("lite-1")
        self.assertEqual(conn.core.state.value, "idle")


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.bridge = mqtt_bridge.MqttBridge(lambda: [])
        self.bridge._client = FakeClient()
        self.station = mqtt_bridge.Station(name="lite-1", conn=FakeConn())
        self.bridge._publish_discovery(self.bridge._client, self.station)
        self.payloads = {t: json.loads(p) for t, p, _ in self.bridge._client.published}

    def test_notify_switch_and_volume_are_published(self):
        """Пять сущностей на станцию: озвучка, музыка, режим и две громкости."""
        self.assertEqual(set(self.payloads), {
            "homeassistant/notify/hermes_lite_1/config",
            "homeassistant/switch/hermes_lite_1_music/config",
            "homeassistant/sensor/hermes_lite_1_mode/config",
            "homeassistant/sensor/hermes_lite_1_volume/config",
            "homeassistant/number/hermes_lite_1_volume/config",
        })

    def test_entity_ids_include_station(self):
        """Иначе вторая колонка затрёт сущности первой."""
        self.assertEqual(self.payloads["homeassistant/notify/hermes_lite_1/config"]["default_entity_id"],
                         "notify.hermes_lite_1")
        self.assertEqual(
            self.payloads["homeassistant/switch/hermes_lite_1_music/config"]["default_entity_id"],
            "switch.hermes_lite_1_music")

    def test_payloads_are_retained_and_belong_to_device(self):
        for topic, raw, retain in self.bridge._client.published:
            with self.subTest(topic=topic):
                payload = json.loads(raw)
                self.assertTrue(retain)
                self.assertEqual(payload["availability_topic"],
                                 "hermes/stations/lite-1/availability")
                self.assertEqual(payload["device"]["identifiers"], ["hermes_station_lite_1"])
                self.assertIn("origin", payload)

    def test_switch_reads_state_and_commands_same_topic(self):
        cfg = self.payloads["homeassistant/switch/hermes_lite_1_music/config"]
        self.assertEqual(cfg["state_topic"], "hermes/stations/lite-1/music")
        self.assertEqual(cfg["value_template"], "{{ value_json.state }}")
        self.assertEqual((cfg["state_on"], cfg["state_off"]), ("playing", "paused"))
        self.assertEqual(cfg["command_topic"], "hermes/stations/lite-1/music/set")
        self.assertEqual((cfg["payload_on"], cfg["payload_off"]), ("play", "pause"))
        self.assertEqual(cfg["json_attributes_topic"], "hermes/stations/lite-1/music")

    def test_mode_sensor_is_enum_with_station_topic(self):
        cfg = self.payloads["homeassistant/sensor/hermes_lite_1_mode/config"]
        self.assertEqual(cfg["default_entity_id"], "sensor.hermes_lite_1_mode")
        self.assertEqual(cfg["state_topic"], "hermes/stations/lite-1/mode")
        self.assertEqual(cfg["value_template"], "{{ value_json.mode }}")
        self.assertEqual(cfg["device_class"], "enum")
        self.assertEqual(cfg["options"], ["local", "cloud", "none"])


class ConnectTest(unittest.TestCase):
    def test_connect_subscribes_wildcards_and_marks_online(self):
        bridge = mqtt_bridge.MqttBridge(lambda: [])
        client = FakeClient()
        bridge._on_connect(client, None, None, "Success")
        self.assertEqual(set(client.subscribed), {
            "hermes/stations/+/say", "hermes/stations/+/music/set",
            "hermes/stations/+/volume/set",
        })
        self.assertIn(("hermes/stations/hub/availability", "online", True), client.published)

    def test_failed_connect_does_not_subscribe(self):
        bridge = mqtt_bridge.MqttBridge(lambda: [])
        client = FakeClient()
        bridge._on_connect(client, None, None, "Connection refused")
        self.assertEqual(client.subscribed, [])


class MusicSwitchTest(unittest.IsolatedAsyncioTestCase):
    """Команды switch.hermes_<station>_music доходят до колонки как play/pause."""

    async def asyncSetUp(self):
        self._pause = AsyncMock(return_value=False)
        self._resume = AsyncMock(return_value=False)
        self._p_pause = patch.object(mqtt_bridge.ma_control, "pause_if_playing", self._pause)
        self._p_resume = patch.object(mqtt_bridge.ma_control, "resume_if_paused", self._resume)
        self._p_pause.start()
        self._p_resume.start()

    async def asyncTearDown(self):
        self._p_pause.stop()
        self._p_resume.stop()

    async def test_on_payload_plays(self):
        conn = FakeConn()
        bridge = make_bridge(conn)
        bridge._sync_stations()
        await bridge._handle("hermes/stations/lite-1/music/set", "play")
        self.assertEqual(conn.calls, ["play"])
        self._resume.assert_awaited_once_with("lite-1")

    async def test_off_payload_pauses(self):
        conn = FakeConn()
        bridge = make_bridge(conn)
        bridge._sync_stations()
        await bridge._handle("hermes/stations/lite-1/music/set", "pause")
        self.assertEqual(conn.calls, ["pause"])
        self._pause.assert_awaited_once_with("lite-1")

    async def test_unknown_payload_is_ignored(self):
        conn = FakeConn()
        bridge = make_bridge(conn)
        bridge._sync_stations()
        await bridge._handle("hermes/stations/lite-1/music/set", "ON")
        self.assertEqual(conn.calls, [])


class MusicStatePublishTest(unittest.IsolatedAsyncioTestCase):
    async def test_state_is_published_per_station(self):
        conn = FakeConn()

        async def no_sleep(_):
            raise asyncio.CancelledError

        bridge = make_bridge(conn)
        with patch.object(mqtt_bridge.asyncio, "sleep", side_effect=no_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await bridge._state_loop()
        self.assertIn(("hermes/stations/lite-1/music", '{"state": "paused", "track": ""}', True),
                      bridge._client.published)


class VolumeDiscoveryTest(unittest.TestCase):
    """Громкость станции: ALSA-значение приходит в HA сенсором и числом."""

    def setUp(self):
        bridge = mqtt_bridge.MqttBridge(lambda: [])
        bridge._client = FakeClient()
        st = mqtt_bridge.Station(name="lite-1", conn=FakeConn())
        bridge._publish_discovery(bridge._client, st)
        self.payloads = {t: json.loads(p) for t, p, _ in bridge._client.published}

    def test_sensor_reads_level_from_volume_topic(self):
        cfg = self.payloads["homeassistant/sensor/hermes_lite_1_volume/config"]
        self.assertEqual(cfg["state_topic"], "hermes/stations/lite-1/volume")
        self.assertEqual(cfg["value_template"], "{{ value_json.level }}")
        self.assertEqual(cfg["default_entity_id"], "sensor.hermes_lite_1_volume")

    def test_number_writes_to_volume_set(self):
        cfg = self.payloads["homeassistant/number/hermes_lite_1_volume/config"]
        self.assertEqual(cfg["command_topic"], "hermes/stations/lite-1/volume/set")
        self.assertEqual(cfg["state_topic"], "hermes/stations/lite-1/volume")
        # шкала станции: 10 ступеней плюс мьют
        self.assertEqual((cfg["min"], cfg["max"], cfg["step"]), (0, 10, 1))
        self.assertEqual(cfg["value_template"], "{{ value_json.level }}")
        self.assertEqual(cfg["default_entity_id"], "number.hermes_lite_1_volume")


class VolumeCommandTest(unittest.IsolatedAsyncioTestCase):
    """number.hermes_<station>_volume ставит громкость через агент."""

    async def _send(self, payload):
        conn = FakeConn()
        bridge = make_bridge(conn)
        bridge._sync_stations()
        await bridge._handle("hermes/stations/lite-1/volume/set", payload)
        return conn

    async def test_value_is_forwarded(self):
        conn = await self._send("35")
        self.assertEqual(conn.calls[-1], "volume_set")

    async def test_value_is_clamped(self):
        conn = await self._send("250")
        self.assertEqual(conn.calls[-1], "volume_set")
        self.assertEqual(conn.volume, 10)

    async def test_garbage_is_ignored(self):
        conn = await self._send("громче")
        self.assertNotIn("volume_set", conn.calls)


class VolumePublishTest(unittest.IsolatedAsyncioTestCase):
    """Опрос состояния публикует громкость один раз на изменение."""

    async def test_volume_is_published_once(self):
        conn = FakeConn()
        conn.volume = 4
        bridge = make_bridge(conn)
        bridge._sync_stations()
        st = bridge._stations["lite-1"]
        await bridge._poll_station(st)
        await bridge._poll_station(st)
        vol = [(t, p) for t, p, _ in bridge._client.published if t.endswith("/volume")]
        self.assertEqual(len(vol), 1)
        self.assertEqual(json.loads(vol[0][1])["level"], 4)


class ModePublishTest(unittest.IsolatedAsyncioTestCase):
    """Режим публикуется retained и только на изменение."""

    async def test_poll_publishes_mode_once(self):
        conn = FakeConn()
        conn.mode = "local"
        bridge = make_bridge(conn)
        bridge._sync_stations()
        st = bridge._stations["lite-1"]
        await bridge._poll_station(st)
        await bridge._poll_station(st)
        modes = [(t, p) for t, p, _ in bridge._client.published if t.endswith("/mode")]
        self.assertEqual(len(modes), 1)
        self.assertEqual(json.loads(modes[0][1])["mode"], "local")


if __name__ == "__main__":
    unittest.main()
