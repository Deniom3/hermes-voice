"""Диагностический маршрут `/route` — только с самой машины.

Хаб слушает 0.0.0.0:8796 ради агентов и мониторинга. `/route` выполняет
настоящие команды дома, поэтому с LAN он должен отвечать 403: иначе любой
в сети мог бы дёргать чайник или свет.
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_hub  # noqa: E402


def request(remote):
    return SimpleNamespace(remote=remote, query={})


class LocalOnlyTest(unittest.TestCase):
    def test_remote_addresses_are_rejected(self):
        self.assertFalse(voice_hub._local_only(request("192.168.1.50")))
        self.assertFalse(voice_hub._local_only(request("10.0.0.2")))
        self.assertTrue(voice_hub._local_only(request("127.0.0.1")))
        self.assertTrue(voice_hub._local_only(request("::1")))

    def test_route_probe_denies_lan(self):
        resp = asyncio.run(voice_hub.route_probe(request("192.168.1.50")))
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
