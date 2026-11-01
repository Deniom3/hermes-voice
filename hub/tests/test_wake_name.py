"""Обращение к ассистенту по тексту фразы.

Проверка решает, отвечать или промолчать, поэтому ошибаться в обе стороны
дорого: пропуск ломает управление, лишнее «да» — отвечает на болтовню.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_core import name_like  # noqa: E402


class NameLikeTest(unittest.TestCase):
    def test_clean_name(self):
        self.assertTrue(name_like("Окей, Гермес"))
        self.assertTrue(name_like("Гермес, сколько времени?"))

    def test_asr_distortions_pass(self):
        """Реальные искажения распознавания не должны отбиваться."""
        for text in ("Окей Гермец", "гермез", "хермес", "дерми включи музыку",
                     "О'кей, Гермец."):
            with self.subTest(text=text):
                self.assertTrue(name_like(text))

    def test_lookalike_words_rejected(self):
        """Похожие слова — не имя: иначе ассистент отвечает на «Германию»."""
        for text in ("Германия победила", "герметик купи", "германий редкий",
                     "капец как смешно"):
            with self.subTest(text=text):
                self.assertFalse(name_like(text))

    def test_plain_speech_rejected(self):
        for text in ("Сколько времени?", "Помнишь?", "включи музыку",
                     "какая сегодня погода"):
            with self.subTest(text=text):
                self.assertFalse(name_like(text))

    def test_empty_and_short(self):
        self.assertFalse(name_like(""))
        self.assertFalse(name_like(None))
        self.assertFalse(name_like("ну и"))

    def test_name_at_start_of_long_phrase(self):
        self.assertTrue(name_like("Окей, Гермес. Сколько времени?"))

    def test_latin_and_mixed_case(self):
        self.assertTrue(name_like("Hermes, what time is it"))
        self.assertTrue(name_like("ОКЕЙ ГЕРМЕС"))

    def test_yo_letter_normalised(self):
        """«ё» и «е» не должны ломать сравнение."""
        self.assertTrue(name_like("Гермёс"))


if __name__ == "__main__":
    unittest.main()
