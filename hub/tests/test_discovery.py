"""Поиск хаба по broadcast и определение станции по MAC.

Зачем: адрес хаба и имя станции не должны храниться на колонке. Адрес —
потому что DHCP может его сменить, имя — потому что это ключ к порогам,
а не то, что удобно помнить человеку.

mDNS здесь не работает: на колонке нет avahi/nss-mdns, а агент собран с
CGO_ENABLED=0, и его резолвер умеет только обычный DNS.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_hub  # noqa: E402

STATIONS = {
    "wake": {
        "stations": {
            "lite-1": {"mac": "AA:BB:CC:DD:EE:01", "threshold": 0.95},
            "lite-2": {"mac": "aa-bb-cc-dd-ee-02", "threshold": 0.95},
        }
    }
}


def fake_get(*keys, default=None):
    node = STATIONS
    for key in keys:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return default
    return node


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))


class NormalizeMacTest(unittest.TestCase):
    def test_formats(self):
        expected = "aa:bb:cc:dd:ee:01"
        self.assertEqual(voice_hub.normalize_mac("AA:BB:CC:DD:EE:01"), expected)
        self.assertEqual(voice_hub.normalize_mac("aa-bb-cc-dd-ee-01"), expected)
        self.assertEqual(voice_hub.normalize_mac("aabbccddee01"), expected)
        self.assertEqual(voice_hub.normalize_mac("aa.bb.cc.dd.ee.01"), expected)

    def test_rejects_garbage(self):
        for value in ("", None, "не mac", "aa:bb:cc:dd:ee", "zz:zz:zz:zz:zz:zz"):
            self.assertEqual(voice_hub.normalize_mac(value), "", f"принято: {value!r}")


class StationByMacTest(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(voice_hub.config, "get", fake_get)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_finds_station(self):
        self.assertEqual(voice_hub.station_by_mac("aa:bb:cc:dd:ee:01"), "lite-1")
        # Дефисы в конфиге не мешают: MAC нормализуется с обеих сторон.
        self.assertEqual(voice_hub.station_by_mac("AA:BB:CC:DD:EE:02"), "lite-2")

    def test_unknown_mac(self):
        self.assertIsNone(voice_hub.station_by_mac("00:11:22:33:44:55"))
        self.assertIsNone(voice_hub.station_by_mac(""))


class HubDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.proto = voice_hub.HubDiscovery(8796)
        self.transport = FakeTransport()
        self.proto.connection_made(self.transport)

    def test_replies_to_query(self):
        self.proto.datagram_received(b"HERMES-HUB?", ("192.168.1.20", 40000))
        self.assertEqual(len(self.transport.sent), 1)
        reply, addr = self.transport.sent[0]
        self.assertEqual(addr, ("192.168.1.20", 40000))
        parts = reply.decode().split()
        self.assertEqual(parts[0], "HERMES-HUB")
        self.assertEqual(parts[2], "8796")
        self.assertEqual(parts[1].count("."), 3)

    def test_ignores_other_traffic(self):
        self.proto.datagram_received(b"something else", ("192.168.1.20", 40000))
        self.proto.datagram_received(b"", ("192.168.1.20", 40000))
        self.assertEqual(self.transport.sent, [])


if __name__ == "__main__":
    unittest.main()
