"""Параметры детектора живут в hub.json, а не в агенте.

Детектор крутится в хабе: колонка присылает сырой PCM, `LocalWakeGate`
считает score, он же сравнивает с порогом. Поэтому порог, усиление и полоса
сбора промахов — настройки хаба, с переопределением по имени станции.

Правила:
1. `wake.stations.<имя>.<ключ>` главнее общего `wake.<ключ>`.
2. Нет ключа станции — берётся общий; нет общего — дефолт кода.
3. Значение агента в `hello` не применяется: порог 0.95 не должен зависеть
   от бинарника на колонке и от забытого `agent.env`.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_hub  # noqa: E402

DATA = {
    "wake": {
        "threshold": 0.95,
        "gain": 10.0,
        "miss_min": 0.65,
        "stations": {
            "lite-2": {"threshold": 0.90, "gain": 12.0},
        },
    },
}


def fake_get(*keys, default=None):
    node = DATA
    for key in keys:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return default
    return node


class StationWakeTest(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(voice_hub.config, "get", fake_get)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_station_override_wins(self):
        self.assertAlmostEqual(voice_hub.station_wake("lite-2", "threshold", 0.95), 0.90)
        self.assertAlmostEqual(voice_hub.station_wake("lite-2", "gain", 10.0), 12.0)

    def test_common_value_when_station_has_no_override(self):
        self.assertAlmostEqual(voice_hub.station_wake("lite-1", "threshold", 0.95), 0.95)
        self.assertAlmostEqual(voice_hub.station_wake("lite-1", "gain", 10.0), 10.0)

    def test_default_when_key_is_absent(self):
        # Станции нет в списке и общего ключа нет — работает дефолт кода.
        self.assertAlmostEqual(voice_hub.station_wake("lite-3", "unknown", 0.7), 0.7)

    def test_non_numeric_value_falls_back(self):
        data = {"wake": {"threshold": "высокий"}}
        with mock.patch.object(voice_hub.config, "get",
                               lambda *keys, default=None: _pick(data, keys, default)):
            self.assertAlmostEqual(voice_hub.station_wake("lite-1", "threshold", 0.95), 0.95)


def _pick(tree, keys, default):
    node = tree
    for key in keys:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return default
    return node


if __name__ == "__main__":
    unittest.main()
