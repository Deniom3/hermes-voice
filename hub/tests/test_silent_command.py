"""Быстрые команды выполняются молча: маркер SILENT не попадает в TTS."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_dialog  # noqa: E402


class SilentTest(unittest.IsolatedAsyncioTestCase):
    async def test_silent_marker_not_spoken(self):
        async def stream(text):
            yield voice_dialog.SILENT

        async def on_audio(chunk):
            raise AssertionError("для молчаливой команды звука быть не должно")

        turn = voice_dialog.DialogTurn(
            "включи лампу", on_audio=on_audio,
            stream_fn=stream, tts_fn=lambda seg: b"wav",
        )
        answer = await turn.run()
        self.assertEqual(answer, "")
        self.assertTrue(turn.silent)

    async def test_normal_answer_still_spoken(self):
        async def stream(text):
            yield "В комнате двадцать пять градусов."

        audio = []

        async def on_audio(chunk):
            audio.append(chunk)

        turn = voice_dialog.DialogTurn(
            "какая температура", on_audio=on_audio,
            stream_fn=stream, tts_fn=lambda seg: b"wav",
        )
        answer = await turn.run()
        self.assertIn("двадцать пять", answer)
        self.assertFalse(turn.silent)
        self.assertTrue(audio)


if __name__ == "__main__":
    unittest.main()
