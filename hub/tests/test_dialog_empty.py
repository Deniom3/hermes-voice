"""Окно продолжения: пустое распознавание закрывает его сразу.

Smart Turn закрыл фразу (конец речи), но ASR не вернул ни слова — значит
продолжения нет. Раньше окно в этом месте оставалось в DIALOG_WAIT до
таймаута и заново открывало сбор, и со стороны это выглядело как «агент
долго ждёт» после ответа.
"""
import asyncio
import os
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_core  # noqa: E402


class FakeWake(voice_core.WakeGate):
    last_max = 0.0

    async def feed(self, pcm):
        return []

    def reset(self):
        pass


def make_core(text):
    states = []

    async def asr(_pcm):
        return text

    async def on_phrase(*_args):
        raise AssertionError("пустая фраза не должна уходить агенту")

    async def on_state(state, origin=None, score=None):
        states.append((state, origin))

    core = voice_core.SpeechCore(
        wake=FakeWake(), asr=asr, on_state=on_state, on_phrase=on_phrase,
        cfg=voice_core.CoreConfig(smart_turn=False))
    return core, states


class DialogEmptyTest(unittest.TestCase):
    def test_empty_asr_closes_dialog(self):
        core, states = make_core("")
        core.state = voice_core.State.BUSY
        core.dialog_active = True
        core.dialog_until = time.time() + 12

        pcm = np.zeros(16000, dtype=np.int16)
        asyncio.run(core._recognize(pcm, b"", "dialog"))

        self.assertIs(core.state, voice_core.State.IDLE)
        self.assertEqual(core.dialog_until, 0.0)
        self.assertFalse(core.dialog_active)
        self.assertIn((voice_core.State.IDLE, None), states)


if __name__ == "__main__":
    unittest.main()
