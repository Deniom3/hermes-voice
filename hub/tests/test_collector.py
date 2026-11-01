"""Сбор окон событий для дообучения: класс, дедуп, отсев короткого.

Ошибка здесь не видна в работе, но портит датасет: клип не в тот каталог или
дубликат учит модель дважды на одном и том же.

Сигнал в тестах — синус с паузами по краям, а не ровный тон: нормализация
ищет активную речь по контрасту с тишиной, и ровный сигнал она отбрасывает
(active_rms получается нулевым, потому что тишины в кадре нет).
"""
import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_capture  # noqa: E402


def speech_pcm(seconds=2.0, freq=150.0, amp=0.3, silence=0.4):
    """Речеподобное окно: тишина по краям, сигнал в середине."""
    sr = voice_capture.SR
    n = int(sr * seconds)
    w = np.zeros(n, dtype=np.float32)
    a, b = int(sr * silence), n - int(sr * silence)
    t = np.arange(max(0, b - a)) / sr
    w[a:b] = np.sin(2 * np.pi * freq * t) * amp
    return (w * 32767).astype("<i2").tobytes()


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.col = voice_capture.Collector(root=self.tmp.name, enabled=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_saves_wav_and_sidecar_in_class_dir(self):
        path = self.col.save("negatives", speech_pcm(), 1.0, {"reason": "no_name"})
        self.assertIsNotNone(path)
        self.assertEqual(os.path.dirname(path),
                         os.path.join(self.tmp.name, "negatives"))
        sidecar = path[:-4] + ".json"
        self.assertTrue(os.path.exists(sidecar))
        with open(sidecar) as f:
            meta = json.load(f)
        self.assertEqual(meta["class"], "negatives")
        self.assertEqual(meta["reason"], "no_name")
        self.assertIn("md5", meta)
        self.assertGreater(meta["peak"], 0)
        self.assertGreater(meta["sec"], 1.0)

    def test_duplicate_clip_not_saved_twice(self):
        pcm = speech_pcm()
        first = self.col.save("negatives", pcm, 1.0, {})
        second = self.col.save("negatives", pcm, 1.0, {})
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_same_clip_in_other_class_is_separate(self):
        pcm = speech_pcm()
        self.assertIsNotNone(self.col.save("negatives", pcm, 1.0, {}))
        self.assertIsNotNone(self.col.save("positives", pcm, 1.0, {}))

    def test_short_clip_rejected(self):
        self.assertIsNone(self.col.save("unclear", speech_pcm(0.8), 0.4, {}))

    def test_silence_rejected(self):
        silence = b"\x00\x00" * voice_capture.SR * 2
        self.assertIsNone(self.col.save("unclear", silence, 1.0, {}))

    def test_disabled_collector_saves_nothing(self):
        col = voice_capture.Collector(root=self.tmp.name, enabled=False)
        self.assertIsNone(col.save("positives", speech_pcm(), 1.0, {}))
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "positives")), [])

    def test_all_class_dirs_created(self):
        for sub in ("positives", "negatives", "unclear"):
            self.assertTrue(os.path.isdir(os.path.join(self.tmp.name, sub)))


class CropTest(unittest.TestCase):
    def test_crop_honours_total_window(self):
        """Длину окна задаёт total, а не right (right не используется)."""
        pcm = speech_pcm(4.0)
        cropped = voice_capture.crop_around(pcm, 2.0, left=0.5, total=1.0)
        self.assertAlmostEqual(len(cropped) / voice_capture.SR, 1.0, delta=0.05)

    def test_crop_starts_before_peak(self):
        pcm = speech_pcm(4.0)
        cropped = voice_capture.crop_around(pcm, 2.0, left=0.5, total=1.0)
        self.assertEqual(len(cropped), int(1.0 * voice_capture.SR))

    def test_crop_at_start_and_beyond_end(self):
        pcm = speech_pcm(3.0)
        self.assertGreater(len(voice_capture.crop_around(pcm, 0.0)), 0)
        self.assertGreater(len(voice_capture.crop_around(pcm, 99.0)), 0)

    def test_crop_keeps_minimum_duration(self):
        """У начала записи окно всё равно добирается до 1.2 с."""
        pcm = speech_pcm(3.0)
        cropped = voice_capture.crop_around(pcm, 0.05)
        self.assertGreaterEqual(len(cropped), int(1.2 * voice_capture.SR))


class NormalizeTest(unittest.TestCase):
    def test_quiet_signal_amplified_to_target(self):
        pcm = speech_pcm(2.0)
        quiet = (np.frombuffer(pcm, dtype="<i2") // 20).astype("<i2")
        out = voice_capture.normalize(quiet)
        self.assertIsNotNone(out)
        target = voice_capture.TARGET_RMS
        self.assertAlmostEqual(voice_capture.active_rms(out.astype(np.float32)),
                               target, delta=target * 0.15)

    def test_loud_signal_not_clipped(self):
        pcm = speech_pcm(2.0, amp=0.95)
        out = voice_capture.normalize(np.frombuffer(pcm, dtype="<i2"))
        self.assertIsNotNone(out)
        self.assertLess(int(np.abs(out).max()), 32767)

    def test_flat_tone_without_silence_rejected(self):
        """Ровный тон без пауз — не речь: контраста нет, active_rms = 0."""
        sr = voice_capture.SR
        t = np.arange(sr) / sr
        flat = (np.sin(2 * np.pi * 150 * t) * 0.3 * 32767).astype("<i2")
        self.assertIsNone(voice_capture.normalize(flat))

    def test_silence_rejected(self):
        silence = np.zeros(voice_capture.SR, dtype="<i2")
        self.assertIsNone(voice_capture.normalize(silence))


class RetroScanTest(unittest.TestCase):
    def test_scanner_without_model_is_unavailable(self):
        scan = voice_capture.RetroScanner(model_json="/nonexistent/manifest.json")
        self.assertFalse(scan.available)
        self.assertEqual(scan.scan(b"\x00\x00" * 16000, 0.55), [])


if __name__ == "__main__":
    unittest.main()
