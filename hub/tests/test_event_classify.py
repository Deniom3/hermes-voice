"""Куда попадает окно события: positives / negatives / unclear.

Класс определяет, на чём модель будет учиться. Перепутать обращение с
негативом — значит учить её не срабатывать на имя: такой дефект уже был.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_capture import classify  # noqa: E402


class ClassifyTest(unittest.TestCase):
    def test_address_goes_to_positives(self):
        for text in ("Окей, Гермес", "Гермец, включи музыку", "Гермес."):
            with self.subTest(text=text):
                self.assertEqual(classify(text), "positives")

    def test_false_trigger_goes_to_negatives(self):
        """Детектор сработал, имени не было — это hard-негатив."""
        self.assertEqual(classify("Помнишь?"), "negatives")
        self.assertEqual(classify("Сколько времени?"), "negatives")

    def test_lookalike_word_is_negative(self):
        self.assertEqual(classify("Германия победила"), "negatives")

    def test_empty_is_unclear(self):
        self.assertEqual(classify(""), "unclear")
        self.assertEqual(classify("   "), "unclear")
        self.assertEqual(classify(None), "unclear")

    def test_too_short_is_unclear(self):
        """Одно-два слова без смысла — не материал для обучения."""
        self.assertEqual(classify("да"), "unclear")

    def test_whitespace_trimmed(self):
        self.assertEqual(classify("  Окей, Гермес  "), "positives")


if __name__ == "__main__":
    unittest.main()
