"""Сбор промахов: пик был, а порог не превышен.

Это данные, которых не хватало для дообучения: раньше на диск попадали только
события после срабатывания, поэтому модель не видела, где почти справилась.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_capture  # noqa: E402


class MissWatcherTest(unittest.TestCase):
    def make(self, **kw):
        opts = dict(min_score=0.70, high=0.95, gap_s=0.02, min_gap_s=0.0)
        opts.update(kw)
        return voice_capture.MissWatcher(**opts)

    def test_closed_episode_returns_peak(self):
        m = self.make()
        self.assertIsNone(m.offer(0.82))
        self.assertIsNone(m.offer(0.91))
        time.sleep(0.05)
        got = m.offer(0.10)
        self.assertIsNotNone(got)
        peak_at, peak = got
        self.assertAlmostEqual(peak, 0.91)
        self.assertLessEqual(peak_at, time.monotonic())

    def test_default_band_starts_at_065(self):
        """По умолчанию пишем 0.65–0.95: живой промах 08.11 дал 0.697."""
        m = voice_capture.MissWatcher(gap_s=0.02, min_gap_s=0.0)
        self.assertIsNone(m.offer(0.60))
        time.sleep(0.05)
        self.assertIsNone(m.offer(0.10))
        m2 = voice_capture.MissWatcher(gap_s=0.02, min_gap_s=0.0)
        m2.offer(0.70)
        time.sleep(0.05)
        got = m2.offer(0.10)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertAlmostEqual(got[1], 0.70)

    def test_activation_resets_episode(self):
        """Пик выше порога — это настоящее срабатывание, промах не пишем."""
        m = self.make()
        m.offer(0.80)
        self.assertIsNone(m.offer(0.97))
        time.sleep(0.05)
        self.assertIsNone(m.offer(0.10))

    def test_activation_blocks_following_residual_peak(self):
        """Остаточный пик после срабатывания не должен дублировать клип в misses."""
        m = self.make(block_s=1.0)
        m.reset()
        self.assertIsNone(m.offer(0.80))
        time.sleep(0.05)
        self.assertIsNone(m.offer(0.10))

    def test_silence_does_not_produce_episode(self):
        m = self.make()
        for _ in range(5):
            m.offer(0.2)
        time.sleep(0.05)
        self.assertIsNone(m.offer(0.1))

    def test_low_peak_is_ignored(self):
        m = self.make()
        m.offer(0.69)
        time.sleep(0.05)
        self.assertIsNone(m.offer(0.1))

    def test_rate_limit(self):
        m = self.make(min_gap_s=10.0)
        m.offer(0.9)
        time.sleep(0.05)
        self.assertIsNotNone(m.offer(0.1))
        m.offer(0.9)
        time.sleep(0.05)
        self.assertIsNone(m.offer(0.1))

    def test_hourly_limit(self):
        m = self.make(per_hour=1)
        m.offer(0.9)
        time.sleep(0.05)
        self.assertIsNotNone(m.offer(0.1))
        m.offer(0.9)
        time.sleep(0.05)
        self.assertIsNone(m.offer(0.1))


class MissSaveTest(unittest.TestCase):
    def test_collector_has_misses_dir(self):
        root = tempfile.mkdtemp(prefix="kws-")
        voice_capture.Collector(root=root, enabled=True)
        self.assertTrue(os.path.isdir(os.path.join(root, "misses")))


if __name__ == "__main__":
    unittest.main()
