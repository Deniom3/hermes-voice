"""Правила активации: повтор пользователя не теряется.

1. Промах с подтверждённым обращением обрабатывается как команда.
2. Если ключ звучал до срабатывания, снимок кольца уходит в ASR целиком.
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_core  # noqa: E402
import voice_hub  # noqa: E402


def make_core(state=voice_core.State.IDLE):
    core = voice_core.SpeechCore.__new__(voice_core.SpeechCore)
    core.state = state
    core.dialog_active = False
    core.output_busy = lambda: False
    core.last_rms = 21.0
    core.dialog_until = 5.0
    core.origin = None
    core.wake_rms = 0.0
    core.seen = []

    async def set_state(new_state, origin=None, score=None):
        core.state = new_state

    async def recognize(pcm, prefix, origin):
        core.seen.append((pcm.size, prefix, origin))

    core._set_state = set_state
    core._recognize = recognize
    return core


class ProcessMissTest(unittest.TestCase):
    def test_miss_with_wake_name_becomes_command(self):
        core = make_core()

        async def run():
            ok = await core.process_miss(b"\x01\x00" * 16000, 0.948)
            await asyncio.sleep(0)
            return ok

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(core.state, voice_core.State.BUSY)
        self.assertEqual(core.seen[0][2], "wake")
        self.assertEqual(core.seen[0][0], 16000)

    def test_busy_station_ignores_miss(self):
        core = make_core(voice_core.State.BUSY)
        ok = asyncio.run(core.process_miss(b"\x01\x00" * 16000, 0.94))
        self.assertFalse(ok)

    def test_speaking_station_ignores_miss(self):
        core = make_core()
        core.output_busy = lambda: True
        ok = asyncio.run(core.process_miss(b"\x01\x00" * 16000, 0.94))
        self.assertFalse(ok)

    def test_short_window_ignored(self):
        core = make_core()
        ok = asyncio.run(core.process_miss(b"\x01\x00" * 100, 0.94))
        self.assertFalse(ok)

    def test_numpy_window_accepted(self):
        import numpy as np
        core = make_core()
        window = np.ones(16000, dtype=np.int16)
        self.assertTrue(asyncio.run(core.process_miss(window, 0.9)))


class RetroPrefixTest(unittest.TestCase):
    def _conn(self, peaks, prefix_len=1600):
        conn = voice_hub.Conn.__new__(voice_hub.Conn)
        conn.name = "lite-1"
        conn.retro = SimpleNamespace(available=True,
                                     scan=lambda pcm, thr: list(peaks))
        conn._snap_peaks = []
        conn.core = SimpleNamespace(prefix=b"\x00\x00" * prefix_len)
        return conn

    def test_earlier_key_extends_prefix(self):
        conn = self._conn([(1.0, 0.90), (5.9, 0.99)])
        pcm = b"\x00\x00" * (6 * 16000)
        asyncio.run(conn._retro_scan(pcm))
        # история ограничена 3 с: длинный снимок раздувал ASR и модель путалась
        self.assertEqual(len(conn.core.prefix), 3 * 32000 + 3200)

    def test_only_current_peak_keeps_prefix(self):
        conn = self._conn([(5.9, 0.99)])
        pcm = b"\x00\x00" * (6 * 16000)
        asyncio.run(conn._retro_scan(pcm))
        self.assertEqual(len(conn.core.prefix), 3200)


class NameLikeTest(unittest.TestCase):
    def test_soft_sign_variant_accepted(self):
        # Живой ASR 06.11: «Вот тебе, дерьмя, сколько времени?» — это «Гермес».
        self.assertTrue(voice_core.name_like("Вот тебе, дерьмя, сколько времени?"))
        self.assertTrue(voice_core.name_like("Окей, Дермес, сколько времени?"))

    def test_foreign_words_rejected(self):
        self.assertFalse(voice_core.name_like("Германия выиграла матч"))
        self.assertFalse(voice_core.name_like("Герметик застыл за час"))


if __name__ == "__main__":
    unittest.main()
