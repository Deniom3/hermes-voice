import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ma_control  # noqa: E402


class ParseMusicCommandTest(unittest.TestCase):
    def test_stop(self):
        self.assertEqual(ma_control.parse("стоп"), ("stop", None))
        self.assertEqual(ma_control.parse("выключи музыку"), ("stop", None))

    def test_transport(self):
        self.assertEqual(ma_control.parse("пауза"), ("pause", None))
        self.assertEqual(ma_control.parse("продолжай"), ("play", None))
        self.assertEqual(ma_control.parse("следующий трек"), ("next", None))
        self.assertEqual(ma_control.parse("предыдущий"), ("previous", None))

    def test_seek(self):
        self.assertEqual(ma_control.parse("перемотай на 30 секунд"), ("seek", 30))
        self.assertEqual(ma_control.parse("перемотай на 2 минуты"), ("seek", 120))
        self.assertEqual(ma_control.parse("в начало"), ("seek", 0))

    def test_not_music(self):
        self.assertIsNone(ma_control.parse("включи свет"))
        self.assertIsNone(ma_control.parse("какая температура"))
        self.assertIsNone(ma_control.parse("стоп, а который час?"))

    def test_polite_forms(self):
        """Обращение и «пожалуйста» не должны мешать команде."""
        self.assertEqual(ma_control.parse("Гермес, поставь на паузу, пожалуйста"),
                         ("pause", None))
        self.assertEqual(ma_control.parse("Алиса, выключи музыку, плиз"),
                         ("stop", None))
        self.assertEqual(ma_control.parse("ну давай дальше"), ("next", None))


class FakeMA:
    def __init__(self):
        self.calls = []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return {}


class TransportTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = FakeMA()
        self.orig_ma = ma_control.MA
        self.orig_player = ma_control._player
        ma_control._PAUSED_BY_HUB.clear()

    def tearDown(self):
        ma_control.MA = self.orig_ma
        ma_control._player = self.orig_player
        ma_control._PAUSED_BY_HUB.clear()

    async def test_pause_only_playing(self):
        ma_control.MA = self.fake

        async def playing(_station):
            return {"player_id": "p1", "state": "playing"}

        ma_control._player = playing
        self.assertTrue(await ma_control.pause_if_playing("lite-1"))
        self.assertEqual(self.fake.calls[0][1]["params"]["command"], "pause")

        async def idle(_station):
            return {"player_id": "p1", "state": "idle"}

        ma_control._player = idle
        self.fake.calls.clear()
        self.assertFalse(await ma_control.pause_if_playing("lite-1"))
        self.assertEqual(self.fake.calls, [])

    async def test_resume_only_paused(self):
        ma_control.MA = self.fake

        async def paused(_station):
            return {"player_id": "p1", "state": "paused"}

        ma_control._player = paused
        self.assertTrue(await ma_control.resume_if_paused("lite-1"))
        self.assertEqual(self.fake.calls[0][1]["params"]["command"], "play")

        async def playing(_station):
            return {"player_id": "p1", "state": "playing"}

        ma_control._player = playing
        self.fake.calls.clear()
        self.assertFalse(await ma_control.resume_if_paused("lite-1"))
        self.assertEqual(self.fake.calls, [])

    async def test_resume_after_idle_when_paused_by_hub(self):
        """MA после pause может отдавать idle — флаг хаба всё равно возвращает."""
        ma_control.MA = self.fake

        async def playing(_station):
            return {"player_id": "p1", "state": "playing"}

        async def idle(_station):
            return {"player_id": "p1", "state": "idle"}

        ma_control._player = playing
        self.assertTrue(await ma_control.pause_if_playing("lite-1"))
        ma_control._player = idle
        self.fake.calls.clear()
        self.assertTrue(await ma_control.resume_if_paused("lite-1"))
        self.assertEqual(self.fake.calls[0][1]["params"]["command"], "play")

    async def test_volume_uses_same_player(self):
        ma_control.MA = self.fake

        async def player(_station):
            return {"player_id": "p1", "state": "playing", "volume_level": 10}

        ma_control._player = player
        self.assertTrue(await ma_control.set_volume("lite-1", 42))
        name, args = self.fake.calls[0]
        self.assertEqual(name, "ma_volume")
        self.assertEqual(args["params"]["player_id"], "p1")
        self.assertEqual(args["params"]["level"], 42)

    async def test_volume_skips_same_level(self):
        """Ответ MA на нашу же команду не должен запускать петлю."""
        ma_control.MA = self.fake

        async def player(_station):
            return {"player_id": "p1", "state": "playing", "volume_level": 42}

        ma_control._player = player
        self.assertFalse(await ma_control.set_volume("lite-1", 42))
        self.assertEqual(self.fake.calls, [])

    async def test_volume_force_ignores_stale_same_level(self):
        """Событие от агента должно дойти до MA даже при старом кэше 42."""
        ma_control.MA = self.fake

        async def player(_station):
            return {"player_id": "p1", "state": "playing", "volume_level": 42}

        ma_control._player = player
        self.assertTrue(await ma_control.set_volume("lite-1", 30, force=True))
        self.assertEqual(self.fake.calls[0][1]["params"]["level"], 30)


    async def test_queue_len_unknown_when_payload_broken(self):
        """Битый ответ MA — «не знаем», а не «пусто»: иначе кнопка уйдёт maind."""
        ma_control.MA = self.fake

        async def player(_station):
            return {"player_id": "p1", "state": "paused"}

        ma_control._player = player
        self.assertEqual(await ma_control.queue_len("lite-1"), -1)


if __name__ == "__main__":
    unittest.main()
