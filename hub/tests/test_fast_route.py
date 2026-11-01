"""Обёртка fast_route: команда молчит, показание озвучивается, сбой — к агенту."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fast_route  # noqa: E402
import voice_dialog  # noqa: E402


class FakeRoute:
    def __init__(self, answer, kind):
        self.answer = answer
        self.last_kind = kind

    async def route(self, text, area=None):
        return self.answer


class BrokenRoute:
    last_kind = None

    async def route(self, text, area=None):
        raise RuntimeError("HA недоступен")


class FastRouteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_route = fast_route._route
        self.old_failed = fast_route._failed
        self.old_stream = fast_route.hermes_ws.stream

        async def agent_stream(text, on_event=None):
            yield "ответ агента"

        fast_route.hermes_ws.stream = agent_stream

    async def asyncTearDown(self):
        fast_route._route = self.old_route
        fast_route._failed = self.old_failed
        fast_route.hermes_ws.stream = self.old_stream

    async def collect(self, text):
        return [p async for p in fast_route.stream(text)]

    async def test_command_is_silent(self):
        fast_route._route = FakeRoute("Включил", "command")
        fast_route._failed = False
        self.assertEqual(await self.collect("включи лампу"), [voice_dialog.SILENT])

    async def test_reading_is_spoken(self):
        fast_route._route = FakeRoute("Температура: 25,6 градуса", "reading")
        fast_route._failed = False
        self.assertEqual(await self.collect("какая температура"),
                         ["Температура: 25,6 градуса"])

    async def test_unknown_goes_to_agent(self):
        fast_route._route = FakeRoute(None, None)
        fast_route._failed = False
        self.assertEqual(await self.collect("расскажи анекдот"), ["ответ агента"])

    async def test_broken_route_goes_to_agent(self):
        fast_route._route = BrokenRoute()
        fast_route._failed = False
        self.assertEqual(await self.collect("какая температура"), ["ответ агента"])

    async def test_disabled_without_env(self):
        fast_route._route = None
        fast_route._failed = False
        old_url = os.environ.pop("HASS_URL", None)
        old_token = os.environ.pop("HASS_TOKEN", None)
        try:
            self.assertIsNone(fast_route.route())
            self.assertEqual(await self.collect("какая температура"), ["ответ агента"])
        finally:
            if old_url is not None:
                os.environ["HASS_URL"] = old_url
            if old_token is not None:
                os.environ["HASS_TOKEN"] = old_token


if __name__ == "__main__":
    unittest.main()
