"""Перебивание колонки голосом: ключ во время ответа агента.

Правила:
1. В состоянии BUSY срабатывание детектора — это перебивание, а не пропуск.
2. Ядро отменяет текущий ход и сразу начинает сбор новой фразы (origin=barge).
3. Хаб глушит уже отправленный звук командой `stop`, музыку не возвращает.
4. `play_done` от оборванного ответа не гасит кольцо нового хода.
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_core  # noqa: E402
import voice_hub  # noqa: E402


class FakeWake:
    def __init__(self, hits=None):
        self.hits = hits or []
        self.resets = 0
        self.last_max = 0.0

    async def feed(self, pcm):
        return list(self.hits)

    def reset(self):
        self.resets += 1


def make_core(state=voice_core.State.BUSY, barge=True, hits=None):
    core = voice_core.SpeechCore.__new__(voice_core.SpeechCore)
    core.cfg = voice_core.CoreConfig(smart_turn=False, barge_in=barge)
    core.state = state
    core.origin = None
    core.prefix = b""
    core.pre = bytearray(b"\x00\x00" * 3200)
    core.pre_bytes = 32000
    core._task = None
    core._barging = False
    core._closed = False
    core.dialog_active = False
    core.dialog_until = 0.0
    core.dialog_collect_done = False
    core.follow_up = False
    core.coll = None
    core.smart_turn = None
    core.last_rms = 21.0
    core.wake_rms = 0.0
    core.last_score = 0.0
    core.wake = FakeWake(hits=hits)
    core.level = voice_core.DetectorLevel(gain=1.0)
    core.output_busy = lambda: False
    core.states = []
    core.barges = 0

    async def on_state(new_state, origin=None, score=None):
        core.states.append((new_state, origin))

    async def on_phrase(text, origin, had_wake):
        return None

    async def on_barge():
        core.barges += 1

    core.on_state = on_state
    core.on_phrase = on_phrase
    core.on_barge = on_barge
    return core


class CoreBargeTest(unittest.TestCase):
    def test_barge_starts_new_phrase_instead_of_ignoring_key(self):
        core = make_core(hits=[(0.4, 0.97)], state=voice_core.State.COLLECTING)

        async def run():
            # BUSY — ответ агента идёт: ключ должен перебивать.
            core.state = voice_core.State.BUSY
            await core.feed(b"\x00\x00" * 512, b"\x00\x00" * 512)

        asyncio.run(run())
        self.assertEqual(core.barges, 1)
        self.assertEqual(core.state, voice_core.State.COLLECTING)
        self.assertEqual(core.origin, "barge")
        self.assertEqual(core.wake.resets, 1)
        self.assertTrue(any(o == "barge" for _, o in core.states))

    def test_barge_disabled_keeps_old_behaviour(self):
        core = make_core(barge=False)
        core.state = voice_core.State.BUSY

        async def run():
            await core.feed(b"\x00\x00" * 512, b"\x00\x00" * 512)

        asyncio.run(run())
        self.assertEqual(core.barges, 0)
        self.assertEqual(core.state, voice_core.State.BUSY)

    def test_barge_cancels_running_task(self):
        core = make_core()

        async def forever():
            await asyncio.sleep(30)

        async def run():
            core._task = asyncio.create_task(forever())
            await asyncio.sleep(0)
            await core.barge_in((0.4, 0.96))
            await asyncio.sleep(0)
            return core._task

        self.assertIsNone(asyncio.run(run()))

    def test_idle_activation_is_not_barge(self):
        core = make_core(hits=[(0.4, 0.96)])
        core.state = voice_core.State.IDLE
        # _activate создаёт сборщик и зовёт _set_state — этого достаточно,
        # чтобы отличить обычную активацию от перебивания.
        asyncio.run(core.feed(b"\x00\x00" * 512, b"\x00\x00" * 512))
        self.assertEqual(core.barges, 0)
        self.assertEqual(core.origin, "wake")


def make_conn(role="station"):
    conn = voice_hub.Conn.__new__(voice_hub.Conn)
    conn.name = "lite-1"
    conn.role = role
    conn.sent = []
    conn._led_task = None
    conn._sending_audio = True
    conn._dialog_hold = True
    conn._audio_deadline = 123.0
    conn._play_done = False
    conn._pending_say = True
    conn._barge_guard_until = 0.0
    conn.finished = 0
    conn.turn = None
    conn._sources = voice_hub.sources.Sources()
    conn._prev_native = False
    conn._prev_spin = False
    conn._button_guard_until = 0.0

    async def command(payload, timeout=120.0):
        conn.sent.append(payload)
        return {"ok": True}

    async def pause_music():
        conn.sent.append({"cmd": "pause"})
        return None

    conn._pause_music_for_turn = pause_music

    async def finish():
        conn.finished += 1

    conn.command = command
    conn._finish_speech = finish
    return conn


class HubBargeTest(unittest.TestCase):
    def test_barge_stops_audio_and_keeps_music_paused(self):
        conn = make_conn()
        asyncio.run(conn._on_barge())
        self.assertEqual(conn.sent, [{"cmd": "stop"}])
        self.assertFalse(conn._sending_audio)
        self.assertFalse(conn._dialog_hold)
        self.assertEqual(conn._audio_deadline, 0.0)
        self.assertGreater(conn._barge_guard_until, 0.0)

    def test_panel_gets_no_device_command(self):
        conn = make_conn(role="panel")
        asyncio.run(conn._on_barge())
        self.assertEqual(conn.sent, [])

    def test_play_done_after_barge_is_ignored(self):
        conn = make_conn()
        asyncio.run(conn._on_barge())
        conn._sending_audio = False
        asyncio.run(conn.on_play_done())
        self.assertEqual(conn.finished, 0)
        self.assertFalse(conn._play_done)

    def test_play_done_without_barge_finishes_speech(self):
        conn = make_conn()
        conn._sending_audio = False
        conn._barge_guard_until = 0.0
        asyncio.run(conn.on_play_done())
        self.assertEqual(conn.finished, 1)
        self.assertTrue(conn._play_done)


class FastRouteBargeTest(unittest.TestCase):
    """В перебивании быстрый дом не участвует: только агент."""

    def test_barge_skips_fast_route(self):
        import fast_route

        calls = []

        class FakeRoute:
            last_kind = "command"

            async def route(self, text, station=None):
                calls.append(text)
                return "команда выполнена"

        async def fake_stream(text, on_event=None):
            yield "агент"

        saved = (fast_route._route, fast_route._failed, fast_route.hermes_ws.stream)
        try:
            fast_route._route = FakeRoute()
            fast_route._failed = False
            fast_route.hermes_ws.stream = fake_stream

            async def run(origin):
                fast_route.set_origin(origin)
                return [p async for p in fast_route.stream("включи лампу")]

            barge = asyncio.run(run("barge"))
            wake = asyncio.run(run("wake"))
        finally:
            fast_route._route, fast_route._failed, fast_route.hermes_ws.stream = saved
            fast_route.set_origin(None)
        self.assertEqual(barge, ["агент"])
        # wake идёт через быстрый дом; команда выполняется молча (SILENT).
        import voice_dialog
        self.assertEqual(wake, [voice_dialog.SILENT])
        self.assertEqual(calls, ["включи лампу"])


class DialogWaitBargeTest(unittest.TestCase):
    """Ответ синтезирован, окно открыто, но звук ещё играет — это barge."""

    def test_key_while_audio_plays_starts_barge(self):
        core = make_core(hits=[(0.4, 0.98)], state=voice_core.State.DIALOG_WAIT)
        core.dialog_active = True
        core.dialog_until = voice_core.time.time() + 10
        core.output_busy = lambda: True
        asyncio.run(core.feed(b"\x00\x00" * 512, b"\x00\x00" * 512))
        self.assertEqual(core.barges, 1)
        self.assertEqual(core.state, voice_core.State.COLLECTING)
        self.assertFalse(core.dialog_active)

    def test_key_after_audio_closes_window_without_barge(self):
        core = make_core(hits=[(0.4, 0.98)], state=voice_core.State.DIALOG_WAIT)
        core.dialog_active = True
        core.dialog_until = voice_core.time.time() + 10
        core.output_busy = lambda: False
        asyncio.run(core.feed(b"\x00\x00" * 512, b"\x00\x00" * 512))
        self.assertEqual(core.barges, 0)
        self.assertEqual(core.origin, "wake")
        self.assertFalse(core.dialog_active)
        self.assertEqual(core.state, voice_core.State.COLLECTING)


class StopCommandTest(unittest.TestCase):
    """«Стоп» обрывает и молчит: в LLM такая фраза не уходит."""

    def test_stop_phrases(self):
        import voice_dialog
        for text in ("Гермес, стоп.", "Стоп!", "О'кей, герместоп.",
                     "Гермес, хватит", "стоп, пожалуйста", "тихо", "замолчи",
                     "О'кей, Germes stop.", "Dermest stop.", "stop",
                     "Ок гермес стоп", "Стоп, блядь. Стоп. Стоп."):
            self.assertTrue(voice_dialog.is_stop_command(text), text)

    def test_not_stop_phrases(self):
        import voice_dialog
        for text in ("включи свет", "стоп, а который час?",
                     "включи свет, стоп", "я сказал стоп",
                     "Очень гермест.", "расскажи анекдот"):
            self.assertFalse(voice_dialog.is_stop_command(text), text)

    def test_stop_does_not_reach_llm(self):
        conn = make_conn()
        conn.speaks = []
        conn.dropped = 0

        async def speak(text, origin=None):
            conn.speaks.append(text)

        async def drop_dialog():
            conn.dropped += 1

        async def save_settled(*a, **kw):
            return None

        conn._speak = speak
        conn._save_settled = save_settled
        conn.core = SimpleNamespace(drop_dialog=drop_dialog)
        asyncio.run(conn._on_phrase("Ок гермес стоп", "barge", False))
        self.assertEqual(conn.speaks, [])
        self.assertEqual(conn.dropped, 1)

    def test_drop_dialog_closes_window(self):
        core = make_core(state=voice_core.State.DIALOG_WAIT)
        core.dialog_active = True
        core.dialog_until = voice_core.time.time() + 10
        asyncio.run(core.drop_dialog())
        self.assertFalse(core.dialog_active)
        self.assertEqual(core.state, voice_core.State.IDLE)


class BargeDialogTest(unittest.TestCase):
    """После перебивания диалог идёт без быстрого дома."""

    def test_followup_after_barge_avoids_fast_route(self):
        conn = make_conn()
        asyncio.run(conn._on_barge())
        self.assertEqual(conn._route_origin("followup"), "barge")
        self.assertEqual(conn._route_origin("dialog"), "barge")
        self.assertEqual(conn._route_origin(None), "barge")
        # Новое обращение по ключу — новый диалог, быстрый дом снова работает.
        self.assertEqual(conn._route_origin("wake"), "wake")

    def test_barge_flag_resets_when_window_closes(self):
        conn = make_conn()
        asyncio.run(conn._on_barge())

        async def send_json(obj):
            return None

        conn.send_json = send_json
        conn.core = SimpleNamespace(follow_up=True)
        conn._clear_led_after_audio = lambda: None

        async def run():
            await conn._on_state(voice_core.State.IDLE, "barge", None)
            inside = conn._route_origin("followup")
            await conn._on_state(voice_core.State.IDLE, None, None)
            return inside, conn._route_origin("followup")

        inside, after = asyncio.run(run())
        self.assertEqual(inside, "barge")
        self.assertEqual(after, "followup")


class WakeGlueTest(unittest.TestCase):
    """«Гермес, стоп» ASR отдаёт слитно — это не «только обращение»."""

    def test_glued_stop_is_not_just_wake(self):
        import voice_dialog
        self.assertFalse(voice_dialog.is_just_wake("О'кей, герместоп."))
        self.assertTrue(voice_dialog.strip_wake("О'кей, герместоп."))

    def test_plain_wake_still_counts(self):
        import voice_dialog
        self.assertTrue(voice_dialog.is_just_wake("Окей, Гермес."))
        self.assertEqual(voice_dialog.strip_wake("Окей, Гермес."), "")

    def test_normal_phrase_is_untouched(self):
        import voice_dialog
        self.assertEqual(voice_dialog.strip_wake("Окей, Гермес, спокойной ночи"),
                         "спокойной ночи")


if __name__ == "__main__":
    unittest.main()
