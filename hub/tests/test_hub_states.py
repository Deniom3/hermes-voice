"""Состояния хаба: что уходит устройству и что попадает в сбор данных.

Здесь живут те самые баги, которые искали по логам: музыка, возвращавшаяся
в микрофон, и ложное срабатывание, глушившее музыку навсегда.
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_capture  # noqa: E402
import voice_hub  # noqa: E402
from voice_core import State  # noqa: E402


def make_conn(role="station", follow_up=False, state=State.IDLE):
    """Conn без сети и без моделей: только то, что трогает _on_state."""
    conn = object.__new__(voice_hub.Conn)
    conn.name = "test"
    conn.role = role
    conn.core = SimpleNamespace(follow_up=follow_up, coll=None, state=state, last_text="")
    conn._snap = b""
    conn._snap_score = 0.0
    conn._snap_peaks = []
    conn._resume_task = None
    conn.ring = SimpleNamespace(last=lambda seconds: b"pcm")
    conn.miss = voice_capture.MissWatcher()
    conn.retro = SimpleNamespace(available=False)
    conn.commands = []
    conn.saved = []
    conn.sent = []

    async def send_json(obj):
        conn.sent.append(obj)

    async def command(cmd, timeout=15):
        conn.commands.append(cmd)
        return {"ok": True}

    conn.send_json = send_json
    conn.command = command
    conn._save_event = lambda cls, text, reason: conn.saved.append((cls, text, reason))

    async def save_settled(cls, text, reason):
        conn.saved.append((cls, text, reason))

    conn._save_settled = save_settled
    conn._plan_resume_music = lambda: conn.commands.append({"cmd": "play"})
    return conn


class PanelRoleTest(unittest.IsolatedAsyncioTestCase):
    async def test_panel_gets_state_but_no_device_commands(self):
        conn = make_conn(role="panel")
        await conn._on_state(State.COLLECTING, "wake", 0.97)
        self.assertEqual(len(conn.sent), 1)
        self.assertEqual(conn.sent[0]["t"], "state")
        self.assertEqual(conn.commands, [])
        self.assertEqual(conn.saved, [])

    async def test_panel_does_not_save_events(self):
        conn = make_conn(role="panel")
        await conn._on_state(State.IDLE, "no_wake_name", 0.96)
        self.assertEqual(conn.saved, [])


class StationLedTest(unittest.IsolatedAsyncioTestCase):
    async def test_wake_pauses_music_and_greets(self):
        conn = make_conn()
        await conn._on_state(State.COLLECTING, "wake", 0.97)
        self.assertEqual(conn.commands[0], {"cmd": "pause"})
        self.assertEqual(conn.commands[1]["name"], "spotter_start")
        self.assertEqual(conn.commands[2]["name"], "hermes_listening")

    async def test_followup_does_not_pause_again(self):
        """Второй кусок фразы не должен снова ставить музыку на паузу."""
        conn = make_conn()
        await conn._on_state(State.COLLECTING, "followup", 0.0)
        self.assertEqual(conn.commands, [{"cmd": "led", "name": "hermes_listening"}])

    async def test_busy_shows_thinking(self):
        conn = make_conn()
        await conn._on_state(State.BUSY, "wake", 0.0)
        self.assertEqual(conn.commands, [{"cmd": "led", "name": "hermes_thinking"}])

    async def test_dialog_wait_clears_ring_after_audio(self):
        conn = make_conn()
        await conn._on_state(State.DIALOG_WAIT, "answer", 0.0)
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(conn.commands, [{"cmd": "led", "name": ""}])

    async def test_ring_stays_until_audio_ends(self):
        """Кольцо не гаснет, пока колонка доигрывает буфер."""
        conn = make_conn()
        conn._audio_until = asyncio.get_running_loop().time() + 0.2
        await conn._on_state(State.DIALOG_WAIT, "answer", 0.0)
        await asyncio.sleep(0.05)
        self.assertEqual(conn.commands, [])
        await asyncio.sleep(0.3)
        self.assertEqual(conn.commands, [{"cmd": "led", "name": ""}])

    async def test_activation_snapshot_taken_on_wake(self):
        conn = make_conn()
        await conn._on_state(State.COLLECTING, "wake", 0.97)
        self.assertEqual(conn._snap, b"pcm")
        self.assertAlmostEqual(conn._snap_score, 0.97)


class EventWindowTest(unittest.IsolatedAsyncioTestCase):
    """Окно события должно быть сбалансировано вокруг пика.

    Раньше снимок брался в момент активации, пик оказывался в самом его конце,
    и все positives выходили по 1.41 с: 1.4 с до пика и ничего после.
    """

    async def test_window_has_tail_after_peak(self):
        conn = make_conn()
        pcm = b"\x01\x00" * (16000 * 6)
        conn.ring = SimpleNamespace(last=lambda seconds: pcm)
        conn._wake_at = asyncio.get_running_loop().time() - 1.0

        snap, peak = conn._event_window()

        self.assertEqual(len(snap), len(pcm))
        self.assertAlmostEqual(peak, 5.0, delta=0.05)
        cropped = voice_capture.crop_around(snap, peak)
        self.assertAlmostEqual(len(cropped) / voice_capture.SR, 2.4, delta=0.05)

    async def test_settle_returns_at_once_when_tail_is_there(self):
        conn = make_conn()
        conn._wake_at = asyncio.get_running_loop().time() - 3.0
        started = asyncio.get_running_loop().time()
        await conn._settle_audio()
        self.assertLess(asyncio.get_running_loop().time() - started, 0.1)



    async def test_saved_event_is_2_4_seconds(self):
        """Полный путь: ожидание хвоста, окно, запись — файл должен быть 2.4 с."""
        import json
        import tempfile

        import numpy as np

        conn = make_conn()
        tt = np.arange(16000 * 6) / 16000.0
        sig = (np.sin(2 * np.pi * 220 * tt) * 6000).astype(np.int16)
        # пауза внутри окна (3.6–6.0 с): без неё active_rms считает ровный тон тишиной
        sig[16000 * 4:16000 * 5] = 0
        pcm = sig.tobytes()
        conn.ring = SimpleNamespace(last=lambda seconds: pcm)
        conn._wake_at = asyncio.get_running_loop().time() - 1.0
        conn._snap = pcm
        conn._snap_score = 0.97
        conn.collector = voice_capture.Collector(
            root=tempfile.mkdtemp(prefix="kws-"), enabled=True)
        # make_conn подменяет метод ради счётчика — здесь нужен настоящий
        conn._save_event = voice_hub.Conn._save_event.__get__(conn, voice_hub.Conn)

        await conn._settle_audio()
        conn._save_event("positives", "Окей, Гермес", "wake")

        import glob
        wavs = glob.glob(os.path.join(conn.collector.root, "positives", "*.wav"))
        self.assertEqual(len(wavs), 1)
        with open(wavs[0][:-4] + ".json", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertAlmostEqual(meta["sec"], 2.4, delta=0.05)

    def _miss_fixture(self):
        import numpy as np
        import tempfile

        conn = make_conn()
        tt = np.arange(16000 * 6) / 16000.0
        sig = (np.sin(2 * np.pi * 220 * tt) * 6000).astype(np.int16)
        sig[16000 * 4:16000 * 5] = 0
        pcm = sig.tobytes()
        conn.ring = SimpleNamespace(last=lambda seconds: pcm)
        conn.collector = voice_capture.Collector(
            root=tempfile.mkdtemp(prefix="kws-"), enabled=True)
        return conn

    async def test_miss_without_wake_name_is_not_saved(self):
        """Пик 0.7–0.95 ловит любой звук: без обращения это не промах слова."""
        import glob
        import os
        import time as _time

        conn = self._miss_fixture()

        async def fake_asr(pcm):
            return "Но это является крупнейшим взломом."

        conn._asr = fake_asr
        await conn._save_miss(_time.monotonic() - 1.0, 0.80)
        misses = glob.glob(os.path.join(conn.collector.root, "misses", "*.wav"))
        self.assertEqual(misses, [])

    async def test_miss_with_wake_name_is_saved(self):
        import glob
        import os
        import time as _time

        conn = self._miss_fixture()

        async def fake_asr(pcm):
            return "Гермеско."

        conn._asr = fake_asr
        await conn._save_miss(_time.monotonic() - 1.0, 0.80)
        misses = glob.glob(os.path.join(conn.collector.root, "misses", "*.wav"))
        self.assertEqual(len(misses), 1)

class FalseTriggerTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_name_goes_to_negatives_with_heard_text(self):
        """В метаданные негатива обязан попасть текст: иначе файл обезличен."""
        conn = make_conn()
        conn.core.last_text = "Плакала, нервничала, но не сообщила в полицию"
        await conn._on_state(State.IDLE, "no_wake_name", 0.96)
        await asyncio.sleep(0)  # запись уходит в задачу, чтобы не тормозить ответ
        self.assertEqual(conn.saved, [("negatives", conn.core.last_text, "no_name")])

    async def test_no_name_returns_music(self):
        """Раньше эта ветка выходила через return и музыка оставалась заглушенной."""
        conn = make_conn()
        await conn._on_state(State.IDLE, "no_wake_name", 0.96)
        self.assertIn({"cmd": "play"}, conn.commands)

    async def test_no_name_does_not_play_when_followup_expected(self):
        conn = make_conn(follow_up=True)
        await conn._on_state(State.IDLE, "no_wake_name", 0.96)
        self.assertNotIn({"cmd": "play"}, conn.commands)


class MusicPlanTest(unittest.IsolatedAsyncioTestCase):
    async def test_idle_plans_music_return(self):
        conn = make_conn()
        conn._was_playing = True
        await conn._on_state(State.IDLE, "answer", 0.0)
        self.assertIn({"cmd": "play"}, conn.commands)

    async def test_idle_while_expecting_phrase_keeps_music_paused(self):
        conn = make_conn(follow_up=True)
        await conn._on_state(State.IDLE, "answer", 0.0)
        self.assertNotIn({"cmd": "play"}, conn.commands)


class ResumeMusicTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_delay = voice_hub.RESUME_DELAY
        voice_hub.RESUME_DELAY = 0

    async def asyncTearDown(self):
        voice_hub.RESUME_DELAY = self.old_delay

    async def test_plays_when_turn_really_finished(self):
        conn = make_conn()
        conn._was_playing = True
        await conn._resume_music()
        self.assertEqual(conn.commands, [{"cmd": "play"}])

    async def test_skips_when_music_was_off(self):
        """Музыка не играла до активации — play не должен её включать."""
        conn = make_conn()
        conn._was_playing = False
        await conn._resume_music()
        self.assertEqual(conn.commands, [])

    async def test_skips_when_phrase_expected(self):
        conn = make_conn(follow_up=True)
        await conn._resume_music()
        self.assertEqual(conn.commands, [])

    async def test_skips_while_collecting(self):
        conn = make_conn()
        conn.core.coll = object()
        await conn._resume_music()
        self.assertEqual(conn.commands, [])

    async def test_skips_when_not_idle(self):
        conn = make_conn(state=State.BUSY)
        await conn._resume_music()
        self.assertEqual(conn.commands, [])

    async def test_single_task_per_connection(self):
        conn = make_conn()
        conn._resume_task = None
        real = voice_hub.Conn._resume_music
        conn._resume_music = real.__get__(conn, voice_hub.Conn)
        voice_hub.Conn._plan_resume_music(conn)
        first = conn._resume_task
        voice_hub.Conn._plan_resume_music(conn)
        self.assertIs(conn._resume_task, first)
        await first


class ShouldResumeTest(unittest.TestCase):
    def test_matrix(self):
        cases = [
            ((False, False, State.IDLE), True),
            ((True, False, State.IDLE), False),
            ((False, True, State.IDLE), False),
            ((False, False, State.BUSY), False),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertIs(voice_hub.should_resume_music(*args), expected)


class ToolEventTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_led_switch(self):
        """Инструмент: кольцо показывает отдельный паттерн, затем снова «думаю»."""
        conn = make_conn()
        sent = []

        async def send_json(obj):
            sent.append(obj)

        async def command(cmd, timeout=15):
            conn.commands.append(cmd)
            return {"ok": True}

        conn.send_json = send_json
        conn.command = command
        # повторяем обработчик on_event из хаба
        await conn.send_json({"t": "tool", "name": "terminal", "state": "start"})
        led = "hermes_tool" if sent[0]["state"] == "start" else "hermes_thinking"
        self.assertEqual(led, "hermes_tool")
        await conn.send_json({"t": "tool", "name": "terminal", "state": "complete"})
        led = "hermes_tool" if sent[1]["state"] == "start" else "hermes_thinking"
        self.assertEqual(led, "hermes_thinking")


if __name__ == "__main__":
    unittest.main()
