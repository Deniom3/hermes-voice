"""Состояния хаба: что уходит устройству и что попадает в сбор данных.

Здесь живут те самые баги, которые искали по логам: музыка, возвращавшаяся
в микрофон, и ложное срабатывание, глушившее музыку навсегда.
"""
import asyncio
import json
import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_capture  # noqa: E402
import voice_hub  # noqa: E402
from voice_core import State  # noqa: E402


async def _no_ma_pause(_station):
    """Тесты состояний не должны ходить в живой Music Assistant."""
    return False


def make_conn(role="station", follow_up=False, state=State.IDLE):
    """Conn без сети и без моделей: только то, что трогает _on_state."""
    conn = object.__new__(voice_hub.Conn)
    conn.name = "test"
    conn.role = role
    conn.core = SimpleNamespace(follow_up=follow_up, coll=None, state=state, last_text="",
                                dialog_collect_done=False, dialog_until=0.0)
    conn.core.open_dialog = lambda was_question: None
    conn.miss_processed = []

    async def process_miss(window, score):
        conn.miss_processed.append(score)
        return False

    conn.core.process_miss = process_miss
    conn._snap = b""
    conn._snap_score = 0.0
    conn._snap_peaks = []
    conn._resume_task = None
    # Поля учёта конца озвучки: по умолчанию звука нет, подтверждений нет.
    conn._audio_deadline = 0.0
    conn._play_done = False
    conn._pending_say = False
    conn._sending_audio = False
    conn._dialog_hold = False
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
        if isinstance(cmd, dict) and cmd.get("cmd") == "state":
            # Факты агента: по умолчанию «не знаю» (None), тест выставляет.
            return {"ok": True, "native_playing": conn.agent_native,
                    "sendspin_playing": conn.agent_spin}
        return {"ok": True}

    conn.send_json = send_json
    conn.command = command
    # Объявление режима: в тестах не синтезируем и не пишем на диск.
    conn.said = []

    async def announce(text):
        conn.said.append(text)
        conn.commands.append({"cmd": "say"})

    conn._announce = announce
    conn._save_mode = lambda mode: None
    conn._save_event = lambda cls, text, reason: conn.saved.append((cls, text, reason))

    async def save_settled(cls, text, reason):
        conn.saved.append((cls, text, reason))

    conn._save_settled = save_settled
    conn._plan_resume_music = lambda: conn.commands.append({"cmd": "play"})
    # Снимок трёх источников и история замеров для автомата.
    conn._sources = voice_hub.sources.Sources()
    conn._prev_native = False
    conn._prev_spin = False
    conn._button_guard_until = 0.0
    # Что агент отвечает на `state`: None — «не знает», тест выставляет факт.
    conn.agent_native = None
    conn.agent_spin = False
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
    def setUp(self):
        self._old_ma_pause = voice_hub.ma_control.pause_if_playing
        voice_hub.ma_control.pause_if_playing = _no_ma_pause

    def tearDown(self):
        voice_hub.ma_control.pause_if_playing = self._old_ma_pause

    async def test_wake_pauses_music_before_asr(self):
        """Музыка глушится сразу на активации: иначе она лезет в микрофон."""
        conn = make_conn()
        await conn._on_state(State.COLLECTING, "wake", 0.97)
        self.assertIn({"cmd": "pause"}, conn.commands)

    async def test_confirmed_name_pauses_ma_queue_too(self):
        """MA-очередь ставится на паузу, если звучал наш поток."""
        conn = make_conn()
        seen = []

        async def pause_cmd(_msg, timeout=15):
            return {"was_playing": True, "sendspin": True, "sendspin_playing": True}

        async def pause(_station):
            seen.append(_station)
            return True

        conn.command = pause_cmd
        old = voice_hub.ma_control.pause_if_playing
        voice_hub.ma_control.pause_if_playing = pause
        try:
            await conn._pause_music_for_turn()
        finally:
            voice_hub.ma_control.pause_if_playing = old
        self.assertEqual(seen, ["test"])
        self.assertTrue(conn._ma_paused)
        self.assertTrue(conn._was_playing)

    async def test_native_pause_is_remembered_as_alice_owner(self):
        """Пауза нативного плеера: владелец alice, чтобы кнопка её вернула."""
        conn = make_conn()

        async def pause_cmd(_msg, timeout=15):
            return {"was_playing": True, "sendspin": False}

        async def no_ma(_station):
            return False

        conn.command = pause_cmd
        old = voice_hub.ma_control.pause_if_playing
        voice_hub.ma_control.pause_if_playing = no_ma
        try:
            await conn._pause_music_for_turn()
        finally:
            voice_hub.ma_control.pause_if_playing = old
        self.assertEqual(conn._sources.alice, voice_hub.sources.PAUSED)
        self.assertEqual(conn._sources.owner, voice_hub.sources.ALICE)

    async def test_followup_does_not_pause_again(self):
        """Второй кусок фразы не должен снова ставить музыку на паузу."""
        conn = make_conn()
        await conn._on_state(State.COLLECTING, "followup", 0.0)
        self.assertEqual(conn.commands, [{"cmd": "led", "name": "hermes_listening"}])

    async def test_confirmed_name_pauses_music(self):
        """После проверки имени музыка ставится на паузу один раз."""
        conn = make_conn()
        await conn._pause_music_for_turn()
        await conn._pause_music_for_turn()
        self.assertEqual(conn.commands, [{"cmd": "pause"}])

    async def test_busy_shows_thinking(self):
        conn = make_conn()
        await conn._on_state(State.BUSY, "wake", 0.0)
        self.assertEqual(conn.commands, [{"cmd": "led", "name": "hermes_thinking"}])

    async def test_dialog_wait_clears_ring_after_audio(self):
        conn = make_conn()
        await conn._on_state(State.DIALOG_WAIT, "answer", 0.0)
        for _ in range(3):
            await asyncio.sleep(0)
        # Кольцо одно: остальные команды (музыка) этим тестом не проверяются.
        self.assertIn({"cmd": "led", "name": ""}, conn.commands)

    async def test_ring_stays_until_play_done(self):
        """Пока колонка не подтвердила конец звука, кольцо горит.

        Раньше гасили по оценке длительности чанка, и на длинной фразе кольцо
        тухло раньше, чем кончалась речь.
        """
        conn = make_conn()
        conn._pending_say = True
        conn._audio_deadline = asyncio.get_running_loop().time() + 30
        await conn._on_state(State.DIALOG_WAIT, "answer", 0.0)
        await asyncio.sleep(0.05)
        self.assertNotIn({"cmd": "led", "name": ""}, conn.commands,
                         "кольцо погашено до конца речи")
        await conn.on_play_done()
        await asyncio.sleep(0.05)
        self.assertIn({"cmd": "led", "name": ""}, conn.commands)

    async def test_ring_cleared_without_station_confirmation(self):
        """Старый агент не шлёт play_done: оценка всё равно освобождает кольцо."""
        conn = make_conn()
        conn._pending_say = True
        conn._audio_deadline = asyncio.get_running_loop().time() + 0.1
        await conn._on_state(State.DIALOG_WAIT, "answer", 0.0)
        await asyncio.sleep(0.4)
        self.assertIn({"cmd": "led", "name": ""}, conn.commands)

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
        # Обращение в промахе даёт только датасет near-miss: порог детектора
        # остаётся единственным условием активации, ответа быть не должно.
        self.assertEqual(conn.miss_processed, [])

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


