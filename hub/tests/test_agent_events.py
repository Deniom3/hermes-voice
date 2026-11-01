"""Протокол агента: какие события он отдаёт и как они превращаются в «делаю».

Индикация инструментов опирается на tool.start/tool.complete из WS 9119.
Если разбор этих сообщений сломается, колонка и панель одновременно
перестанут показывать, что агент делает, — молча.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hermes_ws  # noqa: E402


def msg(ptype, **payload):
    return {"params": {"type": ptype, "payload": payload}}


class NotifyTest(unittest.IsolatedAsyncioTestCase):
    async def test_none_callback_is_ignored(self):
        await hermes_ws.HermesClient._notify(None, "tool.start", {"name": "x"})

    async def test_tool_start_shape(self):
        got = []

        async def cb(ev):
            got.append(ev)

        await hermes_ws.HermesClient._notify(
            cb, "tool.start", {"tool_id": "abc", "name": "terminal", "args": {}})
        self.assertEqual(got, [{"type": "tool", "name": "terminal",
                                "id": "abc", "state": "start"}])

    async def test_tool_complete_shape(self):
        got = []

        async def cb(ev):
            got.append(ev)

        await hermes_ws.HermesClient._notify(
            cb, "tool.complete", {"tool_id": "abc", "name": "terminal"})
        self.assertEqual(got[0]["state"], "complete")

    async def test_sync_callback_supported(self):
        got = []
        await hermes_ws.HermesClient._notify(lambda ev: got.append(ev),
                                             "tool.start", {"name": "x"})
        self.assertEqual(got[0]["name"], "x")

    async def test_missing_name_falls_back(self):
        got = []

        async def cb(ev):
            got.append(ev)

        await hermes_ws.HermesClient._notify(cb, "tool.start", {"preview": "ls -la"})
        self.assertEqual(got[0]["name"], "ls -la")

        got.clear()
        await hermes_ws.HermesClient._notify(cb, "tool.start", {})
        self.assertEqual(got[0]["name"], "инструмент")

    async def test_broken_callback_does_not_break_turn(self):
        async def cb(ev):
            raise RuntimeError("клиент умер")

        await hermes_ws.HermesClient._notify(cb, "tool.start", {"name": "x"})


class TurnStreamTest(unittest.IsolatedAsyncioTestCase):
    def make_client(self, messages):
        client = hermes_ws.HermesClient()
        client.sid = "sid"
        client.ws = object()
        sent = []

        async def fake_send(method, params=None):
            sent.append((method, params))

        async def fake_recv():
            if not messages:
                raise AssertionError("поток сообщений кончился раньше времени")
            return messages.pop(0)

        client._send = fake_send
        client._recv = fake_recv
        return client, sent

    async def test_text_and_tool_events(self):
        messages = [
            msg("message.delta", text="Привет"),
            msg("tool.start", tool_id="1", name="terminal"),
            msg("tool.complete", tool_id="1", name="terminal"),
            msg("message.delta", text=", готово"),
            msg("message.complete", text=""),
            msg("turn.complete"),
        ]
        client, sent = self.make_client(messages)
        events = []

        async def cb(ev):
            events.append(ev)

        pieces = [p async for p in client._turn_stream("вопрос", on_event=cb)]
        self.assertEqual(pieces, ["Привет", ", готово"])
        self.assertEqual([e["state"] for e in events], ["start", "complete"])
        self.assertEqual(sent[0][0], "prompt.submit")

    async def test_no_events_without_callback(self):
        messages = [
            msg("tool.start", tool_id="1", name="terminal"),
            msg("message.delta", text="ок"),
            msg("turn.complete"),
        ]
        client, _ = self.make_client(messages)
        pieces = [p async for p in client._turn_stream("вопрос")]
        self.assertEqual(pieces, ["ок"])

    async def test_full_text_used_when_no_deltas(self):
        messages = [msg("message.complete", text="целиком"), msg("turn.complete")]
        client, _ = self.make_client(messages)
        pieces = [p async for p in client._turn_stream("вопрос")]
        self.assertEqual(pieces, ["целиком"])

    async def test_session_idle_ends_turn(self):
        messages = [msg("message.delta", text="а"), msg("session.idle")]
        client, _ = self.make_client(messages)
        pieces = [p async for p in client._turn_stream("вопрос")]
        self.assertEqual(pieces, ["а"])


class StreamSignatureTest(unittest.TestCase):
    def test_stream_accepts_events(self):
        import inspect
        self.assertIn("on_event", inspect.signature(hermes_ws.stream).parameters)
        self.assertIn("on_event",
                      inspect.signature(hermes_ws.HermesClient.stream).parameters)


if __name__ == "__main__":
    unittest.main()
