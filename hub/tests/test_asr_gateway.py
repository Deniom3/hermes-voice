"""Распознавание ходит в один шлюз и не прячет его ошибки.

Раньше при 502 шлюза запрос молча уходил напрямую в audio.cpp на :8010.
Два пути к одной модели означали разное поведение на одной и той же фразе
и полное отсутствие следов в журнале: нельзя было узнать, срабатывал обход
или шлюз просто работал. Тест держит один адрес и проверяет, что провал
шлюза виден в логе.
"""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_loop  # noqa: E402


def quiet_pcm(seconds=0.5):
    """Тихая фраза: нормализация громкости не должна мешать проверке."""
    return (np.sin(np.arange(int(16000 * seconds)) / 20.0) * 100).astype(np.int16)


class SingleGatewayTest(unittest.TestCase):
    def test_only_one_asr_endpoint(self):
        self.assertTrue(voice_loop.ASR_URL.endswith('/v1/audio/transcriptions'))
        self.assertIn(':8000', voice_loop.ASR_URL)
        self.assertNotIn(':8010', voice_loop.ASR_URL)
        self.assertFalse(hasattr(voice_loop, 'ASR_URLS'),
                         'второго адреса распознавания быть не должно')

    def test_success_returns_text_without_retry(self):
        with mock.patch.object(voice_loop, '_post', return_value='Окей, Гермес.') as p:
            self.assertEqual(voice_loop.transcribe(quiet_pcm()), 'Окей, Гермес.')
            self.assertEqual(p.call_count, 1)
            self.assertEqual(p.call_args[0][0], voice_loop.ASR_URL)

    def test_failure_retries_twice_and_logs(self):
        with mock.patch.object(voice_loop, '_post', side_effect=OSError('502')) as p, \
                self.assertLogs(voice_loop.log, level='WARNING') as logs:
            with self.assertRaises(OSError):
                voice_loop.transcribe(quiet_pcm())
        self.assertEqual(p.call_count, 2)
        self.assertTrue(any('502' in line for line in logs.output))
        self.assertTrue(all(':8000' in line for line in logs.output))

    def test_explicit_url_wins(self):
        target = 'http://127.0.0.1:9999/v1/audio/transcriptions'
        with mock.patch.object(voice_loop, '_post', return_value='текст') as p:
            voice_loop.transcribe(quiet_pcm(), url=target)
        self.assertEqual(p.call_args[0][0], target)


if __name__ == '__main__':
    unittest.main()