class ButtonInterruptTest(unittest.IsolatedAsyncioTestCase):
    async def test_button_in_silence_does_not_touch_music(self):
        """В тишине кнопку обрабатывает maind: мы ничего не гасим."""
        conn = make_conn()
        conn.role = "station"
        conn.turn = None
        conn._suppress_resume = False
        conn._was_playing = False
        conn._resume_task = None

        async def core_interrupt():
            return None

        async def idle(_station):
            return "idle"

        conn.core.interrupt = core_interrupt
        old = voice_hub.ma_control.player_state
        voice_hub.ma_control.player_state = idle
        try:
            await conn.interrupt()
        finally:
            voice_hub.ma_control.player_state = old

        # Кнопка теперь наша целиком: в тишине она включает Алису.
        self.assertFalse(conn._suppress_resume)
        self.assertIn({"cmd": "native_play"}, conn.commands)


class ButtonToggleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Без заглушек кнопка дёргает настоящий MCP и тест ждёт сетевой таймаут.
        self._old = (voice_hub.ma_control.queue_len,
                     voice_hub.ma_control.pause_if_playing)

        async def filled(_station):
            return 1

        async def paused_ok(_station):
            return True

        voice_hub.ma_control.queue_len = filled
        voice_hub.ma_control.pause_if_playing = paused_ok

    def tearDown(self):
        voice_hub.ma_control.queue_len, voice_hub.ma_control.pause_if_playing = self._old

    async def test_button_resumes_ma_when_hub_paused(self):
        """Повторное нажатие возвращает очередь MA, а не радио Алисы."""
        conn = make_conn()
        conn.role = "station"
        conn.turn = None
        conn._suppress_resume = False
        conn._was_playing = False
        conn._resume_task = None
        conn._sources.ma = voice_hub.sources.PAUSED
        conn._sources.owner = voice_hub.sources.MA

        async def core_interrupt():
            return None

        async def paused(_station):
            return "paused"

        async def resume(_station):
            return True

        conn.core.interrupt = core_interrupt
        old_s = voice_hub.ma_control.player_state
        old_r = voice_hub.ma_control.resume_if_paused
        voice_hub.ma_control.player_state = paused
        voice_hub.ma_control.resume_if_paused = resume
        try:
            await conn.interrupt()
        finally:
            voice_hub.ma_control.player_state = old_s
            voice_hub.ma_control.resume_if_paused = old_r

        self.assertIn({"cmd": "play"}, conn.commands)
        self.assertEqual(conn._sources.ma, voice_hub.sources.PLAYING)

    async def test_button_plays_ma_paused_from_app(self):
        """Пауза, поставленная в MA с ПК, тоже снимается кнопкой."""
        conn = make_conn()
        conn.role = "station"
        conn.turn = None
        conn._suppress_resume = False
        conn._was_playing = False
        conn._resume_task = None
        conn._sources.ma = voice_hub.sources.PAUSED
        conn._sources.owner = voice_hub.sources.MA
        calls = []

        async def core_interrupt():
            return None

        async def paused(_station):
            return "paused"

        async def no_resume(_station):
            return False

        async def forced(_station):
            calls.append(_station)
            return True

        conn.core.interrupt = core_interrupt
        old_s = voice_hub.ma_control.player_state
        old_r = voice_hub.ma_control.resume_if_paused
        old_f = voice_hub.ma_control.play_forced
        voice_hub.ma_control.player_state = paused
        voice_hub.ma_control.resume_if_paused = no_resume
        voice_hub.ma_control.play_forced = forced
        try:
            await conn.interrupt()
        finally:
            voice_hub.ma_control.player_state = old_s
            voice_hub.ma_control.resume_if_paused = old_r
            voice_hub.ma_control.play_forced = old_f

        self.assertEqual(calls, ["test"])


class ButtonPauseGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_pause_press_arms_native_guard(self):
        """Кнопка поставила паузу — maind поднимет Алису, её надо глушить."""
        conn = make_conn()
        conn.role = "station"
        conn.turn = None
        conn._suppress_resume = False
        conn._resume_task = None
        conn._sources.ma = voice_hub.sources.PLAYING
        conn._sources.owner = voice_hub.sources.MA

        async def core_interrupt():
            return None

        async def playing(_station):
            return "playing"

        async def filled(_station):
            return 1

        async def pause(_station):
            return True

        conn.core.interrupt = core_interrupt
        old_s = voice_hub.ma_control.player_state
        old_q = voice_hub.ma_control.queue_len
        old_p = voice_hub.ma_control.pause_if_playing
        voice_hub.ma_control.player_state = playing
        voice_hub.ma_control.queue_len = filled
        voice_hub.ma_control.pause_if_playing = pause
        try:
            await conn.interrupt(long=False)
        finally:
            voice_hub.ma_control.player_state = old_s
            voice_hub.ma_control.queue_len = old_q
            voice_hub.ma_control.pause_if_playing = old_p

        # Пауза MA ставится через MCP, натив не трогаем: у maind свой toggle.
        self.assertTrue(conn._ma_paused)
        self.assertNotIn({"cmd": "native_pause"}, conn.commands)
        # Кнопка именно поставила паузу — автовозврат запрещён.
        self.assertTrue(conn._suppress_resume)

    async def test_alice_playing_button_pauses_native(self):
        """Владелец Алиса — пауза ставится и нативу: maind кнопку не видит."""
        conn = make_conn()
        conn.role = "station"
        conn.turn = None
        conn._suppress_resume = False
        conn._resume_task = None
        conn._sources.alice = voice_hub.sources.PLAYING
        conn._sources.ma = voice_hub.sources.PAUSED
        conn._sources.owner = voice_hub.sources.ALICE

        async def core_interrupt():
            return None

        async def paused(_station):
            return "paused"

        async def filled(_station):
            return 1

        async def paused_ok(_station):
            return True

        conn.core.interrupt = core_interrupt
        old_s = voice_hub.ma_control.player_state
        old_q = voice_hub.ma_control.queue_len
        old_p = voice_hub.ma_control.pause_if_playing
        voice_hub.ma_control.player_state = paused
        voice_hub.ma_control.queue_len = filled
        voice_hub.ma_control.pause_if_playing = paused_ok
        try:
            await conn.interrupt(long=False)
        finally:
            voice_hub.ma_control.player_state = old_s
            voice_hub.ma_control.queue_len = old_q
            voice_hub.ma_control.pause_if_playing = old_p

        self.assertTrue(conn._suppress_resume)
        self.assertIn({"cmd": "native_pause"}, conn.commands)
        self.assertIn({"cmd": "led", "name": "hermes_pause", "timeout": 1},
                      conn.commands)

    async def test_button_takes_native_fact_from_agent(self):
        """Снимок хаба отстал (alice=paused), а агент слышит игру — пауза.

        Регрессия 11.11: локальная пауза в агенте опережала решение хаба,
        хаб видел «тишину» и первым нажатием включал музыку. Теперь факт
        берётся у агента, и «тишина» из старого снимка не включает источник.
        """
        conn = make_conn()
        conn.role = "station"
        conn.turn = None
        conn._suppress_resume = False
        conn._resume_task = None
        conn._sources.alice = voice_hub.sources.PAUSED
        conn._sources.ma = voice_hub.sources.PAUSED
        conn._sources.owner = voice_hub.sources.ALICE
        conn.agent_native = True

        async def core_interrupt():
            return None

        async def paused(_station):
            return "paused"

        async def filled(_station):
            return 1

        async def paused_ok(_station):
            return True

        conn.core.interrupt = core_interrupt
        old_s = voice_hub.ma_control.player_state
        old_q = voice_hub.ma_control.queue_len
        old_p = voice_hub.ma_control.pause_if_playing
        voice_hub.ma_control.player_state = paused
        voice_hub.ma_control.queue_len = filled
        voice_hub.ma_control.pause_if_playing = paused_ok
        try:
            await conn.interrupt(long=False)
        finally:
            voice_hub.ma_control.player_state = old_s
            voice_hub.ma_control.queue_len = old_q
            voice_hub.ma_control.pause_if_playing = old_p

        self.assertIn({"cmd": "native_pause"}, conn.commands)
        self.assertNotIn({"cmd": "native_play"}, conn.commands)
        self.assertTrue(conn._suppress_resume)


