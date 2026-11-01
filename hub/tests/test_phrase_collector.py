"""Тесты сборщика фразы: где кончается реплика и где она ещё не началась.

Запуск: python -m unittest discover -s hub/tests -t . -v
"""
import unittest

import numpy as np

from hub.voice_loop import PhraseCollector

SR = 16000


def silence(ms: int) -> bytes:
    return np.zeros(int(SR * ms / 1000), dtype=np.int16).tobytes()


def speech(ms: int, level: int = 3000) -> bytes:
    """Речеподобный сигнал: низкий тон со слогами 50 мс звук / 30 мс пауза.

    Период 80 мс намеренно короче окна анализа (0.2 с), чтобы в любом окне
    были и громкие кадры, и пауза: признак речи считается по контрасту
    с шумовым полом окна, а у ровного тона контраста нет.
    """
    n = int(SR * ms / 1000)
    t = np.arange(n) / SR
    envelope = np.where((t % 0.08) < 0.05, 0.95, 0.02)
    return (level * envelope * np.sin(2 * np.pi * 150 * t)).astype(np.int16).tobytes()


class CollectorTest(unittest.TestCase):
    def test_cancel_when_nobody_speaks(self):
        """После «Окей, Гермес» ждём начало фразы; тишина = отмена, а не пустая фраза."""
        coll = PhraseCollector(skip_ms=0, wait_voice_s=0.5, silence_ms=200)
        result = None
        for _ in range(12):                      # 12 x 100 мс = 1.2 с тишины
            result = coll.feed(silence(100))
            if result is not None:
                break
        self.assertEqual(result, 'cancel')

    def test_phrase_after_pause_is_collected(self):
        """Пауза между именем и фразой — норма, фраза не должна теряться."""
        coll = PhraseCollector(skip_ms=0, wait_voice_s=6.0, silence_ms=300, min_voice_ms=200)
        coll.feed(silence(400))                  # думает после имени
        coll.feed(speech(500))
        result = None
        for _ in range(5):                       # тишина закрывает фразу
            result = coll.feed(silence(100))
            if isinstance(result, np.ndarray):
                break
        self.assertIsInstance(result, np.ndarray)
        self.assertGreater(len(result) / SR, 0.4)

    def test_short_blip_is_not_phrase(self):
        """Короткий щелчок короче min_voice_ms фразой не считается."""
        coll = PhraseCollector(skip_ms=0, wait_voice_s=6.0, silence_ms=200, min_voice_ms=400)
        coll.feed(speech(100))                   # 100 мс < 400 мс
        result = None
        for _ in range(5):
            result = coll.feed(silence(100))
            if result is not None:
                break
        self.assertNotIsInstance(result, np.ndarray)

    def test_max_length_is_bounded(self):
        """Предохранитель длины: непрерывный поток не растёт бесконечно."""
        coll = PhraseCollector(skip_ms=0, wait_voice_s=6.0, silence_ms=500, max_s=1.0)
        coll.feed(silence(500))                  # тишина перед фразой задаёт шумовой пол
        out = None
        for i in range(90):                      # 1.8 с потока при max 1.0 с
            # 200 мс речи, 60 мс паузы: пауза короче silence_ms, фраза не рвётся
            out = coll.feed(speech(20) if (i % 13) < 10 else silence(20))
            if isinstance(out, np.ndarray):
                break
        self.assertIsInstance(out, np.ndarray)
        self.assertLessEqual(len(out) / SR, 1.05)
        self.assertGreater(len(out) / SR, 0.9)

    def test_skip_trims_wake_tail(self):
        """skip_ms отрезает хвост самого слова активации."""
        coll = PhraseCollector(skip_ms=300, wait_voice_s=6.0, silence_ms=200)
        coll.feed(speech(300))                   # целиком уйдёт в skip
        self.assertFalse(coll.voiced)


if __name__ == '__main__':
    unittest.main()
