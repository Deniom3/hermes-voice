"""Служебные тексты шлюза не должны попадать в озвучку."""
import unittest

import answer_filter as F


class TechnicalTest(unittest.TestCase):
    def test_interrupted_retry_is_technical(self):
        self.assertTrue(F.is_technical(
            "Operation interrupted: retrying API call after error (retry 1/3)."))

    def test_normal_answer_passes(self):
        self.assertFalse(F.is_technical("Включил свет в спальне."))

    def test_answer_about_error_passes(self):
        """Слово «ошибка» в человеческом ответе — не повод его глотать."""
        self.assertFalse(F.is_technical(
            "В логах была ошибка подключения, но сейчас всё работает."))

    def test_traceback_is_technical(self):
        self.assertTrue(F.is_technical(
            "Traceback (most recent call last):\n  File \"x.py\", line 1"))

    def test_rate_limit_is_technical(self):
        self.assertTrue(F.is_technical("Rate limit exceeded, try later"))

    def test_empty_is_not_technical(self):
        self.assertFalse(F.is_technical(""))

    def test_clean_replaces_technical(self):
        self.assertEqual(F.clean("Error: boom", "не получилось"),
                         "не получилось")

    def test_clean_keeps_normal(self):
        self.assertEqual(F.clean("Доброе утро", "не получилось"),
                         "Доброе утро")
