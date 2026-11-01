"""Smart Turn: порог, last_prob, окно 8 секунд. Модель подменена."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import smart_turn  # noqa: E402


class FakeSession:
    def __init__(self, prob: float):
        self.prob = prob
        self.shapes: list[tuple] = []

    def run(self, _outputs, feeds):
        self.shapes.append(tuple(np.asarray(feeds["input_features"]).shape))
        return [np.array([[self.prob]], dtype=np.float32)]


def make_turn(prob: float, threshold: float = 0.75):
    turn = smart_turn.SmartTurn.__new__(smart_turn.SmartTurn)
    turn.session = FakeSession(prob)
    turn.threshold = threshold
    turn.last_prob = None
    return turn


class SmartTurnTest(unittest.TestCase):
    def setUp(self):
        self.old = smart_turn.compute_whisper_log_mel_features
        smart_turn.compute_whisper_log_mel_features = (
            lambda audio, do_normalize=True: np.zeros((80, 3000), dtype=np.float32))

    def tearDown(self):
        smart_turn.compute_whisper_log_mel_features = self.old

    def test_end_above_threshold(self):
        turn = make_turn(0.9)
        self.assertTrue(turn(np.zeros(16000, dtype=np.int16)))
        self.assertAlmostEqual(turn.last_prob or 0.0, 0.9, places=5)

    def test_pause_below_threshold(self):
        turn = make_turn(0.4)
        self.assertFalse(turn(np.zeros(16000, dtype=np.int16)))

    def test_threshold_is_configurable(self):
        turn = make_turn(0.6)
        self.assertFalse(turn(np.zeros(16000, dtype=np.int16)))
        turn.threshold = 0.5
        self.assertTrue(turn(np.zeros(16000, dtype=np.int16)))

    def test_short_audio_padded_to_window(self):
        turn = make_turn(0.9)
        turn(np.zeros(4000, dtype=np.int16))
        self.assertEqual(turn.session.shapes[0], (1, 80, 3000))

    def test_long_audio_trimmed_to_tail(self):
        turn = make_turn(0.9)
        turn(np.zeros(smart_turn.SAMPLE_RATE * 20, dtype=np.int16))
        self.assertEqual(turn.session.shapes[0], (1, 80, 3000))


if __name__ == "__main__":
    unittest.main()
