"""Отмена хода: озвучка не должна продолжаться после перебивания.

Ход отменяется (barge, «стоп», кнопка), но синтез и плеер — отдельные
задачи внутри DialogTurn. Если их не отменить, уже запущенный синтез
доносит кусок в очередь, плеер его отдаёт, и оборванный ответ звучит
заново поверх новой фразы — уже после команды `stop`.
"""
import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_dialog  # noqa: E402

FIRST = "Первое предложение достаточно длинное. "
SECOND = "Второе предложение тоже длинное. "


class DialogCancelTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_turn_stops_audio(self):
        chunks = []

        async def stream(_text, on_event=None):
            # Оба предложения уходят в синтез сразу: к моменту отмены второй
            # кусок уже в работе, и старый код успевал его доиграть.
            yield FIRST
            yield SECOND
            await asyncio.sleep(10)

        def tts(_seg):
            time.sleep(0.25)
            return b"\x00" * 320

        async def on_audio(chunk):
            chunks.append(chunk)

        turn = voice_dialog.DialogTurn(
            "вопрос", on_audio=on_audio, stream_fn=stream, tts_fn=tts)
        task = asyncio.create_task(turn.run())
        while not chunks:
            await asyncio.sleep(0.01)
        heard = len(chunks)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        await asyncio.sleep(0.8)
        self.assertEqual(len(chunks), heard,
                         "после отмены хода новые куски в колонку не идут")


if __name__ == "__main__":
    unittest.main()
