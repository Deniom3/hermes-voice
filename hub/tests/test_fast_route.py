"""Обёртка fast_route: команда молчит, показание озвучивается, сбой — к агенту."""
from __future__ import annotations

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fast_route  # noqa: E402
import voice_dialog  # noqa: E402


class FakeRoute:
    def __init__(self, answer, kind):
        self.answer = answer
        self.last_kind = kind

    async def route(self, text, area=None, station=None):
        return self.answer


class BrokenRoute:
    last_kind = None

    async def route(self, text, area=None, station=None):
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


class SilentCommandTest(unittest.IsolatedAsyncioTestCase):
    """Недопустимая громкость: команда «выполнена», но вслух ничего и не к агенту."""

    async def test_empty_command_answer_is_silent(self):
        old_route, old_failed = fast_route._route, fast_route._failed
        called = {"agent": False}

        async def agent_stream(text, on_event=None):
            called["agent"] = True
            yield "ответ агента"

        old_stream = fast_route.hermes_ws.stream
        fast_route.hermes_ws.stream = agent_stream
        fast_route._route = FakeRoute("", "command")
        try:
            out = [chunk async for chunk in fast_route.stream("громкость 70")]
        finally:
            fast_route._route, fast_route._failed = old_route, old_failed
            fast_route.hermes_ws.stream = old_stream
        self.assertEqual(out, [voice_dialog.SILENT])
        self.assertFalse(called["agent"], "фраза не должна уходить агенту")


class VolumeSourceWiringTest(unittest.TestCase):
    """Быстрый маршрут получает фактическую громкость от хаба, а не из HA."""

    def test_route_gets_station_volume_callback(self):
        old_env = {k: os.environ.get(k) for k in ("HASS_URL", "HASS_TOKEN")}
        os.environ["HASS_URL"] = "http://example.invalid"
        os.environ["HASS_TOKEN"] = "test-token"
        old_route, old_failed = fast_route._route, fast_route._failed
        old_hub = sys.modules.get("voice_hub")
        sentinel = object()
        sys.modules["voice_hub"] = types.SimpleNamespace(station_volume=sentinel)
        fast_route._route, fast_route._failed = None, False
        try:
            route = fast_route.route()
            self.assertIsNotNone(route, "маршрут не создался")
            self.assertIs(route.volume_source, sentinel)
        finally:
            fast_route._route, fast_route._failed = old_route, old_failed
            if old_hub is None:
                sys.modules.pop("voice_hub", None)
            else:
                sys.modules["voice_hub"] = old_hub
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
