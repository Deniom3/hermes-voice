"""Искажённое ASR имя: срезается и находит фразу-триггер.

Реальный случай 07.11 12:57: ASR вернул «О'кей, Дермис, спокойной ночи.»,
`name_like` признал обращение (Левенштейн ≤3), но `strip_wake` — нет:
у него был свой, более строгий список форм. В реестр ушло
«Дермис, спокойной ночи», совпадения не было, и фраза уехала в LLM вместо
Home Assistant.

Критерий теперь один — `wake_names.is_name`.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_dialog  # noqa: E402
import voice_hub  # noqa: E402
import wake_names  # noqa: E402

RULES = {
    "Спокойной ночи": {
        "phrases": ["Спокойной ночи", "Я спать", "Спать"],
        "anchors": ["ноч", "спат"],
    },
}


class AsrNameTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(voice_hub.config, "intents", lambda: RULES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fuzzy_name_recognised(self):
        self.assertTrue(wake_names.is_name("Дермис"))
        self.assertTrue(wake_names.is_name("Гермя"))
        self.assertFalse(wake_names.is_name("германия"))
        self.assertFalse(wake_names.is_name("герметик"))

    def test_distorted_name_is_stripped(self):
        self.assertEqual(
            voice_dialog.strip_wake("О'кей, Дермис, спокойной ночи."),
            "спокойной ночи.")

    def test_distorted_name_matches_intent(self):
        self.assertEqual(
            voice_hub.match_intent("О'кей, Дермис, спокойной ночи."),
            "Спокойной ночи")

    def test_regular_phrase_keeps_name_out_of_question(self):
        self.assertEqual(
            voice_dialog.strip_wake("О'кей, Дермис, сколько времени?"),
            "сколько времени?")


if __name__ == "__main__":
    unittest.main()
