"""Матрица автомата источников: каждая клетка отдельным кейсом."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sources as S  # noqa: E402


def src(**kw):
    return S.Sources(**kw)


class WakeTest(unittest.TestCase):
    def test_alice_playing_pauses_alice(self):
        d = S.decide(src(alice=S.PLAYING, owner=S.ALICE), "wake")
        self.assertEqual(d.actions, [S.PAUSE_ALICE])
        self.assertEqual(d.sources.alice, S.PAUSED)
        self.assertEqual(d.sources.pause_reason, S.REASON_HUB)

    def test_ma_playing_pauses_ma_and_agent(self):
        d = S.decide(src(ma=S.PLAYING, agent=S.STREAMING, owner=S.MA), "wake")
        self.assertEqual(d.actions, [S.PAUSE_MA, S.PAUSE_AGENT])
        self.assertEqual(d.sources.ma, S.PAUSED)
        self.assertEqual(d.sources.owner, S.MA)

    def test_silence_does_nothing(self):
        d = S.decide(src(), "wake")
        self.assertEqual(d.actions, [])
        self.assertEqual(d.sources.pause_reason, "none")


class ResponseEndTest(unittest.TestCase):
    def test_returns_alice_after_hub_pause(self):
        d = S.decide(src(alice=S.PAUSED, owner=S.ALICE,
                         pause_reason=S.REASON_HUB), "response_end")
        self.assertEqual(d.actions, [S.PLAY_ALICE])
        self.assertEqual(d.sources.alice, S.PLAYING)

    def test_returns_ma_after_hub_pause(self):
        d = S.decide(src(ma=S.PAUSED, agent=S.PAUSED, owner=S.MA,
                         pause_reason=S.REASON_HUB), "response_end")
        self.assertEqual(d.actions, [S.PLAY_MA, S.PLAY_AGENT])
        self.assertEqual(d.sources.pause_reason, "none")

    def test_does_not_touch_button_pause(self):
        d = S.decide(src(ma=S.PAUSED, owner=S.MA,
                         pause_reason=S.REASON_BUTTON), "response_end")
        self.assertEqual(d.actions, [])


class ButtonTest(unittest.TestCase):
    def test_ma_playing_pauses_ma(self):
        d = S.decide(src(ma=S.PLAYING, agent=S.STREAMING, owner=S.MA), "button")
        self.assertEqual(d.actions, [S.PAUSE_MA, S.PAUSE_SPIN])
        self.assertEqual(d.sources.pause_reason, S.REASON_BUTTON)
        self.assertEqual(d.sources.owner, S.MA)

    def test_ma_paused_plays_ma(self):
        d = S.decide(src(ma=S.PAUSED, owner=S.MA), "button")
        self.assertEqual(d.actions, [S.PLAY_MA, S.PLAY_AGENT])

    def test_ma_paused_by_app_also_plays(self):
        """Пауза из приложения/с ПК — тоже валидное состояние для toggle."""
        d = S.decide(src(ma=S.PAUSED, owner=S.MA,
                         pause_reason=S.REASON_APP), "button")
        self.assertEqual(d.actions, [S.PLAY_MA, S.PLAY_AGENT])

    def test_alice_playing_pauses_everything(self):
        """Кнопка наша: пауза = тишина, гасим и натив, и очередь."""
        d = S.decide(src(alice=S.PLAYING, owner=S.ALICE), "button")
        self.assertEqual(d.actions, [S.PAUSE_ALICE, S.PAUSE_MA, S.PAUSE_SPIN])
        self.assertEqual(d.sources.alice, S.PAUSED)
        self.assertEqual(d.sources.owner, S.ALICE)

    def test_alice_and_ma_playing_pauses_both(self):
        d = S.decide(src(alice=S.PLAYING, ma=S.PLAYING, agent=S.STREAMING,
                         owner=S.ALICE), "button")
        self.assertEqual(d.actions, [S.PAUSE_ALICE, S.PAUSE_MA, S.PAUSE_SPIN])
        self.assertEqual(d.sources.ma, S.PAUSED)

    def test_silence_plays_alice(self):
        """В тишине нажатие включает Алису — maind кнопку больше не видит."""
        d = S.decide(src(), "button")
        self.assertEqual(d.actions, [S.PLAY_ALICE])
        self.assertEqual(d.sources.alice, S.PLAYING)

    def test_empty_queue_plays_alice(self):
        d = S.decide(src(ma=S.PAUSED, owner=S.MA, ma_queue="empty"), "button")
        self.assertEqual(d.actions, [S.PLAY_ALICE])

    def test_silence_with_ma_owner_plays_ma(self):
        d = S.decide(src(ma=S.PAUSED, owner=S.MA, ma_queue="filled"), "button")
        self.assertEqual(d.actions, [S.PLAY_MA, S.PLAY_AGENT])
        self.assertEqual(d.sources.owner, S.MA)

    def test_button_during_dialog_is_silence(self):
        """Кнопка во время ответа — тишина: обрываем речь, музыку не поднимаем."""
        d = S.decide(src(ma=S.PAUSED, agent=S.STREAMING, owner=S.MA,
                         pause_reason=S.REASON_HUB, ma_queue="filled"), "button")
        self.assertEqual(d.actions, [S.STOP_AGENT])
        self.assertEqual(d.sources.ma, S.PAUSED)

    def test_button_during_dialog_keeps_alice_paused(self):
        d = S.decide(src(alice=S.PAUSED, owner=S.ALICE,
                         pause_reason=S.REASON_HUB), "button")
        self.assertEqual(d.actions, [S.STOP_AGENT])
        self.assertEqual(d.sources.alice, S.PAUSED)

    def test_second_press_after_dialog_returns_owner(self):
        """Второе нажатие в тишине возвращает того, кто был владельцем."""
        d = S.decide(src(ma=S.PAUSED, owner=S.MA, ma_queue="filled"), "button")
        self.assertEqual(d.actions, [S.PLAY_MA, S.PLAY_AGENT])

    def test_button_during_dialog_in_silence_only_stops(self):
        """Если до хода ничего не звучало — только обрываем ответ."""
        d = S.decide(src(agent=S.STREAMING, pause_reason=S.REASON_HUB), "button")
        self.assertEqual(d.actions, [S.STOP_AGENT])


class SwitchAliceTest(unittest.TestCase):
    def test_long_press_switches_ma_to_alice(self):
        d = S.decide(src(ma=S.PLAYING, agent=S.STREAMING, owner=S.MA,
                         ma_queue="filled"), "switch_alice")
        self.assertEqual(d.actions, [S.PAUSE_MA, S.PAUSE_AGENT, S.PLAY_ALICE])
        self.assertEqual(d.sources.owner, S.ALICE)
        self.assertEqual(d.sources.ma, S.PAUSED)
        self.assertEqual(d.sources.alice, S.PLAYING)

    def test_long_press_stops_agent_buffer_without_ma(self):
        d = S.decide(src(ma=S.PAUSED, agent=S.STREAMING, owner=S.MA),
                     "switch_alice")
        self.assertEqual(d.actions, [S.PAUSE_AGENT, S.PLAY_ALICE])
        self.assertEqual(d.sources.owner, S.ALICE)

    def test_long_press_in_silence_plays_alice(self):
        d = S.decide(src(), "switch_alice")
        self.assertEqual(d.actions, [S.PLAY_ALICE])

    def test_reason_cleared(self):
        d = S.decide(src(alice=S.PAUSED, owner=S.ALICE,
                         pause_reason=S.REASON_HUB), "switch_alice")
        self.assertEqual(d.sources.pause_reason, "none")

    def test_long_press_switches_alice_to_ma(self):
        """Обратный переход тем же жестом: Алиса → MA."""
        d = S.decide(src(alice=S.PLAYING, owner=S.ALICE,
                         ma_queue="filled"), "switch_source")
        self.assertEqual(d.actions, [S.PAUSE_ALICE, S.PLAY_MA, S.PLAY_AGENT])
        self.assertEqual(d.sources.owner, S.MA)
        self.assertEqual(d.sources.ma, S.PLAYING)

    def test_long_press_does_not_kill_alice_when_queue_empty(self):
        d = S.decide(src(alice=S.PLAYING, owner=S.ALICE,
                         ma_queue="empty"), "switch_source")
        self.assertEqual(d.actions, [])
        self.assertEqual(d.sources.alice, S.PLAYING)

    def test_long_press_empty_queue_switches_to_cloud(self):
        """Продолжать локальную очередь нечем — уходим в облако, не в тишину."""
        d = S.decide(src(alice=S.PAUSED, owner=S.ALICE,
                         ma_queue="empty"), "switch_source")
        self.assertEqual(d.actions, [S.PLAY_ALICE])
        self.assertEqual(d.sources.owner, S.ALICE)
        self.assertEqual(d.sources.alice, S.PLAYING)

    def test_long_press_from_paused_alice_goes_to_ma(self):
        d = S.decide(src(alice=S.PAUSED, owner=S.ALICE,
                         ma_queue="filled"), "switch_source")
        self.assertEqual(d.actions, [S.PLAY_MA, S.PLAY_AGENT])
        self.assertEqual(d.sources.owner, S.MA)

    def test_long_press_during_dialog_stops_answer_first(self):
        d = S.decide(src(ma=S.PAUSED, agent=S.STREAMING, owner=S.MA,
                         pause_reason=S.REASON_HUB, ma_queue="filled"),
                     "switch_source")
        self.assertEqual(d.actions[0], S.STOP_AGENT)
        self.assertEqual(d.sources.owner, S.ALICE)


class OverlapTest(unittest.TestCase):
    def test_alice_started_pauses_ma_and_stream(self):
        """Алиса перебила MA — гасим очередь и наш aplay, иначе буфер играет."""
        d = S.decide(src(ma=S.PLAYING, agent=S.STREAMING, owner=S.MA),
                     "alice_started")
        self.assertEqual(d.actions, [S.PAUSE_MA, S.PAUSE_SPIN])
        self.assertEqual(d.sources.owner, S.ALICE)

    def test_alice_started_when_ma_paused_does_nothing(self):
        d = S.decide(src(ma=S.PAUSED, owner=S.MA), "alice_started")
        self.assertEqual(d.actions, [])
        self.assertEqual(d.sources.owner, S.ALICE)

    def test_ma_started_pauses_alice(self):
        d = S.decide(src(alice=S.PLAYING, owner=S.ALICE), "ma_started")
        self.assertEqual(d.actions, [S.PAUSE_ALICE])
        self.assertEqual(d.sources.owner, S.MA)

    def test_poll_pauses_only_one_side(self):
        d = S.decide(src(alice=S.PLAYING, ma=S.PLAYING, agent=S.STREAMING,
                         owner=S.ALICE), "poll")
        self.assertEqual(d.actions, [S.PAUSE_MA, S.PAUSE_SPIN])
        self.assertEqual(d.sources.alice, S.PLAYING)

    def test_poll_is_idempotent(self):
        """После гашения второй источник уже paused — повтора нет."""
        d1 = S.decide(src(alice=S.PLAYING, ma=S.PLAYING, agent=S.STREAMING,
                          owner=S.MA), "poll")
        self.assertEqual(d1.actions, [S.PAUSE_ALICE])
        d2 = S.decide(d1.sources, "poll")
        self.assertEqual(d2.actions, [])

    def test_poll_raises_agent_when_ma_plays(self):
        d = S.decide(src(ma=S.PLAYING, agent=S.IDLE, owner=S.MA), "poll")
        self.assertEqual(d.actions, [S.PLAY_AGENT])


class StopTest(unittest.TestCase):
    def test_stop_returns_music_paused_by_hub(self):
        d = S.decide(src(ma=S.PAUSED, agent=S.PAUSED, owner=S.MA,
                         pause_reason=S.REASON_HUB), "stop")
        self.assertEqual(d.actions, [S.STOP_AGENT, S.PLAY_MA, S.PLAY_AGENT])

    def test_stop_does_not_touch_button_pause(self):
        d = S.decide(src(ma=S.PAUSED, owner=S.MA,
                         pause_reason=S.REASON_BUTTON), "stop")
        self.assertEqual(d.actions, [S.STOP_AGENT])

    def test_stop_in_silence_only_stops_agent(self):
        d = S.decide(src(), "stop")
        self.assertEqual(d.actions, [S.STOP_AGENT])


class InvariantsTest(unittest.TestCase):
    def test_no_event_leaves_two_sources_playing(self):
        """Инвариант: после любого события владелец ровно один."""
        states = [S.IDLE, S.PLAYING, S.PAUSED]
        events = ["wake", "response_end", "button", "alice_started",
                  "ma_started", "stop", "poll"]
        owners = ["none", S.ALICE, S.MA]
        reasons = ["none", S.REASON_HUB, S.REASON_BUTTON, S.REASON_OVERLAP]
        for a in states:
            for m in states:
                for o in owners:
                    for r in reasons:
                        base = src(alice=a, ma=m, owner=o, pause_reason=r)
                        for e in events:
                            d = S.decide(base, e)
                            playing = [x for x in
                                       (d.sources.alice, d.sources.ma)
                                       if x == S.PLAYING]
                            self.assertLessEqual(
                                len(playing), 1,
                                f"два источника играют: {base} + {e}")

    def test_unknown_event_noop(self):
        d = S.decide(src(alice=S.PLAYING), "какое-то")
        self.assertEqual(d.actions, [])
        self.assertEqual(d.sources.alice, S.PLAYING)


if __name__ == "__main__":
    unittest.main()
