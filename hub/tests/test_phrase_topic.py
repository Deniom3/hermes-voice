"""Единый конфиг, реестр фраз и ветвление «HA или LLM».

Правило контура: распознанная фраза без обращения сравнивается с реестром
из `hub.json`. Совпала — уходит в Home Assistant и НЕ идёт в LLM; не
совпала — идёт в LLM как обычный запрос. Иначе сцена выполнится дважды.
"""
import asyncio
import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import intents  # noqa: E402
import mqtt_bridge  # noqa: E402
import voice_dialog  # noqa: E402

RULES = {
    "Спокойной ночи": {
        "phrases": ["Спокойной ночи", "Я спать", "Спать"],
        "anchors": ["ноч", "спат"],
    },
    "Вода в чайнике": {
        "phrases": ["Вода в чайнике", "Сколько воды в чайнике"],
        "anchors": ["чайник"],
    },
    "Да": {"phrases": ["Да"], "anchors": [], "mqtt": False},
}


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic, qos=0):
        pass


class FakeConn:
    def __init__(self, name="lite-1"):
        self.name = name
        self.role = "station"
        self.core = SimpleNamespace(state=SimpleNamespace(value="idle"))

    volume = 5
    volume_db = -25.4


class StripWakeTest(unittest.TestCase):
    """Срезаем только начало фразы и только до трёх слов."""

    def test_full_greeting_and_phrase(self):
        self.assertEqual(
            voice_dialog.strip_wake("О'кей, Гермес, спокойной ночи"),
            "спокойной ночи")

    def test_greeting_without_comma(self):
        self.assertEqual(
            voice_dialog.strip_wake("Окей Гермес сколько времени?"),
            "сколько времени?")

    def test_short_name_form(self):
        self.assertEqual(
            voice_dialog.strip_wake("Герми, включи свет"), "включи свет")

    def test_name_without_okey(self):
        self.assertEqual(
            voice_dialog.strip_wake("Гермес, вода в чайнике"), "вода в чайнике")

    def test_phrase_without_greeting_is_untouched(self):
        self.assertEqual(
            voice_dialog.strip_wake("Спокойной ночи"), "Спокойной ночи")

    def test_name_in_the_middle_is_untouched(self):
        self.assertEqual(
            voice_dialog.strip_wake("Включи трек Гермес"), "Включи трек Гермес")

    def test_only_greeting_gives_empty(self):
        self.assertEqual(voice_dialog.strip_wake("Окей, Гермес"), "")

    def test_empty(self):
        self.assertEqual(voice_dialog.strip_wake(""), "")

    def test_okey_alone_is_stripped(self):
        self.assertEqual(voice_dialog.strip_wake("Ок, включи свет"), "включи свет")

    def test_not_a_greeting_word(self):
        # «Около» начинается как «ок», но это не обращение.
        self.assertEqual(
            voice_dialog.strip_wake("Около дома холодно"), "Около дома холодно")


class IntentMatchTest(unittest.TestCase):
    """Сопоставление терпит ошибки ASR, но не ловит постороннюю речь."""

    def test_exact(self):
        self.assertEqual(intents.match("спокойной ночи", RULES), "Спокойной ночи")

    def test_asr_error_one_letter(self):
        self.assertEqual(intents.match("спакойной ночи", RULES), "Спокойной ночи")

    def test_phrase_with_extra_word(self):
        self.assertEqual(intents.match("я спать", RULES), "Спокойной ночи")

    def test_anchor_for_strong_distortion(self):
        self.assertEqual(intents.match("спакойнай ноче", RULES), "Спокойной ночи")

    def test_unrelated_text_is_not_matched(self):
        self.assertIsNone(intents.match("сколько времени", RULES))

    def test_long_phrase_with_anchor_is_not_matched(self):
        self.assertIsNone(
            intents.match("включи ночник в спальне и закрой шторы", RULES))

    def test_short_phrase_with_anchor_but_far_is_not_matched(self):
        # «ночник» содержит «ноч», но к «спокойной ночи» это не относится.
        self.assertIsNone(intents.match("включи ночник", RULES))

    def test_short_phrase_with_anchor_and_close_matches(self):
        self.assertEqual(intents.match("спакойнай ноче", RULES), "Спокойной ночи")

    def test_yes_is_excluded_from_mqtt(self):
        self.assertTrue(intents.is_excluded("Да", RULES))
        self.assertFalse(intents.is_excluded("Спокойной ночи", RULES))


class PublishPhraseTest(unittest.TestCase):
    """Топик на колонку, без retain, повтор не глушится."""

    def make_bridge(self, *conns):
        bridge = mqtt_bridge.MqttBridge(lambda: list(conns))
        bridge._client = FakeClient()
        return bridge

    def test_topic_path_has_station(self):
        bridge = self.make_bridge(FakeConn("lite-2"))
        self.assertTrue(bridge.publish_phrase("lite-2", "Спокойной ночи"))
        topic, payload, retain = bridge._client.published[-1]
        self.assertEqual(topic, "hermes/stations/lite-2/phrase")
        self.assertEqual(json.loads(payload), {"text": "Спокойной ночи"})
        self.assertFalse(retain)

    def test_repeat_is_published_again(self):
        bridge = self.make_bridge(FakeConn("lite-1"))
        bridge.publish_phrase("lite-1", "Спокойной ночи")
        bridge.publish_phrase("lite-1", "Спокойной ночи")
        phrases = [p for p in bridge._client.published if p[0].endswith("/phrase")]
        self.assertEqual(len(phrases), 2)

    def test_empty_text_is_not_published(self):
        bridge = self.make_bridge(FakeConn("lite-1"))
        self.assertFalse(bridge.publish_phrase("lite-1", "   "))
        self.assertEqual(bridge._client.published, [])

    def test_unknown_station_is_not_published(self):
        bridge = self.make_bridge(FakeConn("lite-1"))
        self.assertFalse(bridge.publish_phrase("lite-9", "Спокойной ночи"))
        # Синхронизация могла отправить discovery для известной станции —
        # проверяем именно фразовый топик.
        phrases = [p for p in bridge._client.published if p[0].endswith("/phrase")]
        self.assertEqual(phrases, [])