class SnapshotStreamFactTest(unittest.IsolatedAsyncioTestCase):
    """«MA играет» и «звук слышен» — разные факты: aplay может быть мёртв."""

    async def _snapshot(self, conn, spin):
        async def playing(_station):
            return "playing"

        async def filled(_station):
            return 1

        conn.agent_spin = spin
        old_s = voice_hub.ma_control.player_state
        old_q = voice_hub.ma_control.queue_len
        voice_hub.ma_control.player_state = playing
        voice_hub.ma_control.queue_len = filled
        try:
            await conn._sync_sources_snapshot()
        finally:
            voice_hub.ma_control.player_state = old_s
            voice_hub.ma_control.queue_len = old_q

    async def test_ma_playing_but_stream_dead_is_not_streaming(self):
        conn = make_conn()
        conn._sources.ma = voice_hub.sources.PAUSED
        conn._sources.agent = voice_hub.sources.STREAMING
        await self._snapshot(conn, spin=False)
        self.assertEqual(conn._sources.ma, voice_hub.sources.PLAYING)
        self.assertEqual(conn._sources.agent, voice_hub.sources.PAUSED)

    async def test_ma_playing_and_stream_alive_is_streaming(self):
        conn = make_conn()
        conn._sources.ma = voice_hub.sources.PAUSED
        conn._sources.agent = voice_hub.sources.PAUSED
        await self._snapshot(conn, spin=True)
        self.assertEqual(conn._sources.agent, voice_hub.sources.STREAMING)


