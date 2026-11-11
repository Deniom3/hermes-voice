"""Pre-roll активации должен попадать в аудио для ASR.

Раньше префикс отдавался сборщику обычным `feed`, и VAD выбрасывал тихое
«окей, Гермес» как фон. В журнале это выглядело так: клип активации 2.4 с,
а в ASR уходит 1.4 с и текст без начала фразы («Спокойной ночи» вместо
«О'кей, Дермис, спокойной ночи»).

Правило: активацию подтвердил детектор, поэтому префикс кладётся в фразу
целиком, без фильтра VAD.
"""
import asyncio
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_core  # noqa: E402
from voice_loop import PhraseCollector  # noqa: E402

SILENCE = np.zeros(3200, dtype=np.int16).tobytes()  # 0.2 с тишины


class CollectorSeedTest(unittest.TestCase):
    def make(self):
        return PhraseCollector(skip_ms=0, silence_ms=700, min_voice_ms=350,
                               wait_voice_s=6.0)

    def test_seed_keeps_quiet_prefix(self):
        coll = self.make()
        self.assertIsNone(coll.feed(SILENCE, SILENCE, seed=True))
        self.assertEqual(len(coll.buf), 3200)

    def test_seed_marks_phrase_started(self):
        coll = self.make()
        coll.feed(SILENCE, SILENCE, seed=True)
        self.assertTrue(coll.voiced)
        self.assertEqual(coll.voice_run, 3200)

    def test_plain_feed_still_drops_quiet_prefix(self):
        # Без seed поведение прежнее: тишина не копится, пока нет речи.
        coll = self.make()
        self.assertIsNone(coll.feed(SILENCE, SILENCE))
        self.assertEqual(len(coll.buf), 0)
        self.assertFalse(coll.voiced)

    def test_seed_then_speech_keeps_both(self):
        coll = self.make()
        coll.feed(SILENCE, SILENCE, seed=True)
        # Речь: синус с шумом, заметно громче пола.
        t = np.arange(3200) / 16000
        speech = (3000 * np.sin(2 * np.pi * 200 * t)).astype(np.int16)
        speech = (speech + np.random.default_rng(0).normal(0, 50,
                                                          3200)).astype(np.int16)
        coll.feed(speech.tobytes(), speech.tobytes())
        self.assertEqual(len(coll.buf), 6400)


class CoreSeedTest(unittest.TestCase):
    def test_seed_collector_passes_preroll_to_phrase(self):
        core = voice_core.SpeechCore.__new__(voice_core.SpeechCore)
        core.coll = PhraseCollector(skip_ms=0, wait_voice_s=6.0)
        core.prefix = b""
        core.origin = "wake"

        prefix = SILENCE * 2
        asyncio.run(core._seed_collector(prefix))

        self.assertEqual(len(core.coll.buf), len(prefix) // 2)


if __name__ == "__main__":
    unittest.main()
