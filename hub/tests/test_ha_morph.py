"""Тесты морфологического слоя быстрого маршрута."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ha_morph import Device, Matcher, normalize, stem, stems  # noqa: E402


def devices() -> list[Device]:
    return [
        Device("light.lustra", "Люстра", area="spalnia", area_name="Комната", role="light"),
        Device("light.lamp", "Настольная лампа", area="spalnia", area_name="Комната",
               synonyms=("лампа", "настольная"), role="light"),
        Device("switch.kettle", "Включение чайника", area="kukhnia", area_name="Кухня",
               synonyms=("чайник",), role="switch"),
        Device("water_heater.kettle", "Чайник", area="kukhnia", area_name="Кухня",
               synonyms=("чайник",), role="water_heater"),
        Device("vacuum.robot", "Пылесос Робот пылесос", area="kukhnia", area_name="Кухня",
               role="vacuum"),
        Device("sensor.bedroom_temp", "Температура", area="spalnia", area_name="Комната",
               role="sensor", kind="sensor", unit="°C"),
        Device("sensor.kitchen_temp", "Температура", area="kukhnia", area_name="Кухня",
               role="sensor", kind="sensor", unit="°C"),
        Device("sensor.water", "Объем воды в чайнике", area="kukhnia", area_name="Кухня",
               role="sensor", kind="sensor", unit="L"),
    ]


class StemTest(unittest.TestCase):
    def test_endings(self):
        self.assertEqual(stem("настольную"), "настольн")
        self.assertEqual(stem("лампу"), "ламп")
        self.assertEqual(stem("чайника"), "чайник")
        self.assertEqual(stem("розетку"), "розетк")

    def test_short_words_stay(self):
        self.assertEqual(stem("свет"), "свет")
        self.assertEqual(stem("ночь"), "ночь")

    def test_numbers(self):
        self.assertEqual(stem("один"), "1")
        self.assertEqual(stem("вторую"), "2")

    def test_normalize(self):
        self.assertEqual(normalize("Включи люстру, пожалуйста!"), "включи люстру пожалуйста")
        self.assertEqual(normalize("Окей, ёлка"), "окей елка")
        self.assertEqual(stems("ёлочка"), ("елочк",))


class FindTest(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(devices())

    def check(self, phrase, entity, **kw):
        best = self.m.best(phrase, **kw)
        self.assertTrue(best is not None, f"не найдено: {phrase}")
        assert best is not None
        self.assertEqual(best.device.entity_id, entity, phrase)

    def test_devices(self):
        self.check("включи настольную лампу", "light.lamp", role="light")
        self.check("выключи люстру", "light.lustra", role="light")
        self.check("включи лампу", "light.lamp", role="light")
        self.check("включи пылесос", "vacuum.robot", role="vacuum")

    def test_kettle_domains(self):
        # одно имя у двух сущностей: без домена неоднозначно
        self.assertIsNone(self.m.best("включи чайник"))
        self.check("включи чайник", "switch.kettle", role="switch")
        self.check("поставь чайник на 70 градусов", "water_heater.kettle", role="water_heater")

    def test_sensors_with_area(self):
        self.check("какая температура в комнате", "sensor.bedroom_temp",
                   kind="sensor", area="spalnia")
        self.check("какая температура на кухне", "sensor.kitchen_temp",
                   kind="sensor", area="kukhnia")
        self.assertIsNone(self.m.best("какая температура", kind="sensor"))
        self.check("сколько воды в чайнике", "sensor.water", kind="sensor")

    def test_commands_ignore_sensors(self):
        self.assertIsNone(self.m.best("сколько воды в чайнике", kind="actuator"))

    def test_no_match(self):
        self.assertIsNone(self.m.best("включи музыку", kind="actuator"))
        self.assertIsNone(self.m.best("включи свет", role="light"))
        self.assertIsNone(self.m.best("сколько времени", kind="sensor"))

    def test_duplicate_names_ambiguous(self):
        m = Matcher([
            Device("climate.ac1", "Кондиционер", role="climate"),
            Device("climate.ac2", "Кондиционер", role="climate"),
        ])
        self.assertIsNone(m.best("включи кондиционер", role="climate"))


if __name__ == "__main__":
    unittest.main()
