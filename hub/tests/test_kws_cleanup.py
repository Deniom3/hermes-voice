"""Обслуживание датасета: битые wav, ретенция по классам, лимит positives."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kws_train_cleanup as cleanup  # noqa: E402


def put(path: str, size: int, age_days: float) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * size)
    ts = time.time() - age_days * 86400
    os.utime(path, (ts, ts))
    return path


class CleanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_broken_wav_removed(self):
        bad = put(os.path.join(self.root, "negatives", "bad.wav"), 500, 0)
        cleanup.clean(self.root)
        self.assertFalse(os.path.exists(bad))

    def test_negatives_retention_30_days(self):
        old = put(os.path.join(self.root, "negatives", "old.wav"), 2000, 40)
        fresh = put(os.path.join(self.root, "negatives", "fresh.wav"), 2000, 1)
        cleanup.clean(self.root)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_unclear_retention_7_days(self):
        old = put(os.path.join(self.root, "unclear", "old.wav"), 2000, 10)
        fresh = put(os.path.join(self.root, "unclear", "fresh.wav"), 2000, 1)
        cleanup.clean(self.root)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_positives_kept_by_age(self):
        old = put(os.path.join(self.root, "positives", "old.wav"), 2000, 400)
        cleanup.clean(self.root)
        self.assertTrue(os.path.exists(old))

    def test_positives_trimmed_by_size(self):
        old_limit = cleanup.POSITIVES_LIMIT
        cleanup.POSITIVES_LIMIT = 3000
        try:
            first = put(os.path.join(self.root, "positives", "1.wav"), 2000, 10)
            second = put(os.path.join(self.root, "positives", "2.wav"), 2000, 1)
            cleanup.clean(self.root)
            self.assertFalse(os.path.exists(first))
            self.assertTrue(os.path.exists(second))
        finally:
            cleanup.POSITIVES_LIMIT = old_limit

    def test_orphan_json_removed_with_wav(self):
        wav = put(os.path.join(self.root, "negatives", "old.wav"), 2000, 40)
        meta = os.path.join(self.root, "negatives", "old.json")
        with open(meta, "w", encoding="utf-8") as f:
            f.write("{}")
        cleanup.clean(self.root)
        self.assertFalse(os.path.exists(wav))
        self.assertFalse(os.path.exists(meta))

    def test_dry_run_changes_nothing(self):
        old = put(os.path.join(self.root, "negatives", "old.wav"), 2000, 40)
        stats = cleanup.clean(self.root, dry=True)
        self.assertTrue(os.path.exists(old))
        self.assertEqual(stats["удалено_файлов"], 1)


if __name__ == "__main__":
    unittest.main()
