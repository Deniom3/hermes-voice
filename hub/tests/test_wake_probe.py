"""Замер промахов: полосы балла, окно 2 с и отчёт раз в N секунд."""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "station"))

import wake_probe  # noqa: E402


class BandTest(unittest.TestCase):
    def test_edges(self):
        cases = [(0.0, "<0.5"), (0.499, "<0.5"), (0.5, "0.5-0.7"),
                 (0.699, "0.5-0.7"), (0.7, "0.7-0.9"), (0.899, "0.7-0.9"),
                 (0.9, "0.9-0.95"), (0.949, "0.9-0.95"), (0.95, ">=0.95"),
                 (0.999, ">=0.95")]
        for score, expected in cases:
            self.assertEqual(wake_probe.band_for(score), expected, score)


class ProbeTest(unittest.TestCase):
    def test_max_per_window_is_counted_once(self):
        p = wake_probe.ScoreProbe(window_s=2.0, report_s=600.0)
        # внутри одного окна несколько баллов — берётся максимум (0.87)
        p.offer(0.10, now=0.0)
        p.offer(0.87, now=0.5)
        p.offer(0.20, now=1.0)
        self.assertEqual(p.windows, 0, "окно ещё не закрылось")
        # закрываем окно на 2-й секунде
        p.offer(0.05, now=2.0)
        self.assertEqual(p.windows, 1)
        self.assertEqual(p.counts["0.7-0.9"], 1)
        self.assertEqual(p.counts["<0.5"], 0)

    def test_threshold_hit_is_counted_in_top_band(self):
        p = wake_probe.ScoreProbe(window_s=2.0)
        p.offer(0.97, now=0.0)
        p.offer(0.0, now=2.0)
        self.assertEqual(p.counts[">=0.95"], 1)

    def test_report_waits_for_interval_and_resets(self):
        p = wake_probe.ScoreProbe(window_s=2.0, report_s=10.0)
        p.offer(0.93, now=0.0)
        p.offer(0.0, now=2.0)
        self.assertIsNone(p.report(now=0.0), "первый вызов задаёт отсчёт")
        self.assertIsNone(p.report(now=5.0), "отчёт раньше срока")
        text = p.report(now=10.0)
        self.assertIsNotNone(text)
        self.assertIn("0.9-0.95=1", text)
        self.assertIn("пик 0.930", text)
        # после отчёта счётчики обнулены
        self.assertEqual(p.counts["0.9-0.95"], 0)
        self.assertEqual(p.peak, 0.0)

    def test_no_audio_message(self):
        p = wake_probe.ScoreProbe(report_s=1.0)
        self.assertIsNone(p.report(now=0.0), "первый вызов задаёт отсчёт")
        text = p.report(now=2.0)
        self.assertIn("аудио не приходило", text)


if __name__ == "__main__":
    unittest.main()
