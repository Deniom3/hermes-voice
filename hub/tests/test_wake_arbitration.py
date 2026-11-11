"""Арбитраж активации между колонками.

Проверяем то, что видели в живом логе: две станции слышат одну фразу и обе
запускают ход. Отвечать должна одна — та, у которой сигнал громче.
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_hub  # noqa: E402
from voice_core import SpeechCore, State  # noqa: E402


class FakeConn:
    def __init__(self, name, state=State.IDLE, origin=None):
        self.name = name
        self.role = "station"
        self.core = SimpleNamespace(state=state, origin=origin)


class ArbitrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        voice_hub.CONNS.clear()
        self.arb = voice_hub.WakeArbitration(settle=0.05)

    def tearDown(self):
        voice_hub.CONNS.clear()

    async def test_single_station_wins_immediately(self):
        voice_hub.CONNS["lite-1"] = FakeConn("lite-1")
        self.assertTrue(await self.arb.claim("lite-1", 100.0))

    async def test_louder_station_wins(self):
        voice_hub.CONNS["lite-1"] = FakeConn("lite-1")
        voice_hub.CONNS["lite-2"] = FakeConn("lite-2")
        far, near = await asyncio.gather(
            self.arb.claim("lite-1", 40.0),
            self.arb.claim("lite-2", 120.0),
        )
        self.assertFalse(far)
        self.assertTrue(near)

    async def test_echo_of_same_turn_does_not_restart(self):
        """Эхо той же фразы не запускает второй ход на другой станции."""
        voice_hub.CONNS["lite-1"] = FakeConn("lite-1")
        voice_hub.CONNS["lite-2"] = FakeConn("lite-2")
        far, near = await asyncio.gather(
            self.arb.claim("lite-1", 40.0),
            self.arb.claim("lite-2", 120.0),
        )
        self.assertFalse(far)
        self.assertTrue(near)
        # Через мгновение вторая станция ловит эхо той же фразы — отказ.
        self.assertFalse(await self.arb.claim("lite-1", 500.0))


class CancelWakeTest(unittest.IsolatedAsyncioTestCase):
    def _core(self, state, origin="wake"):
        core = object.__new__(SpeechCore)
        core.state = state
        core.origin = origin
        core.coll = object()
        core.prefix = b"abc"
        core.events = []

        async def on_state(st, org, score):
            core.events.append((st, org, score))

        core.on_state = on_state
        return core

    async def test_cancel_wake_returns_to_idle(self):
        core = self._core(State.COLLECTING)
        await core.cancel_wake()
        self.assertIs(core.state, State.IDLE)
        self.assertIsNone(core.coll)
        self.assertEqual(core.prefix, b"")
        self.assertEqual(core.events[-1][:2], (State.IDLE, "arbitration"))

    async def test_cancel_wake_ignores_busy(self):
        core = self._core(State.BUSY, origin=None)
        await core.cancel_wake()
        self.assertIs(core.state, State.BUSY)
        self.assertEqual(core.events, [])


if __name__ == "__main__":
    unittest.main()