class OnPhraseTest(unittest.TestCase):
    """Совпало с фразой — только HA; не совпало — только LLM."""

    def run_phrase(self, text, origin="wake", rules=None):
        import voice_hub

        published, spoken = [], []

        def publish(station, value):
            published.append((station, value))
            return True

        fake = SimpleNamespace(publish_phrase=publish)

        async def speak(value, origin=None):
            spoken.append(value)

        conn = SimpleNamespace(
            name="lite-1",
            core=SimpleNamespace(expect_followup=lambda: None),
            _save_settled=lambda *a, **k: asyncio.sleep(0),
            _pause_music_for_turn=lambda: asyncio.sleep(0),
            _speak=speak)

        old_bridge = voice_hub.MQTT_BRIDGE
        old_intents = voice_hub.config.intents
        voice_hub.MQTT_BRIDGE = fake
        voice_hub.config.intents = lambda: (rules if rules is not None else RULES)
        try:
            asyncio.run(voice_hub.Conn._on_phrase(
                conn, text, origin, origin == "wake"))
        finally:
            voice_hub.MQTT_BRIDGE = old_bridge
            voice_hub.config.intents = old_intents
        return published, spoken

    def test_intent_goes_to_ha_and_not_to_llm(self):
        published, spoken = self.run_phrase("О'кей, Гермес, спокойной ночи")
        self.assertEqual(published, [("lite-1", "Спокойной ночи")])
        self.assertEqual(spoken, [])

    def test_asr_distortion_still_hits_intent(self):
        published, spoken = self.run_phrase("Окей Гермес спакойной ночи")
        self.assertEqual(published, [("lite-1", "Спокойной ночи")])
        self.assertEqual(spoken, [])

    def test_regular_phrase_goes_to_llm_without_greeting(self):
        published, spoken = self.run_phrase("О'кей, Гермес, сколько времени?")
        self.assertEqual(published, [])
        self.assertEqual(spoken, ["сколько времени?"])

    def test_yes_is_not_sent_to_ha(self):
        published, _ = self.run_phrase("Окей Гермес, да")
        self.assertEqual(published, [])

    def test_dialog_phrase_is_not_treated_as_scenario(self):
        published, spoken = self.run_phrase("спокойной ночи", origin="dialog")
        self.assertEqual(published, [])
        self.assertEqual(spoken, ["спокойной ночи"])

    def test_failed_publish_falls_back_to_llm(self):
        import voice_hub

        spoken = []
        fake = SimpleNamespace(publish_phrase=lambda station, value: False)

        async def speak(value, origin=None):
            spoken.append(value)

        conn = SimpleNamespace(
            name="lite-1",
            core=SimpleNamespace(expect_followup=lambda: None),
            _save_settled=lambda *a, **k: asyncio.sleep(0),
            _pause_music_for_turn=lambda: asyncio.sleep(0),
            _speak=speak)

        old_bridge = voice_hub.MQTT_BRIDGE
        old_intents = voice_hub.config.intents
        voice_hub.MQTT_BRIDGE = fake
        voice_hub.config.intents = lambda: RULES
        try:
            asyncio.run(voice_hub.Conn._on_phrase(
                conn, "О'кей, Гермес, спокойной ночи", "wake", True))
        finally:
            voice_hub.MQTT_BRIDGE = old_bridge
            voice_hub.config.intents = old_intents

        # Брокер недоступен — фраза не должна потеряться.
        self.assertEqual(spoken, ["спокойной ночи"])


class ConfigTest(unittest.TestCase):
    """Файл читается, битый не роняет, env переопределяет."""

    def setUp(self):
        self.path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "hub-test.json")
        self.old_path, config.PATH = config.PATH, self.path
        config._mtime, config._data = None, {}

    def tearDown(self):
        config.PATH = self.old_path
        config._mtime, config._data = None, {}
        if os.path.exists(self.path):
            os.unlink(self.path)

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def test_reads_nested_value(self):
        self.write({"mqtt": {"prefix": "hermes/stations", "port": 1883}})
        self.assertEqual(config.get("mqtt", "prefix"), "hermes/stations")
        self.assertEqual(config.as_int("mqtt", "port", default=1), 1883)

    def test_missing_key_gives_default(self):
        self.write({"mqtt": {}})
        self.assertEqual(config.get("mqtt", "prefix", default="x"), "x")

    def test_broken_file_keeps_previous(self):
        self.write({"mqtt": {"prefix": "ok"}})
        self.assertEqual(config.get("mqtt", "prefix"), "ok")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{битый")
        os.utime(self.path, (1, 1))
        self.assertEqual(config.get("mqtt", "prefix", default="x"), "ok")

    def test_env_overrides(self):
        self.write({"mqtt": {"prefix": "from-file"}})
        os.environ["HUB_MQTT_PREFIX"] = "from-env"
        try:
            self.assertEqual(config.get("mqtt", "prefix"), "from-env")
        finally:
            del os.environ["HUB_MQTT_PREFIX"]

    def test_intents_section(self):
        self.write({"intents": RULES})
        self.assertIn("Спокойной ночи", config.intents())


if __name__ == "__main__":
    unittest.main()
