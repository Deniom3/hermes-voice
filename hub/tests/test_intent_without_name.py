"""Фраза-триггер без обращения в тексте: имя не должно её блокировать.

Правило контура: детектор подтвердил активацию, значит обращение было. Но
окно распознавания начинается с первого кадра речи, и тихое «окей, Гермес»
в него не попадает — ASR отдаёт только «Спокойной ночи.». Проверка
обращения по тексту отбрасывала такую фразу до реестра интентов, сцена в
Home Assistant не выполнялась, а клип уходил в negatives как ложное
срабатывание.

Проверяем на уровне ядра: фраза из реестра доходит до `on_phrase` даже без
имени, а обычная речь без имени по-прежнему отбрасывается.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_core  # noqa: E402
import voice_hub  # noqa: E402

RULES = {
    "Спокойной ночи": {
        "phrases": ["Спокойной ночи", "Я спать", "Спать"],
        "anchors": ["ноч", "спат"],
    },
}


class FakeWake(voice_core.WakeGate):
    last_max = 0.0

    async def feed(self, pcm):
        return []

    def reset(self):
        pass


def make_core(text, intent_of=None):
    calls, states = [], []

    async def asr(_pcm):
        return text

    async def on_phrase(value, origin, had_wake):
        calls.append((value, origin, had_wake))

    async def on_state(state, origin=None, score=None):
        states.append((state, origin))

    core = voice_core.SpeechCore(
        wake=FakeWake(), asr=asr, on_state=on_state, on_phrase=on_phrase,
        cfg=voice_core.CoreConfig(smart_turn=False))
    if intent_of is not None:
        core.intent_of = intent_of
    return core, calls, states


def recognize(core):
    core.state = voice_core.State.BUSY
    pcm = np.zeros(16000, dtype=np.int16)
    asyncio.run(core._recognize(pcm, b"", "wake"))


class IntentWithoutNameTest(unittest.TestCase):
    def setUp(self):
        # Реестр в тесте свой: в CI /etc/hermes-voice/hub.json нет.
        patcher = mock.patch.object(voice_hub.config, "intents", lambda: RULES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_intent_without_name_reaches_phrase_handler(self):
        core, calls, states = make_core(
            "Спокойной ночи.", voice_hub.match_intent)
        recognize(core)
        self.assertEqual(calls, [("Спокойной ночи.", "wake", True)])
        self.assertNotIn("no_wake_name", [o for _s, o in states])

    def test_regular_phrase_without_name_is_still_dropped(self):
        core, calls, states = make_core("включи телевизор")
        recognize(core)
        self.assertEqual(calls, [])
        self.assertIn((voice_core.State.IDLE, "no_wake_name"), states)

    def test_name_in_text_still_works(self):
        core, calls, _ = make_core("Окей Гермес, сколько времени?")
        recognize(core)
        self.assertEqual(len(calls), 1)

    def test_intent_matcher_returns_canonical_phrase(self):
        self.assertEqual(voice_hub.match_intent("Спокойной ночи."),
                         "Спокойной ночи")
        self.assertEqual(voice_hub.match_intent("я спать"), "Спокойной ночи")
        self.assertIsNone(voice_hub.match_intent("включи ночник"))


if __name__ == "__main__":
    unittest.main()
