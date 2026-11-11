"""Изоляция детектора: у каждой колонки своя модель и свои признаки.

Причина: `MicroWakeWordFeatures` держит состояние потока (мел-буферы и
скользящее окно). Когда две колонки кормят один экземпляр, кадры разных
потоков перемешиваются и вероятность падает у обеих. Замер на живых записях:
одиночный поток 0.996, два потока через общую модель 0.046.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_loop  # noqa: E402
import wake_gate  # noqa: E402


class FakeModel:
    def __init__(self):
        self.resets = 0

    def reset(self):
        self.resets += 1

    def process_streaming_prob(self, feats):
        return None


class FakeFeatures:
    def __init__(self):
        self.resets = 0
        self.frames = 0

    def reset(self):
        self.resets += 1

    def process_streaming(self, frame):
        self.frames += 1
        return []


class DetectorIsolationTest(unittest.TestCase):
    def test_each_detector_gets_own_model_and_features(self):
        made = []

        def create():
            pair = (FakeModel(), FakeFeatures())
            made.append(pair)
            return pair

        with patch.object(wake_gate, "create", create):
            first = voice_loop.WakeDetector()
            second = voice_loop.WakeDetector()

        self.assertEqual(len(made), 2)
        self.assertIsNot(first._model, second._model)
        self.assertIsNot(first._features, second._features)

    def test_missing_model_raises_with_reason(self):
        with patch.object(wake_gate, "create", lambda: (None, None)), \
                patch.object(wake_gate, "status", lambda: {"reason": "нет модели"}):
            with self.assertRaises(RuntimeError) as ctx:
                voice_loop.WakeDetector()
        self.assertIn("нет модели", str(ctx.exception))

    def test_feed_uses_own_features(self):
        made = []

        def create():
            pair = (FakeModel(), FakeFeatures())
            made.append(pair)
            return pair

        with patch.object(wake_gate, "create", create):
            det = voice_loop.WakeDetector()
        det.feed(b"\x00" * 640)
        self.assertEqual(det._model.resets, 1)
        self.assertGreater(det._features.frames, 0)


if __name__ == "__main__":
    unittest.main()
