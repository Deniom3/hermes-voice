"""Музыка не возвращается, пока ответ не доиграл.

Дефект 2026-11-02 18:42 (последнее живое обращение):

    18:41:55.052 led → hermes_speaking        (начало озвучки)
    18:41:56.030 озвучено кусков 3, окно диалога открыто на 12 с
    18:42:08.048 окно диалога закрыто (тишина)  ← таймер истёк во время речи
    18:42:11.182 музыка возвращена              ← play поверх речи
    18:42:15.728 звук доигран (play_done)       ← речь кончилась позже

Окно считалось от конца синтеза, а не от конца звука; `_resume_music` не
смотрел на `_output_busy()`. Тесты закрепляют оба условия.
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import voice_hub  # noqa: E402
import voice_core  # noqa: E402

State = voice_hub.State

try:                      # запуск из корня репозитория
    from hub.tests.test_hub_states import make_conn  # noqa: E402
except ImportError:       # запуск внутри hub/tests
    from test_hub_states import make_conn  # noqa: E402


class ResumeMusicGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_play_while_answer_still_sounds(self):
        conn = make_conn()
        conn._was_playing = True
        conn._pending_say = True
        conn._audio_deadline = asyncio.get_running_loop().time() + 30
        with patch.object(voice_hub, "RESUME_DELAY", 0.0), \
             patch.object(voice_hub, "MAX_SPEECH_WAIT", 0.2):
            await conn._resume_music()
        self.assertNotIn({"cmd": "play"}, conn.commands)

    async def test_no_play_while_dialog_hold(self):
        """Окно открыто, микрофон ждёт — музыка ждёт конца речи."""
        conn = make_conn()
        conn._was_playing = True
        conn._dialog_hold = True
        with patch.object(voice_hub, "RESUME_DELAY", 0.0), \
             patch.object(voice_hub, "MAX_SPEECH_WAIT", 0.2):
            await conn._resume_music()
        self.assertNotIn({"cmd": "play"}, conn.commands)

    async def test_play_after_answer_finished(self):
        conn = make_conn()
        conn._was_playing = True
        conn._play_done = True
        conn._audio_deadline = 0.0
        with patch.object(voice_hub, "RESUME_DELAY", 0.0):
            await conn._resume_music()
        self.assertIn({"cmd": "play"}, conn.commands)
        self.assertFalse(conn._was_playing)

    async def test_finish_speech_clears_deadline(self):
        """После факта «доиграно» оценка не должна держать флаг «звучит»."""
        conn = make_conn(state=State.IDLE)
        conn._audio_deadline = asyncio.get_running_loop().time() + 30
        with patch.object(voice_hub, "RESUME_DELAY", 0.0):
            await conn._finish_speech()
            await asyncio.sleep(0.05)
        self.assertEqual(conn._audio_deadline, 0.0)
        self.assertFalse(conn._audio_playing())


class DialogWindowDuringSpeechTest(unittest.TestCase):
    """Окно продолжения не закрывается по таймеру, пока колонка говорит."""

    def _core(self, busy: bool):
        core = voice_core.SpeechCore.__new__(voice_core.SpeechCore)
        core.dialog_active = True
        core.dialog_until = time.time() - 5      # таймер уже истёк
        core.state = State.DIALOG_WAIT
        core.output_busy = lambda: busy
        return core

    def test_window_stays_open_while_speaking(self):
        self.assertFalse(self._core(busy=True).dialog_expired())

    def test_window_closes_when_silent(self):
        self.assertTrue(self._core(busy=False).dialog_expired())

    def test_window_waits_while_busy(self):
        core = self._core(busy=True)
        core.state = State.BUSY
        self.assertFalse(core.dialog_expired())


if __name__ == "__main__":
    unittest.main()