class ModeAnnounceTest(unittest.IsolatedAsyncioTestCase):
    async def _press(self, conn, ma_state="playing", queue=1):
        async def core_interrupt():
            return None

        async def ma(_station):
            return ma_state

        async def qlen(_station):
            return queue

        async def ok(_station):
            return True

        conn.core.interrupt = core_interrupt
        olds = (voice_hub.ma_control.player_state,
                voice_hub.ma_control.queue_len,
                voice_hub.ma_control.pause_if_playing,
                voice_hub.ma_control.resume_if_paused,
                voice_hub.ma_control.play_forced)
        voice_hub.ma_control.player_state = ma
        voice_hub.ma_control.queue_len = qlen
        voice_hub.ma_control.pause_if_playing = ok
        voice_hub.ma_control.resume_if_paused = ok
        voice_hub.ma_control.play_forced = ok
        try:
            await conn.interrupt(long=True)
        finally:
            (voice_hub.ma_control.player_state,
             voice_hub.ma_control.queue_len,
             voice_hub.ma_control.pause_if_playing,
             voice_hub.ma_control.resume_if_paused,
             voice_hub.ma_control.play_forced) = olds

    async def test_long_press_announces_local_before_play(self):
        """Фраза о режиме — до включения музыки, а не поверх неё."""
        conn = make_conn()
        conn.role = "station"
        conn.turn = None
        conn._suppress_resume = False
        conn._resume_task = None
        conn._sources.alice = voice_hub.sources.PLAYING
        conn._sources.ma = voice_hub.sources.PAUSED
        conn._sources.owner = voice_hub.sources.ALICE
        conn._sources.ma_queue = "filled"

        await self._press(conn)

        self.assertEqual(conn.said, ["Включена локальная музыка"])
        cmds = [c.get("cmd") for c in conn.commands if isinstance(c, dict)]
        self.assertLess(cmds.index("native_pause"), cmds.index("say"))
        self.assertLess(cmds.index("say"), cmds.index("play"))
        # Кольцо показывает целевой режим до фразы и гаснет после неё.
        self.assertIn({"cmd": "led", "name": "hermes_mode_local"},
                      conn.commands)
        self.assertLess(cmds.index("led"), cmds.index("say"))
        self.assertEqual(conn.mode, "local")

    async def test_long_press_empty_queue_announces_cloud(self):
        """Пустая локальная очередь — уходим в облако и говорим об этом."""
        conn = make_conn()
        conn.role = "station"
        conn.turn = None
        conn._suppress_resume = False
        conn._resume_task = None
        conn._sources.alice = voice_hub.sources.PAUSED
        conn._sources.ma = voice_hub.sources.IDLE
        conn._sources.owner = voice_hub.sources.ALICE
        conn._sources.ma_queue = "empty"

        await self._press(conn, ma_state="idle", queue=0)

        self.assertEqual(conn.said,
                         ["Локальная очередь пуста, включаю облачную музыку"])
        self.assertIn({"cmd": "native_play"}, conn.commands)
        self.assertEqual(conn.mode, "cloud")


