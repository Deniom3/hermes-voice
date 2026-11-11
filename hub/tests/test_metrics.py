"""Метрики хаба: формат Prometheus, счётчики, гистограммы, набор обучения."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import metrics  # noqa: E402


def sample(text: str, prefix: str) -> float:
    for line in text.splitlines():
        if line.startswith(prefix + " ") and not line.startswith("#"):
            return float(line.split()[-1])
    raise AssertionError(f"нет метрики {prefix}:\n{text}")


class RenderTest(unittest.TestCase):
    def setUp(self):
        metrics.METRICS.reset()

    def test_counter_and_labels(self):
        metrics.METRICS.inc("wake_activations_total", {"station": "lite-1"})
        metrics.METRICS.inc("wake_activations_total", {"station": "lite-1"})
        out = metrics.METRICS.render()
        self.assertIn("# TYPE hermes_voice_wake_activations_total counter", out)
        self.assertEqual(sample(out, 'hermes_voice_wake_activations_total{station="lite-1"}'), 2)

    def test_gauge_set(self):
        metrics.METRICS.set("station_online", 1, {"station": "lite-2"})
        out = metrics.METRICS.render()
        self.assertEqual(sample(out, 'hermes_voice_station_online{station="lite-2"}'), 1)

    def test_histogram_buckets_and_sum(self):
        metrics.METRICS.observe("asr_seconds", 0.08, {"station": "lite-1"})
        metrics.METRICS.observe("asr_seconds", 0.6, {"station": "lite-1"})
        out = metrics.METRICS.render()
        self.assertIn("# TYPE hermes_voice_asr_seconds histogram", out)
        self.assertEqual(sample(out, 'hermes_voice_asr_seconds_bucket{le="0.1",station="lite-1"}'), 1)
        self.assertEqual(sample(out, 'hermes_voice_asr_seconds_bucket{le="1",station="lite-1"}'), 2)
        self.assertEqual(sample(out, 'hermes_voice_asr_seconds_count{station="lite-1"}'), 2)
        self.assertAlmostEqual(sample(out, 'hermes_voice_asr_seconds_sum{station="lite-1"}'), 0.68, places=5)

    def test_inf_bucket_always_present(self):
        metrics.METRICS.observe("wake_score", 0.97, {"station": "lite-1"})
        out = metrics.METRICS.render()
        self.assertEqual(sample(out, 'hermes_voice_wake_score_bucket{le="+Inf",station="lite-1"}'), 1)

    def test_unknown_histogram_raises(self):
        with self.assertRaises(KeyError):
            metrics.METRICS.observe("no_such_metric", 1.0)


class DatasetScanTest(unittest.TestCase):
    def test_counts_wav_and_bytes(self):
        metrics.METRICS.reset()
        with tempfile.TemporaryDirectory() as root:
            for kind, names in (("positives", ["a.wav", "b.wav"]),
                                ("negatives", ["c.wav"]),
                                ("misses", ["d.wav", "e.json"])):
                os.makedirs(os.path.join(root, kind))
                for name in names:
                    with open(os.path.join(root, kind, name), "wb") as f:
                        f.write(b"x" * 100)
            metrics.scan_dataset(root)
            out = metrics.METRICS.render()
            self.assertEqual(sample(out, 'hermes_voice_dataset_files{kind="positives"}'), 2)
            self.assertEqual(sample(out, 'hermes_voice_dataset_files{kind="misses"}'), 1)
            self.assertEqual(sample(out, 'hermes_voice_dataset_bytes{kind="negatives"}'), 100)

    def test_missing_dir_is_zero(self):
        metrics.METRICS.reset()
        with tempfile.TemporaryDirectory() as root:
            metrics.scan_dataset(root)
            out = metrics.METRICS.render()
            self.assertEqual(sample(out, 'hermes_voice_dataset_files{kind="positives"}'), 0)


if __name__ == "__main__":
    unittest.main()
