"""Библиотека реплик: включение заполнителей, ротация, поиск и кэш записей."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import replies  # noqa: E402


class EnabledTest(unittest.TestCase):
    def setUp(self):
        # Тест не должен зависеть от системного /etc/hermes-voice/hub.json.
        self._old_path = config.PATH
        config.PATH = "/nonexistent/hub-test.json"
        config._mtime, config._data = None, {}

    def tearDown(self):
        config.PATH = self._old_path
        config._mtime, config._data = None, {}

    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VOICE_FILLERS", None)
            self.assertFalse(replies.fillers_enabled())

    def test_switched_on(self):
        with mock.patch.dict(os.environ, {"VOICE_FILLERS": "1"}):
            self.assertTrue(replies.fillers_enabled())

    def test_explicit_off_values(self):
        for value in ("0", "false", "no", "off", ""):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"VOICE_FILLERS": value}):
                    self.assertFalse(replies.fillers_enabled())

    def test_timing_has_defaults(self):
        first, gap_min, gap_max, count = replies.fillers_timing()
        self.assertGreater(first, 0)
        self.assertLessEqual(gap_min, gap_max)
        self.assertGreater(count, 0)


class TakeTest(unittest.TestCase):
    def setUp(self):
        replies._DECKS.clear()
        replies._LAST[0] = ""

    def test_pool_from_manifest(self):
        pool = replies.pool("think")
        self.assertGreaterEqual(len(pool), 2)
        self.assertEqual(len(pool), len(set(pool)))

    def test_no_repeat_until_pool_exhausted(self):
        pool = replies.pool("think")
        got = [replies.take("think") for _ in range(len(pool))]
        self.assertEqual(sorted(got), sorted(pool))

    def test_deck_restarts_after_exhaustion(self):
        pool = replies.pool("think")
        first = [replies.take("think") for _ in range(len(pool))]
        second = [replies.take("think") for _ in range(len(pool))]
        self.assertEqual(sorted(first), sorted(pool))
        self.assertEqual(sorted(second), sorted(pool))

    def test_last_phrase_does_not_repeat_across_decks(self):
        pool = replies.pool("think")
        seen = [replies.take("think") for _ in range(len(pool))]
        self.assertNotEqual(replies.take("think"), seen[-1])

    def test_unknown_pool_raises(self):
        with self.assertRaises(RuntimeError):
            replies.take("no-such-pool")


class FileLookupTest(unittest.TestCase):
    def test_found_in_library(self):
        phrase = replies.pool("think")[0]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, replies.key(phrase) + ".wav")
            with open(path, "wb") as f:
                f.write(b"RIFFtest")
            with mock.patch.object(replies, "_repo_dir", lambda: d), \
                    mock.patch.object(replies, "_cache_dir", lambda: d):
                self.assertEqual(replies.path(phrase), path)
                self.assertEqual(replies.audio(phrase), b"RIFFtest")

    def test_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(replies, "_repo_dir", lambda: d), \
                    mock.patch.object(replies, "_cache_dir", lambda: d):
                self.assertIsNone(replies.path("такой фразы нет"))
                self.assertIsNone(replies.cached("такой фразы нет"))

    def test_key_differs_per_voice(self):
        phrase = replies.pool("think")[0]
        before = replies.key(phrase)
        with mock.patch.object(replies, "voice", lambda: "other"):
            self.assertNotEqual(before, replies.key(phrase))

    def test_store_roundtrip(self):
        text = "Громкость пять"
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(replies, "_repo_dir", lambda: d), \
                    mock.patch.object(replies, "_cache_dir", lambda: d):
                replies.store(text, b"RIFFcached")
                self.assertEqual(replies.cached(text), b"RIFFcached")

    def _make(self, folder, name, size=1024, age=0.0):
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(b"x" * size)
        if age:
            os.utime(path, (os.path.getmtime(path) - age, os.path.getmtime(path) - age))
        return path

    def test_prune_by_file_count_keeps_newest(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(5):
                self._make(d, f"old{i}.wav", age=1000 - i)
            self._make(d, "new.wav", age=0)
            with mock.patch.object(replies, "_cache_dir", lambda: d), \
                    mock.patch.object(replies, "cache_limits", lambda: (3, 50.0)):
                self.assertEqual(replies.prune_cache(), 3)
            left = sorted(os.listdir(d))
            self.assertEqual(len(left), 3)
            self.assertIn("new.wav", left)

    def test_prune_by_size(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(4):
                self._make(d, f"f{i}.wav", size=1024 * 1024, age=100 - i)
            with mock.patch.object(replies, "_cache_dir", lambda: d), \
                    mock.patch.object(replies, "cache_limits", lambda: (100, 2.0)):
                self.assertEqual(replies.prune_cache(), 2)
            self.assertEqual(len(os.listdir(d)), 2)

    def test_prune_noop_when_within_limits(self):
        with tempfile.TemporaryDirectory() as d:
            self._make(d, "one.wav")
            with mock.patch.object(replies, "_cache_dir", lambda: d), \
                    mock.patch.object(replies, "cache_limits", lambda: (10, 50.0)):
                self.assertEqual(replies.prune_cache(), 0)
            self.assertEqual(len(os.listdir(d)), 1)

    def test_store_prunes_after_write(self):
        with tempfile.TemporaryDirectory() as d:
            self._make(d, "old.wav", age=1000)
            with mock.patch.object(replies, "_repo_dir", lambda: d), \
                    mock.patch.object(replies, "_cache_dir", lambda: d), \
                    mock.patch.object(replies, "cache_limits", lambda: (1, 50.0)):
                replies.store("Новая реплика", b"RIFFnew")
            self.assertEqual(os.listdir(d), [replies.key("Новая реплика") + ".wav"])

    def test_long_text_not_cached(self):
        text = "а" * (replies.MAX_CACHE_CHARS + 1)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(replies, "_repo_dir", lambda: d), \
                    mock.patch.object(replies, "_cache_dir", lambda: d):
                replies.store(text, b"RIFFcached")
                self.assertIsNone(replies.cached(text))


if __name__ == "__main__":
    unittest.main()
