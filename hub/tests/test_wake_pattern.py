"""Корни имени и адрес Hermes — из настроек, а не из литералов в коде.

Проверяем:
1. Регулярка обращения строится из `wake.names` и ловит искажения ASR.
2. Склейка «Гермес, стоп» в одно слово не считается обращением.
3. `hermes.url` читается из hub.json; env — только явное переопределение.
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import wake_names  # noqa: E402


class WakeRootsTest(unittest.TestCase):
    def test_roots_from_config(self):
        roots = wake_names.roots()
        self.assertIn("герм", roots)
        self.assertIn("herm", roots)

    def test_pattern_matches_asr_distortions(self):
        import voice_dialog

        for text in ("Окей, Гермез, включи лампу",
                     "Гермяс, который час",
                     "hermes, свет",
                     "Ермес, стоп"):
            with self.subTest(text=text):
                self.assertTrue(voice_dialog.has_wake_name(text))

    def test_pattern_ignores_plain_speech(self):
        import voice_dialog

        self.assertFalse(voice_dialog.has_wake_name("включи лампу"))
        self.assertFalse(voice_dialog.has_wake_name("сколько времени"))

    def test_glued_name_is_not_a_name(self):
        """«герместоп» — склейка имени с командой: имя срезать нельзя."""
        import voice_dialog

        self.assertTrue(wake_names.is_stem("гермес"))
        self.assertFalse(voice_dialog._name_stem("герместоп"))


class HermesUrlTest(unittest.TestCase):
    def test_url_comes_from_hub_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "hub.json"
            path.write_text(json.dumps({"hermes": {"url": "ws://10.0.0.5:9200/api/ws"}}),
                            encoding="utf-8")
            old = os.environ.get("HUB_CONFIG")
            os.environ["HUB_CONFIG"] = str(path)
            os.environ.pop("HERMES_WS_URL", None)
            try:
                importlib.reload(config)
                import hermes_ws
                mod = importlib.reload(hermes_ws)
                self.assertEqual(mod.URL, "ws://10.0.0.5:9200/api/ws")
            finally:
                if old is None:
                    os.environ.pop("HUB_CONFIG", None)
                else:
                    os.environ["HUB_CONFIG"] = old
                importlib.reload(config)
                import hermes_ws
                importlib.reload(hermes_ws)

    def test_env_overrides_hub_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "hub.json"
            path.write_text(json.dumps({"hermes": {"url": "ws://json.invalid/api/ws"}}),
                            encoding="utf-8")
            old = os.environ.get("HUB_CONFIG")
            os.environ["HUB_CONFIG"] = str(path)
            os.environ["HERMES_WS_URL"] = "ws://10.0.0.5:9200/api/ws"
            try:
                importlib.reload(config)
                import hermes_ws
                mod = importlib.reload(hermes_ws)
                self.assertEqual(mod.URL, "ws://10.0.0.5:9200/api/ws")
            finally:
                if old is None:
                    os.environ.pop("HUB_CONFIG", None)
                else:
                    os.environ["HUB_CONFIG"] = old
                os.environ.pop("HERMES_WS_URL", None)
                importlib.reload(config)
                import hermes_ws
                importlib.reload(hermes_ws)


if __name__ == "__main__":
    unittest.main()
