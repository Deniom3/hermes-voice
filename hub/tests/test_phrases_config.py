"""Фразы быстрого маршрута живут в hub.json, а не в коде.

Проверяем две вещи:
1. В рабочем конфиге есть все группы слов — иначе маршрут не стартует.
2. Отсутствие ключа — отказ с внятной ошибкой, а не тихий пустой список:
   с пустым списком команда ушла бы в LLM, и это выглядело бы как
   «перестало понимать», а не как ошибка конфига.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import ha_morph  # noqa: E402
import ha_route  # noqa: E402

GROUPS = ("on", "off", "question", "music", "weather", "sensor",
          "volume", "mute", "up", "down", "actuator_domains")


class ConfigHasPhrasesTest(unittest.TestCase):
    def test_all_groups_present(self):
        for key in GROUPS:
            with self.subTest(key=key):
                value = config.as_list("phrases", key, default=None)
                self.assertTrue(value, f"phrases.{key} пуст или отсутствует")
                self.assertTrue(all(isinstance(v, str) and v for v in value),
                                f"phrases.{key}: пустые элементы")

    def test_morphology_present(self):
        for key in ("endings", "numbers", "name_stop", "word_stop"):
            with self.subTest(key=key):
                value = config.get("phrases", "morph", key, default=None)
                self.assertTrue(value, f"phrases.morph.{key} пуст или отсутствует")

    def test_word_numbers_and_units(self):
        numbers = config.get("phrases", "word_numbers", default=None)
        self.assertIn("два", numbers)
        self.assertEqual(numbers["два"], 2)
        units = config.get("phrases", "units", default=None)
        self.assertIn("°C", units)


class LoadedPhrasesTest(unittest.TestCase):
    """Что реально загрузилось в модули — защита от потери слов при переносе."""

    def test_key_words_survive(self):
        for word in ("включ", "зажги"):
            self.assertIn(word, ha_route.ON_WORDS)
        for word in ("выключ", "стоп"):
            self.assertIn(word, ha_route.OFF_WORDS)
        self.assertIn("погод", ha_route.WEATHER_WORDS)
        self.assertIn("громкост", ha_route.VOLUME_WORDS)
        self.assertIn("light", ha_route.ACTUATOR_DOMAINS)
        self.assertEqual(ha_route.WORD_NUMBERS["двадцать"], 20)
        self.assertEqual(ha_route.unit_words("L"), ("литр", "литра", "литров"))

    def test_morphology_loaded(self):
        self.assertIn("иями", ha_morph._ENDINGS)
        self.assertEqual(ha_morph.stem("лампу"), "ламп")
        self.assertEqual(ha_morph.stem("два"), "2")
        self.assertIn("включение", ha_morph._NAME_STOP)
        self.assertIn("пожалуйста", ha_morph._WORD_STOP)


class MissingKeyTest(unittest.TestCase):
    def test_words_raises_without_key(self):
        with mock.patch.object(config, "as_list", lambda *a, **kw: None):
            with self.assertRaises(RuntimeError) as ctx:
                ha_route._words("on")
        self.assertIn("phrases.on", str(ctx.exception))

    def test_mapping_raises_without_key(self):
        with mock.patch.object(config, "get", lambda *a, **kw: None):
            with self.assertRaises(RuntimeError):
                ha_route._mapping("word_numbers")

    def test_morph_raises_without_key(self):
        with mock.patch.object(config, "get", lambda *a, **kw: None):
            with self.assertRaises(RuntimeError) as ctx:
                ha_morph._morph("endings")
        self.assertIn("phrases.morph.endings", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