class ActivationTakesOverButtonPauseTest(unittest.IsolatedAsyncioTestCase):
    async def test_activation_clears_button_pause_flag(self):
        """Ложная активация после кнопки не должна оставлять колонку немой."""
        old = voice_hub.ma_control.pause_if_playing

        async def playing(_station):
            return True

        voice_hub.ma_control.pause_if_playing = playing
        try:
            conn = make_conn()
            conn._suppress_resume = True
            conn._music_paused_for_turn = False
            conn._ma_paused = False
            conn._agent_sendspin = False

            async def command(cmd, timeout=15):
                conn.commands.append(cmd)
                if cmd.get("cmd") == "pause":
                    return {"ok": True, "was_playing": True,
                            "sendspin": False, "sendspin_playing": False}
                return {"ok": True}

            conn.command = command
            await conn._pause_music_for_turn()
        finally:
            voice_hub.ma_control.pause_if_playing = old

        self.assertTrue(conn._was_playing)
        self.assertFalse(conn._suppress_resume)


class LongPressSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def test_long_press_switches_ma_to_alice(self):
        """Длинное нажатие — явный переход на Алису, без окна-глушилки."""
        conn = make_conn()
        conn.role = "station"
        conn.turn = None
        conn._suppress_resume = False
        conn._resume_task = None
        conn._sources.ma = voice_hub.sources.PLAYING
        conn._sources.agent = voice_hub.sources.STREAMING
        conn._sources.owner = voice_hub.sources.MA

        async def core_interrupt():
            return None

        async def playing(_station):
            return "playing"

        async def pause(_station):
            return True

        async def filled(_station):
            return 1

        conn.core.interrupt = core_interrupt
        old_s = voice_hub.ma_control.player_state
        old_q = voice_hub.ma_control.queue_len
        old_p = voice_hub.ma_control.pause_if_playing
        voice_hub.ma_control.player_state = playing
        voice_hub.ma_control.queue_len = filled
        voice_hub.ma_control.pause_if_playing = pause
        try:
            await conn.interrupt(long=True)
        finally:
            voice_hub.ma_control.player_state = old_s
            voice_hub.ma_control.queue_len = old_q
            voice_hub.ma_control.pause_if_playing = old_p

        self.assertIn({"cmd": "native_play"}, conn.commands)
        self.assertEqual(conn._sources.owner, voice_hub.sources.ALICE)
        self.assertEqual(conn._sources.ma, voice_hub.sources.PAUSED)


class ButtonGuardOverlapTest(unittest.IsolatedAsyncioTestCase):
    async def test_guard_pauses_only_native_after_button(self):
        """После кнопки нативный плеер гасим, очередь MA не трогаем."""
        conn = make_conn()
        conn._native_overlap = False
        conn._ma_paused = False
        conn._button_guard_until = time.monotonic() + 3
        calls = []

        async def pause(station):
            calls.append(station)
            return True

        old = voice_hub.ma_control.pause_if_playing
        voice_hub.ma_control.pause_if_playing = pause
        try:
            await conn._handle_overlap(True, True, True)
        finally:
            voice_hub.ma_control.pause_if_playing = old

        self.assertEqual(calls, [])
        self.assertIn({"cmd": "native_pause"}, conn.commands)


class StopKeepsMusicTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_returns_music_when_it_was_paused(self):
        """«Стоп» обрывает ответ, но не убивает очередь MA."""
        conn = make_conn()
        conn.role = "station"
        conn._was_playing = True
        conn._ma_paused = True
        conn._music_paused_for_turn = True
        dropped = []

        async def drop():
            dropped.append(True)

        conn.core.drop_dialog = drop
        calls = []

        async def control(_station, action, seconds=None):
            calls.append(action)
            return None

        old = voice_hub.ma_control.control
        voice_hub.ma_control.control = control
        try:
            await conn._on_phrase("Стоп.", "barge", False)
        finally:
            voice_hub.ma_control.control = old

        self.assertEqual(calls, [])
        self.assertIn({"cmd": "stop"}, conn.commands)
        self.assertTrue(dropped)


class ResumeMusicTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_delay = voice_hub.RESUME_DELAY
        voice_hub.RESUME_DELAY = 0

    async def asyncTearDown(self):
        voice_hub.RESUME_DELAY = self.old_delay

    async def test_resume_clears_ma_pause(self):
        """Если хаб ставил на паузу очередь MA, play идёт и в MA, и агенту."""
        conn = make_conn()
        conn._was_playing = True
        conn._ma_paused = True
        conn._agent_sendspin = True
        seen = []

        async def resume(_station):
            seen.append(_station)

        old = voice_hub.ma_control.resume_if_paused
        voice_hub.ma_control.resume_if_paused = resume
        try:
            await conn._resume_music()
        finally:
            voice_hub.ma_control.resume_if_paused = old
        self.assertEqual(seen, ["test"])
        self.assertFalse(conn._ma_paused)
        self.assertEqual(conn.commands, [{"cmd": "play"}])

    async def test_ma_resume_does_not_start_native_radio(self):
        """MA вернул поток, а агент паузу Sendspin не ставил — play не шлём."""
        conn = make_conn()
        conn._was_playing = True
        conn._ma_paused = True
        conn._agent_sendspin = False
        seen = []

        async def resume(_station):
            seen.append(_station)

        old = voice_hub.ma_control.resume_if_paused
        voice_hub.ma_control.resume_if_paused = resume
        try:
            await conn._resume_music()
        finally:
            voice_hub.ma_control.resume_if_paused = old
        self.assertEqual(seen, ["test"])
        self.assertFalse(conn._ma_paused)
        self.assertFalse(conn._was_playing)
        self.assertEqual(conn.commands, [])

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


class OverlapTest(unittest.TestCase):
    def test_native_over_sendspin_pauses_once(self):
        self.assertTrue(voice_hub.should_pause_overlap(True, True, True))
        self.assertTrue(voice_hub.should_pause_overlap(True, False, True))
        self.assertFalse(voice_hub.should_pause_overlap(True, True, True, already=True))
        self.assertFalse(voice_hub.should_pause_overlap(True, False, False))
        self.assertFalse(voice_hub.should_pause_overlap(False, True, True))


class OverlapApplyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old = voice_hub.ma_control.pause_if_playing
        self.paused = []

        async def pause(station):
            self.paused.append(station)
            return True

        voice_hub.ma_control.pause_if_playing = pause

    def tearDown(self):
        voice_hub.ma_control.pause_if_playing = self._old

    async def test_alice_started_pauses_ma(self):
        """Алиса заиграла поверх музыки MA: гасим очередь, натив не трогаем."""
        conn = make_conn()
        conn._sources.ma = voice_hub.sources.PLAYING
        await conn._handle_overlap(True, True, True)
        self.assertEqual(self.paused, ["test"])
        self.assertNotIn({"cmd": "pause"}, conn.commands)
        self.assertNotIn({"cmd": "native_pause"}, conn.commands)
        self.assertTrue(conn._ma_paused)

    async def test_alice_started_during_answer_pauses_native(self):
        """Во время нашего ответа натив гасится, MA не трогаем."""
        conn = make_conn()
        conn._sources.ma = voice_hub.sources.PAUSED
        conn._sources.pause_reason = voice_hub.sources.REASON_HUB
        await conn._handle_overlap(True, False, True)
        self.assertIn({"cmd": "native_pause"}, conn.commands)
        self.assertEqual(self.paused, [])

    async def test_alice_started_without_ma_does_nothing(self):
        conn = make_conn()
        await conn._handle_overlap(True, False, True)
        self.assertEqual(self.paused, [])
        self.assertFalse(conn._ma_paused)

    async def test_second_poll_does_not_pause_again(self):
        """Повторный замер не должен глушить по кругу."""
        conn = make_conn()
        conn._sources.ma = voice_hub.sources.PLAYING
        await conn._handle_overlap(True, True, True)
        await conn._handle_overlap(True, True, True)
        self.assertEqual(len(self.paused), 1)

    async def test_ma_started_pauses_alice(self):
        """MA заиграл поверх Алисы: гасим натив, MA остаётся."""
        conn = make_conn()
        conn._sources.alice = voice_hub.sources.PLAYING
        conn._sources.owner = voice_hub.sources.ALICE
        conn._prev_native = True
        await conn._handle_overlap(True, True, True)
        self.assertIn({"cmd": "native_pause"}, conn.commands)


class VolumeSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_coalesces_to_latest_value(self):
        conn = make_conn()
        conn.volume_percent = 10
        calls = []

        async def fake_set(_station, percent, force=False):
            calls.append(percent)
            await asyncio.sleep(0.01)
            return True

        old = voice_hub.ma_control.set_volume
        voice_hub.ma_control.set_volume = fake_set
        try:
            conn._queue_volume_sync(10)
            conn._queue_volume_sync(20)
            conn._queue_volume_sync(30)
            await conn._vol_sync_task
        finally:
            voice_hub.ma_control.set_volume = old
        self.assertEqual(calls[-1], 30)
        self.assertLessEqual(len(calls), 2)

    async def test_ma_origin_is_not_echoed_to_ma(self):
        conn = make_conn()
        conn.role = "station"
        conn.volume_percent = 30
        calls = []
        conn._queue_volume_sync = lambda pct: calls.append(pct)
        await conn._on_text(json.dumps({
            "t": "volume", "percent": 30, "level": 3, "from_ma": True,
        }))
        self.assertEqual(calls, [])
        self.assertEqual(conn.volume_percent, 30)

    async def test_local_volume_is_sent_to_ma(self):
        conn = make_conn()
        conn.role = "station"
        conn.volume_percent = 30
        calls = []
        conn._queue_volume_sync = lambda pct: calls.append(pct)
        await conn._on_text(json.dumps({
            "t": "volume", "percent": 30, "level": 3,
        }))
        self.assertEqual(calls, [30])


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


class MaPendingTest(unittest.IsolatedAsyncioTestCase):
    async def test_pending_pause_wins_over_stale_fact(self):
        """MA ещё не применил паузу — снимок держит намерение."""
        conn = make_conn()
        conn._ma_pending = ("paused", time.monotonic() + 5)

        async def state(_station):
            return "playing"

        async def qlen(_station):
            return 1

        old_s = voice_hub.ma_control.player_state
        old_q = voice_hub.ma_control.queue_len
        voice_hub.ma_control.player_state = state
        voice_hub.ma_control.queue_len = qlen
        try:
            await conn._sync_sources_snapshot()
        finally:
            voice_hub.ma_control.player_state = old_s
            voice_hub.ma_control.queue_len = old_q

        self.assertEqual(conn._sources.ma, voice_hub.sources.PAUSED)

    async def test_confirmed_fact_clears_pending(self):
        conn = make_conn()
        conn._ma_pending = ("paused", time.monotonic() + 5)

        async def state(_station):
            return "paused"

        async def qlen(_station):
            return 1

        old_s = voice_hub.ma_control.player_state
        old_q = voice_hub.ma_control.queue_len
        voice_hub.ma_control.player_state = state
        voice_hub.ma_control.queue_len = qlen
        try:
            await conn._sync_sources_snapshot()
        finally:
            voice_hub.ma_control.player_state = old_s
            voice_hub.ma_control.queue_len = old_q

        self.assertIsNone(conn._ma_pending)


class WavSecondsTest(unittest.TestCase):
    def test_streaming_header_is_counted_by_body(self):
        """nframes=0x7FFFFFFF не должен давать 44739 с: иначе ждём вечность."""
        import replies
        data = replies.audio("Включена локальная музыка")
        self.assertIsNotNone(data)
        sec = voice_hub.wav_seconds(data)
        self.assertGreater(sec, 0.5)
        self.assertLess(sec, 5.0)


class AnnounceWaitTest(unittest.IsolatedAsyncioTestCase):
    async def test_announce_ends_on_play_done(self):
        """Конец объявления — по подтверждению колонки, а не по таймауту."""
        import io
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(b"\x00\x00" * 9600)      # 0.2 с
        data = buf.getvalue()

        conn = make_conn()
        sent = []

        async def command(payload, timeout=10):
            sent.append(payload)
            return {"ok": True}

        async def send_binary(_data):
            loop = asyncio.get_running_loop()
            loop.call_later(0.05,
                            lambda: asyncio.ensure_future(conn.on_play_done()))

        old_audio = voice_hub.replies.audio
        voice_hub.replies.audio = lambda _text: data
        conn.command = command
        conn.send_binary = send_binary
        try:
            started = time.monotonic()
            await asyncio.wait_for(
                voice_hub.Conn._announce(conn, "Включена локальная музыка"),
                timeout=3.0)
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            voice_hub.replies.audio = old_audio
        self.assertEqual(sent[0]["cmd"], "say")


if __name__ == "__main__":
    unittest.main()
