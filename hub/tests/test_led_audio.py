"""Кольцо колонки: длительность WAV и отложенное гашение."""
from __future__ import annotations

import io
import os
import sys
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_hub  # noqa: E402


def make_wav(seconds: float, rate: int = 48000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class WavSecondsTest(unittest.TestCase):
    def test_half_second(self):
        self.assertAlmostEqual(voice_hub.wav_seconds(make_wav(0.5)), 0.5, places=2)

    def test_three_seconds(self):
        self.assertAlmostEqual(voice_hub.wav_seconds(make_wav(3.0)), 3.0, places=2)

    def test_fallback_without_header(self):
        # не WAV — оцениваем как 48 кГц, 16 бит, моно: 96000 байт ≈ 1 с
        self.assertAlmostEqual(voice_hub.wav_seconds(b"\x00" * 96000), 1.0, places=2)


if __name__ == "__main__":
    unittest.main()
