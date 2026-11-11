"""Кольцо и музыка не должны опережать звук.

Дефект, который эти тесты закрепляют: на длинной фразе кольцо гасло раньше
начала речи (страховочный таймаут 15 с без учёта длины), а музыка
возвращалась, пока колонка ещё говорила — сервер считал концом отправку
последнего чанка в сокет.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_hub  # noqa: E402
import sources  # noqa: E402

State = voice_hub.State

try:                      # запуск из корня репозитория
    from hub.tests.test_hub_states import make_conn  # noqa: E402
except ImportError:       # запуск внутри hub/tests
    from test_hub_states import make_conn  # noqa: E402


class CollectingWhileSpeakingTest(unittest.IsolatedAsyncioTestCase):
    """Окно продолжения диалога открывается сразу после ответа.

    На длинной фразе колонка ещё доигрывает буфер, и раньше этот переход
    ставил `hermes_listening` — кольцо менялось посреди речи.
    """

    def setUp(self):
        self._old_ma_pause = voice_hub.ma_control.pause_if_playing

        async def no_ma(_station):
            return False

        voice_hub.ma_control.pause_if_playing = no_ma

    def tearDown(self):
        voice_hub.ma_control.pause_if_playing = self._old_ma_pause

    async def test_continuation_does_not_touch_ring_while_audio_plays(self):
        conn = make_conn()
        conn._pending_say = True
        conn._audio_deadline = asyncio.get_running_loop().time() + 30
        await conn._on_state(State.COLLECTING, "dialog", 0.0)
        self.assertNotIn({"cmd": "led", "name": "hermes_listening"}, conn.commands)

    async def test_continuation_switches_ring_after_audio(self):
        conn = make_conn()
        conn._pending_say = False
        await conn._on_state(State.COLLECTING, "dialog", 0.0)
        self.assertIn({"cmd": "led", "name": "hermes_listening"}, conn.commands)

    async def test_wake_always_restarts_ring(self):
        """Новый ход по ключевому слову обрывает прошлый ответ — кольцо меняем."""
        conn = make_conn()
        conn._pending_say = True
        conn._audio_deadline = asyncio.get_running_loop().time() + 30
        await conn._on_state(State.COLLECTING, "wake", 0.97)
        self.assertIn({"cmd": "led", "name": "hermes_listening"}, conn.commands)

    async def test_ring_returns_to_listening_after_speech(self):
        """Кольцо «говорит» погашено, но сбор идёт — значит снова «слушаю»."""
        conn = make_conn(state=State.COLLECTING)
        await conn._finish_speech()
        self.assertIn({"cmd": "led", "name": "hermes_listening"}, conn.commands)

    async def test_dialog_hold_released_after_speech(self):
        """Окно диалога открыто, но микрофон отпускаем только после звука.

        Иначе микрофон слышит ответ колонки, сборщик собирает «фразу»,
        автомат уходит в BUSY и перебивает кольцо посреди речи.
        """
        conn = make_conn(state=State.DIALOG_WAIT)
        conn._dialog_hold = True
        conn._dialog_question = True
        conn.core.dialog_collect_done = True
        with patch.object(voice_hub, "RESUME_DELAY", 0.0):
            await conn._finish_speech()
            await asyncio.sleep(0.05)
        self.assertFalse(conn._dialog_hold)
        self.assertIn({"cmd": "led", "name": "hermes_listening"}, conn.commands)


class SpeakLimitTest(unittest.TestCase):
    def test_limit_covers_normal_answer(self):
        """400 символов обрезали живые ответы (413 симв.) — фраза рвалась."""
        import voice_dialog
        self.assertGreaterEqual(voice_dialog.SPEAK_MAX_CHARS, 1000)


class SpeechDeadlineTest(unittest.TestCase):
    def test_deadline_includes_chunk_length(self):
        """Оценка конца речи = отправка + длительность чанка, иначе рано."""
        now = 100.0
        end = voice_hub.speech_end_at(now, 4.0)
        self.assertGreaterEqual(end, now + 4.0)

    def test_deadline_accumulates_for_queued_chunks(self):
        """Куски играют по очереди: оценка обязана накапливаться.

        Без накопления четыре куска по 4 с давали оценку 4 с вместо 16 —
        и музыка возвращалась посреди ответа.
        """
        now = 100.0
        end = 0.0
        for _ in range(4):
            end = voice_hub.speech_end_at(now, 4.0, end)
        self.assertGreaterEqual(end, now + 16.0)

    def test_empty_chunk_has_no_duration(self):
        self.assertEqual(voice_hub.speech_end_at(100.0, 0.0), 0.0)
        self.assertEqual(voice_hub.speech_end_at(100.0, 0.0, 55.0), 55.0)


class FakeConn:
    """Минимальная колонка: помнит команды, состояние ядра — IDLE."""

    def __init__(self):
        self.calls = []
        self.name = "lite-1"
        self._play_done = False
        self._pending_say = False
        self._sending_audio = False
        self._dialog_hold = False
        self._audio_deadline = 0.0
        self._was_playing = True
        self._resume_task = None
        self._led_task = None
        self.core = type("Core", (), {
            "follow_up": False,
            "coll": None,
            "state": voice_hub.State.IDLE,
            "dialog_collect_done": False,
        })()
        self.core.open_dialog = lambda was_question: setattr(
            self.core, "dialog_collect_done", False)

    async def command(self, payload, timeout=15):
        self.calls.append(payload.get("cmd"))
        return {}

    @property
    def led_off(self) -> bool:
        return any(c == "led" for c in self.calls)


class ConnMethods:
    """Подмешиваем настоящие методы Conn в подделку."""

    _cancel_led_task = voice_hub.Conn._cancel_led_task
    _clear_led_after_audio = voice_hub.Conn._clear_led_after_audio
    _finish_speech = voice_hub.Conn._finish_speech
    _release_after_speech = voice_hub.Conn._release_after_speech
    _plan_resume_music = voice_hub.Conn._plan_resume_music
    _resume_music = voice_hub.Conn._resume_music
    _audio_playing = voice_hub.Conn._audio_playing
    _output_busy = voice_hub.Conn._output_busy
    on_play_done = voice_hub.Conn.on_play_done


class FakeStation(ConnMethods, FakeConn):
    # `_resume_music` пишет в снимок источников — фейку он тоже нужен.
    _sources = sources.Sources()

    async def command(self, payload, timeout=15):
        self.calls.append(payload.get("name") if payload.get("cmd") == "led"
                           else payload.get("cmd"))
        return {}


class LedWaitTest(unittest.IsolatedAsyncioTestCase):
    async def test_led_waits_for_play_done(self):
        """Пока плеер не подтвердил конец, кольцо не гасим и музыку не включаем."""
        st = FakeStation()
        st._pending_say = True
        st._audio_deadline = time.monotonic() + 30  # длинная фраза ещё в буфере
        with patch.object(voice_hub, "RESUME_DELAY", 0.0):
            st._clear_led_after_audio()
            await asyncio.sleep(0.05)
            self.assertNotIn("play", st.calls)
            self.assertFalse(any(c == "" for c in st.calls), "кольцо погашено раньше времени")

            await st.on_play_done()      # агент доиграл
            await asyncio.sleep(0.05)

        self.assertIn("play", st.calls)
        self.assertIn("", st.calls, "кольцо должно быть погашено")

    async def test_play_done_between_chunks_is_ignored(self):
        """Очередь агента пустеет и между кусками — это не конец речи.

        Иначе на длинном ответе, где TTS синтезирует следующий кусок с паузой,
        кольцо гасло и музыка возвращалась в середине фразы.
        """
        st = FakeStation()
        st._sending_audio = True
        st._pending_say = True
        st._audio_deadline = time.monotonic() + 30
        await st.on_play_done()
        self.assertFalse(any(c == "" for c in st.calls), "кольцо погашено на промежуточном сигнале")
        self.assertNotIn("play", st.calls)

    async def test_play_done_after_sending_completes(self):
        st = FakeStation()
        st._sending_audio = False
        st._pending_say = True
        st._audio_deadline = time.monotonic() + 30
        with patch.object(voice_hub, "RESUME_DELAY", 0.0):
            await st.on_play_done()
            await asyncio.sleep(0.05)
        self.assertIn("", st.calls)

    async def test_no_audio_means_immediate_release(self):
        """Ложная активация без звука: гасим сразу, музыку возвращаем."""
        st = FakeStation()
        st._pending_say = False
        with patch.object(voice_hub, "RESUME_DELAY", 0.0):
            st._clear_led_after_audio()
            await asyncio.sleep(0.05)
        self.assertIn("play", st.calls)

    async def test_play_done_without_music_does_not_start_it(self):
        st = FakeStation()
        st._was_playing = False
        st._pending_say = True
        st._audio_deadline = time.monotonic() + 30
        with patch.object(voice_hub, "RESUME_DELAY", 0.0):
            st._clear_led_after_audio()
            await st.on_play_done()
            await asyncio.sleep(0.05)
        self.assertNotIn("play", st.calls)


if __name__ == "__main__":
    unittest.main()
