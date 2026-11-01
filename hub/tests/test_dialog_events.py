"""Ход диалога: проброс событий агента и совместимость с разными stream_fn."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_dialog  # noqa: E402


class AcceptsEventsTest(unittest.TestCase):
    def test_plain_function_rejected(self):
        def stream(text):
            yield text

        self.assertFalse(voice_dialog.accepts_events(stream))

    def test_keyword_argument_accepted(self):
        def stream(text, on_event=None):
            yield text

        self.assertTrue(voice_dialog.accepts_events(stream))

    def test_var_keyword_accepted(self):
        def stream(text, **kwargs):
            yield text

        self.assertTrue(voice_dialog.accepts_events(stream))

    def test_module_stream_function(self):
        import hermes_ws
        self.assertTrue(voice_dialog.accepts_events(hermes_ws.stream))


class AgentEventTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_event_forwarded(self):
        got = []

        async def on_event(ev):
            got.append(ev)

        turn = voice_dialog.DialogTurn("вопрос", on_event=on_event)
        await turn._agent_event({"type": "tool", "name": "terminal",
                                 "id": "1", "state": "start"})
        self.assertEqual(got[0]["type"], "tool")
        self.assertEqual(got[0]["name"], "terminal")
        self.assertEqual(got[0]["state"], "start")

    async def test_unknown_event_ignored(self):
        got = []

        async def on_event(ev):
            got.append(ev)

        turn = voice_dialog.DialogTurn("вопрос", on_event=on_event)
        await turn._agent_event({"type": "message.delta", "text": "x"})
        self.assertEqual(got, [])

    async def test_no_callback_does_not_crash(self):
        turn = voice_dialog.DialogTurn("вопрос")
        await turn._agent_event({"type": "tool", "name": "x", "state": "start"})


class SplitSentencesTest(unittest.TestCase):
    def test_long_text_is_split(self):
        text = "Первое длинное предложение для озвучки. Второе предложение. Третье."
        first, rest = voice_dialog.split_sentences(text)
        self.assertTrue(first)
        self.assertIn("Второе", rest)

    def test_incomplete_sentence_stays_in_buffer(self):
        first, rest = voice_dialog.split_sentences("Незаконченная мысль без точки")
        self.assertEqual(first, "")
        self.assertEqual(rest, "Незаконченная мысль без точки")

    def test_empty_text(self):
        self.assertEqual(voice_dialog.split_sentences(""), ("", ""))


if __name__ == "__main__":
    unittest.main()
