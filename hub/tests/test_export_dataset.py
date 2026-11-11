"""Правила выгрузки данных обучения: в версию идёт только полезное.

Смысл теста — держать фильтры, а не сам перенос файлов. Если фильтр ослабнет,
в обучение поедет тишина, клиппинг или «positives» без обращения, и это
заметят уже по качеству модели, то есть поздно.
"""
import json
import os
import sys
import tempfile
import unittest
import wave

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "hub"))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import export_dataset as prep  # noqa: E402

SR = 16000


def speech(seconds=1.5, amp=3000, seed=0):
    """Речеподобное окно: всплески с паузами.

    Ровный тон не годится: сборщик считает активную речь по кадрам выше
    порога, у ровного тона все кадры одинаковы и active_rms выходит нулём
    (на этом раньше падал тест сборщика).
    """
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    x = np.zeros(n)
    for start in np.arange(0.1, max(0.2, seconds - 0.25), 0.35):
        a = int(start * SR)
        b = min(n, a + int(0.22 * SR))
        x[a:b] = rng.normal(0, amp, b - a)
    x[: int(0.1 * SR)] = 0
    x[-int(0.1 * SR):] = 0
    return np.clip(x, -32768, 32767).astype(np.int16)


def write_event(root, cls, name, pcm, meta):
    d = os.path.join(root, cls)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name + ".wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return path


class BuildRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.collect = os.path.join(self.tmp, "collect")
        self.out = os.path.join(self.tmp, "datasets")

    def build(self, **kw):
        opts = dict(root=self.collect, out_root=self.out, name="set",
                    min_score=0.9, with_unclear=False,
                    exclude_existing=False, dry=False)
        opts.update(kw)
        return prep.export(**opts)

    def test_good_positive_and_negative_are_kept(self):
        write_event(self.collect, "positives", "p1", speech(),
                    {"text": "Окей, Гермес, включи свет", "reason": "wake", "score": 0.98})
        write_event(self.collect, "negatives", "n1", speech(seed=7),
                    {"text": "Плакала, нервничала", "reason": "no_name", "score": 0.96})
        m = self.build()
        self.assertEqual(m["counts"], {"positives": 1, "negatives": 1})
        self.assertEqual(m["skipped"], {})
        for cls in ("positives", "negatives"):
            self.assertTrue(os.path.exists(os.path.join(self.out, "set", cls, f"{cls[0]}1.wav")))

    def test_silence_is_rejected(self):
        write_event(self.collect, "negatives", "sil", np.zeros(SR * 2, dtype=np.int16),
                    {"text": "", "reason": "no_name", "score": 0.96})
        m = self.build()
        self.assertEqual(m["counts"]["negatives"], 0)
        self.assertTrue(any("тишина" in k or "активной речи" in k for k in m["skipped"]))

    def test_clipping_is_rejected(self):
        loud = np.clip(speech(amp=40000), -32768, 32767).astype(np.int16)
        write_event(self.collect, "positives", "clip", loud,
                    {"text": "Окей, Гермес", "reason": "wake", "score": 0.99})
        m = self.build()
        self.assertEqual(m["counts"]["positives"], 0)
        self.assertIn("клиппинг", m["skipped"])

    def test_short_window_is_rejected(self):
        write_event(self.collect, "positives", "short", speech(0.8),
                    {"text": "Окей, Гермес", "reason": "wake", "score": 0.99})
        m = self.build()
        self.assertEqual(m["counts"]["positives"], 0)
        self.assertTrue(any("короткое" in k for k in m["skipped"]))

    def test_positive_without_name_is_rejected(self):
        write_event(self.collect, "positives", "noname", speech(),
                    {"text": "Германия победила в финале", "reason": "wake", "score": 0.97})
        m = self.build()
        self.assertEqual(m["counts"]["positives"], 0)
        self.assertIn("positives без обращения в тексте", m["skipped"])

    def test_positive_without_text_is_rejected(self):
        write_event(self.collect, "positives", "notext", speech(),
                    {"text": "", "reason": "wake", "score": 0.97})
        m = self.build()
        self.assertEqual(m["counts"]["positives"], 0)
        self.assertIn("positives без текста", m["skipped"])

    def test_miss_with_name_goes_to_positives(self):
        """Промах с обращением — это positive для обучения.

        Дефект: `misses` не входили в `CLASSES`, поэтому фразы, на которых
        детектор дал 0.83–0.94 и не сработал, в набор не попадали вообще —
        модель не видела именно тех случаев, которые должна выучить.
        """
        write_event(self.collect, "misses", "m1", speech(seed=3),
                    {"text": "Окей, Герми, сколько времени?", "reason": "near_miss",
                     "score": 0.887})
        m = self.build()
        self.assertEqual(m["counts"], {"positives": 1, "negatives": 0})
        dst = os.path.join(self.out, "set", "positives", "m1.wav")
        self.assertTrue(os.path.exists(dst))
        meta = json.load(open(os.path.join(self.out, "set", "positives", "m1.json")))
        self.assertEqual(meta.get("source_class"), "misses")

    def test_miss_without_name_is_rejected(self):
        write_event(self.collect, "misses", "m2", speech(seed=4),
                    {"text": "Холодно, говорю.", "reason": "near_miss", "score": 0.9})
        m = self.build()
        self.assertEqual(m["counts"]["positives"], 0)
        self.assertIn("misses без обращения в тексте", m["skipped"])

    def test_negative_with_foreign_reason_is_rejected(self):
        write_event(self.collect, "negatives", "odd", speech(seed=12),
                    {"text": "что-то", "reason": "verify_fail", "score": 0.99})
        m = self.build()
        self.assertEqual(m["counts"]["negatives"], 0)
        self.assertTrue(any("чужой причиной" in k for k in m["skipped"]))

    def test_weak_negative_is_rejected(self):
        write_event(self.collect, "negatives", "weak", speech(seed=11),
                    {"text": "что-то", "reason": "no_name", "score": 0.62})
        m = self.build()
        self.assertEqual(m["counts"]["negatives"], 0)
        self.assertTrue(any("слабым score" in k for k in m["skipped"]))

    def test_duplicate_in_batch_is_rejected(self):
        pcm = speech()
        write_event(self.collect, "negatives", "a", pcm,
                    {"text": "раз", "reason": "no_name", "score": 0.96})
        write_event(self.collect, "negatives", "b", pcm,
                    {"text": "два", "reason": "no_name", "score": 0.96})
        m = self.build()
        self.assertEqual(m["counts"]["negatives"], 1)
        self.assertIn("дубль в партии", m["skipped"])

    def test_existing_dataset_is_not_duplicated(self):
        write_event(self.collect, "negatives", "n1", speech(seed=13),
                    {"text": "раз", "reason": "no_name", "score": 0.96})
        self.build()
        m = self.build(name="set2", exclude_existing=True)
        self.assertEqual(m["counts"]["negatives"], 0)
        self.assertIn("уже есть в собранных наборах", m["skipped"])

    def test_unclear_skipped_by_default_and_taken_with_flag(self):
        write_event(self.collect, "unclear", "u1", speech(),
                    {"text": "", "reason": "short_text", "score": 0.95})
        self.assertEqual(self.build()["counts"].get("unclear", 0), 0)
        m = self.build(name="set3", with_unclear=True)
        self.assertEqual(m["counts"]["unclear"], 1)

    def test_quiet_but_valid_event_is_relevelled(self):
        quiet = (speech(amp=300)).astype(np.int16)
        write_event(self.collect, "negatives", "q", quiet,
                    {"text": "раз", "reason": "no_name", "score": 0.96})
        m = self.build()
        self.assertEqual(m["counts"]["negatives"], 1)
        item = m["items"][0]
        self.assertTrue(item["level_fixed"], "тихое окно должно выравниваться")
        self.assertGreaterEqual(item["rms"], 1800)

    def test_manifest_written(self):
        write_event(self.collect, "positives", "p1", speech(),
                    {"text": "Окей, Гермес", "reason": "wake", "score": 0.99})
        self.build()
        with open(os.path.join(self.out, "set", "manifest.json"), encoding="utf-8") as f:
            m = json.load(f)
        self.assertEqual(m["counts"]["positives"], 1)
        self.assertEqual(m["items"][0]["file"], "positives/p1.wav")


if __name__ == "__main__":
    unittest.main()
