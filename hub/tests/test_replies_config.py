"""Реплики и группы слов живут в hub.json, а не строками в коде.

Тест ловит два класса поломок:
1. код просит реплику, которой нет в конфиге — на живой фразе это RuntimeError
   вместо ответа;
2. код снова обзавёлся русским текстом в возврате — значит вынос в конфиг
   откатили и правка фразы опять требует релиза.
"""
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "hub"))

import ha_route  # noqa: E402
import voice_dialog  # noqa: E402

EXAMPLE = os.path.join(ROOT, "hub", "config.example.json")
SOURCE = os.path.join(ROOT, "hub", "ha_route.py")


def load_example() -> dict:
    with open(EXAMPLE, encoding="utf-8") as f:
        return json.load(f)


class ReplyKeysTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_example()
        with open(SOURCE, encoding="utf-8") as f:
            self.src = f.read()

    def test_every_requested_reply_exists(self):
        keys = set(re.findall(r'_re(?:ply|_fmt)\(\s*"([a-z_]+)"', self.src))
        self.assertGreaterEqual(len(keys), 10)
        missing = sorted(k for k in keys if k not in self.cfg["replies"])
        self.assertEqual(missing, [], f"нет в replies: {missing}")

    def test_templates_have_expected_placeholders(self):
        r = self.cfg["replies"]
        for key, fields in {
            "volume": {"level"},
            "brightness": {"pct", "device"},
            "temperature": {"value", "unit"},
            "unavailable": {"device"},
        }.items():
            with self.subTest(key=key):
                found = set(re.findall(r"{(\w+)}", r[key]))
                self.assertEqual(found, fields)

    def test_no_hardcoded_answers_left(self):
        # Возврат русского текста из маршрута — признак, что реплику снова
        # зашили в код. Комментарии и логи не считаем: берём только return.
        bad = [m.group(1) for m in re.finditer(
            r'return\s+f?"([^"]*[А-Яа-яЁё][^"]*)"', self.src)]
        self.assertEqual(bad, [], f"русский текст в return: {bad}")

    def test_missing_reply_raises(self):
        with self.assertRaises(RuntimeError):
            ha_route._reply("no_such_reply_key")

    def test_no_area_has_text(self):
        self.assertTrue(self.cfg["replies"]["no_area"].strip())


class WordGroupsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_example()["phrases"]

    def test_route_groups_present(self):
        for key in ("command_extra", "volume_query", "volume_max",
                    "role_vacuum", "role_climate", "role_water", "role_light"):
            with self.subTest(key=key):
                self.assertTrue(self.cfg.get(key), f"phrases.{key} пуст")

    def test_question_words(self):
        words = self.cfg["question_words"]
        self.assertIn("что", words)
        self.assertIn("сколько", words)
        self.assertIn(tuple(words), (voice_dialog._QUESTION_WORDS, ))

    def test_role_words_reach_matcher(self):
        # Ключевые слова ролей должны попадать в разбор фразы.
        self.assertIn("пылесос", ha_route.ROLE_VACUUM_WORDS)
        self.assertIn("свет", ha_route.ROLE_LIGHT_WORDS)


if __name__ == "__main__":
    unittest.main()
