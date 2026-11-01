"""Фразы-заполнители в паузе: включение, ротация, поиск готовых записей."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_fillers  # noqa: E402


class EnabledTest(unittest.TestCase):
    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VOICE_FILLERS", None)
            self.assertFalse(voice_fillers.enabled())

    def test_switched_on(self):
        with mock.patch.dict(os.environ, {"VOICE_FILLERS": "1"}):
            self.assertTrue(voice_fillers.enabled())

    def test_explicit_off_values(self):
        for value in ("0", "false", "no", "off", ""):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"VOICE_FILLERS": value}):
                    self.assertFalse(voice_fillers.enabled())


class TakeTest(unittest.TestCase):
    def setUp(self):
        voice_fillers._DECKS.clear()
        voice_fillers._LAST[0] = None

    def test_no_repeat_until_pool_exhausted(self):
        pool = ["раз", "два", "три"]
        got = [voice_fillers.take(pool) for _ in range(3)]
        self.assertEqual(sorted(got), sorted(pool))

    def test_deck_restarts_after_exhaustion(self):
        pool = ["раз", "два"]
        first = [voice_fillers.take(pool) for _ in range(2)]
        second = [voice_fillers.take(pool) for _ in range(2)]
        self.assertEqual(sorted(first), sorted(pool))
        self.assertEqual(sorted(second), sorted(pool))

    def test_last_phrase_does_not_repeat_across_decks(self):
        pool = ["раз", "два"]
        seen = [voice_fillers.take(pool) for _ in range(2)]
        next_phrase = voice_fillers.take(pool)
        self.assertNotEqual(next_phrase, seen[-1])

    def test_single_phrase_pool_survives(self):
        self.assertEqual(voice_fillers.take(["только"]), "только")
        self.assertEqual(voice_fillers.take(["только"]), "только")


class FileLookupTest(unittest.TestCase):
    def test_found_in_filler_dir(self):
        phrase = voice_fillers.THINK[0]
        with tempfile.TemporaryDirectory() as d:
            name = voice_fillers.key(phrase) + ".wav"
            path = os.path.join(d, name)
            with open(path, "wb") as f:
                f.write(b"RIFFtest")
            with mock.patch.object(voice_fillers, "REPO_DIRS", [d]), \
                    mock.patch.object(voice_fillers, "CACHE_DIR", d):
                self.assertEqual(voice_fillers.file_for(phrase), path)

    def test_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(voice_fillers, "REPO_DIRS", [d]), \
                    mock.patch.object(voice_fillers, "CACHE_DIR", d):
                self.assertIsNone(voice_fillers.file_for("такой фразы нет"))

    def test_audio_reads_bytes(self):
        phrase = voice_fillers.THINK[0]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, voice_fillers.key(phrase) + ".wav")
            with open(path, "wb") as f:
                f.write(b"RIFFdata")
            with mock.patch.object(voice_fillers, "REPO_DIRS", [d]), \
                    mock.patch.object(voice_fillers, "CACHE_DIR", d):
                self.assertEqual(voice_fillers.audio(phrase), b"RIFFdata")

    def test_key_differs_per_voice(self):
        phrase = voice_fillers.THINK[0]
        before = voice_fillers.key(phrase)
        with mock.patch.object(voice_fillers, "VOICE", "other"):
            self.assertNotEqual(before, voice_fillers.key(phrase))


class PoolsTest(unittest.TestCase):
    def test_pools_not_empty_and_unique(self):
        for pool in (voice_fillers.THINK,):
            self.assertTrue(pool)
            self.assertEqual(len(pool), len(set(pool)))


if __name__ == "__main__":
    unittest.main()
