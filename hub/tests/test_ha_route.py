"""Тесты быстрого домашнего маршрута: команды и показания без LLM.

Клиент HA подменён: состояния, реестры и экспозиция — фикстура, сеть не нужна.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ha_route import HomeRoute  # noqa: E402

STATION = "media_player.yandex_station_lite"


def state(eid, name, value, unit=None, extra=None):
    attrs = {"friendly_name": name}
    if unit:
        attrs["unit_of_measurement"] = unit
    attrs.update(extra or {})
    return {"entity_id": eid, "state": value, "attributes": attrs}


class FakeHA:
    """Клиент HA без сети: состояния, реестры, вызовы сервисов."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.data = {
            "light.lustra": state("light.lustra", "Люстра", "on"),
            "light.bedrom_desk_lamp_s1": state("light.bedrom_desk_lamp_s1", "Настольная лампа", "on"),
            "switch.kettle_pwk_1775cgld_power": state(
                "switch.kettle_pwk_1775cgld_power", "Включение чайника", "off"),
            "water_heater.kettle_pwk_1775cgld_water_heater": state(
                "water_heater.kettle_pwk_1775cgld_water_heater", "Чайник", "performance"),
            "climate.panasonic_ac": state("climate.panasonic_ac", "Кондиционер", "off"),
            "vacuum.cleaner_pvcr_0726_gyro_vacuum": state(
                "vacuum.cleaner_pvcr_0726_gyro_vacuum", "Пылесос Робот пылесос", "unavailable"),
            "sensor.obem_vody_v_chainike": state(
                "sensor.obem_vody_v_chainike", "Объем воды в чайнике", "0.92", "L"),
            "sensor.kettle_pwk_1775cgld_temperature": state(
                "sensor.kettle_pwk_1775cgld_temperature", "Чайник Температура", "90.0", "°C"),
            "sensor.bedroom_thermometer_temperature": state(
                "sensor.bedroom_thermometer_temperature", "Температура", "25.9", "°C"),
            "sensor.bedroom_thermometer_humidity": state(
                "sensor.bedroom_thermometer_humidity", "Влажность", "55.3", "%"),
            "sensor.kitchen_thermometer_temperature": state(
                "sensor.kitchen_thermometer_temperature", "Температура", "24.0", "°C"),
            "weather.yandex_pogoda": state("weather.yandex_pogoda", "Погода", "cloudy",
                                          extra={"temperature": 13.0}),
            "sensor.yandex_pogoda_temperature": state(
                "sensor.yandex_pogoda_temperature", "Погода температура", "13", "°C"),
            STATION: state(STATION, "Станция лайт", "idle"),
        }
        self.reg = {
            "exposed": {"exposed_entities": {eid: {"conversation": True} for eid in self.data}},
            "areas": [
                {"area_id": "spalnia", "name": "Комната", "aliases": []},
                {"area_id": "kukhnia", "name": "Кухня", "aliases": []},
                {"area_id": "koridor", "name": "Коридор", "aliases": []},
                {"area_id": "ulitsa", "name": "Улица", "aliases": []},
            ],
            "devices": [
                {"id": "d_station", "area_id": "spalnia"},
                {"id": "d_lamp", "area_id": "spalnia"},
                {"id": "d_kettle", "area_id": "kukhnia"},
                {"id": "d_ac", "area_id": "koridor"},
                {"id": "d_vacuum", "area_id": "kukhnia"},
                {"id": "d_weather", "area_id": "ulitsa"},
            ],
            "entities": [
                {"entity_id": "light.lustra", "area_id": "spalnia", "device_id": None, "name": None},
                {"entity_id": "light.bedrom_desk_lamp_s1", "device_id": "d_lamp", "name": None},
                {"entity_id": "switch.kettle_pwk_1775cgld_power", "device_id": "d_kettle", "name": None},
                {"entity_id": "water_heater.kettle_pwk_1775cgld_water_heater", "device_id": "d_kettle",
                 "name": None},
                {"entity_id": "climate.panasonic_ac", "device_id": "d_ac", "name": None},
                {"entity_id": "vacuum.cleaner_pvcr_0726_gyro_vacuum", "device_id": "d_vacuum",
                 "name": None},
                {"entity_id": "sensor.obem_vody_v_chainike", "area_id": "kukhnia", "name": None},
                {"entity_id": "sensor.kettle_pwk_1775cgld_temperature", "device_id": "d_kettle",
                 "name": None},
                {"entity_id": "sensor.bedroom_thermometer_temperature", "area_id": "spalnia",
                 "name": None},
                {"entity_id": "sensor.bedroom_thermometer_humidity", "area_id": "spalnia",
                 "name": None},
                {"entity_id": "sensor.kitchen_thermometer_temperature", "area_id": "kukhnia",
                 "name": None},
                {"entity_id": "weather.yandex_pogoda", "device_id": "d_weather", "name": None},
                {"entity_id": "sensor.yandex_pogoda_temperature", "device_id": "d_weather", "name": None},
                {"entity_id": STATION, "device_id": "d_station", "name": None},
            ],
        }

    async def states(self):
        return dict(self.data)

    async def state(self, entity_id):
        return self.data.get(entity_id)

    async def registry(self):
        return self.reg

    async def call(self, domain, service, data):
        self.calls.append((domain, service, dict(data)))
        eid = data.get("entity_id")
        if eid in self.data and service in ("turn_on", "turn_off"):
            self.data[eid]["state"] = "on" if service == "turn_on" else "off"
        return True


def run(coro):
    return asyncio.run(coro)


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.ha = FakeHA()
        self.route = HomeRoute(self.ha, station_entity=STATION)

    def ask(self, text, area=None):
        return run(self.route.route(text, area))

    def last_call(self):
        return self.ha.calls[-1] if self.ha.calls else None

    # --- команды ---

    def test_kettle_switch(self):
        self.assertEqual(self.ask("включи чайник"), "Включил")
        self.assertEqual(self.last_call(), ("switch", "turn_on",
                                            {"entity_id": "switch.kettle_pwk_1775cgld_power"}))

    def test_kettle_temperature(self):
        self.assertEqual(self.ask("поставь чайник на 70 градусов"),
                         "Поставил 70 градусов")
        domain, service, data = self.last_call()
        self.assertEqual((domain, service), ("water_heater", "set_temperature"))
        self.assertEqual(data["temperature"], 70)

    def test_light_off(self):
        self.assertEqual(self.ask("выключи люстру"), "Выключил")
        self.assertEqual(self.last_call()[0:2], ("light", "turn_off"))

    def test_light_on_by_short_name(self):
        self.assertEqual(self.ask("включи лампу"), "Включил")

    def test_room_light(self):
        self.assertEqual(self.ask("включи свет"), "Включил свет")
        self.assertEqual(self.last_call(), ("light", "turn_on", {"area_id": "spalnia"}))

    def test_room_light_off(self):
        self.assertEqual(self.ask("выключи свет в комнате"), "Выключил свет")
        self.assertEqual(self.last_call(), ("light", "turn_off", {"area_id": "spalnia"}))

    def test_climate_on(self):
        self.assertEqual(self.ask("включи кондиционер"), "Включил")
        self.assertEqual(self.last_call()[0:2], ("climate", "turn_on"))

    def test_climate_temperature(self):
        self.assertEqual(self.ask("поставь кондиционер на 22"), "Поставил 22 градуса")
        self.assertEqual(self.last_call()[1], "set_temperature")

    def test_vacuum_unavailable(self):
        self.assertEqual(self.ask("включи пылесос"), "Пылесос сейчас недоступен")
        self.assertEqual(self.ha.calls, [])

    def test_vacuum_return_to_base(self):
        self.ha.data["vacuum.cleaner_pvcr_0726_gyro_vacuum"]["state"] = "docked"
        self.assertEqual(self.ask("верни пылесос на базу"), "Отправил пылесос на базу")
        self.assertEqual(self.last_call()[0:2], ("vacuum", "return_to_base"))

    def test_vacuum_start_when_available(self):
        self.ha.data["vacuum.cleaner_pvcr_0726_gyro_vacuum"]["state"] = "docked"
        self.assertEqual(self.ask("включи пылесос"), "Запустил пылесос")
        self.assertEqual(self.last_call()[0:2], ("vacuum", "start"))

    def test_vacuum_stop(self):
        self.ha.data["vacuum.cleaner_pvcr_0726_gyro_vacuum"]["state"] = "cleaning"
        self.assertEqual(self.ask("останови пылесос"), "Остановил пылесос")
        self.assertEqual(self.last_call()[0:2], ("vacuum", "stop"))

    def test_light_brightness(self):
        self.assertEqual(self.ask("поставь настольной лампе яркость 30"),
                         "Яркость 30 процентов: Настольная лампа")
        domain, service, data = self.last_call()
        self.assertEqual((domain, service), ("light", "turn_on"))
        self.assertEqual(data["brightness_pct"], 30)

    # --- показания ---

    def test_water_volume(self):
        self.assertEqual(self.ask("сколько воды в чайнике"),
                         "Объем воды в чайнике: 0,92 литра")

    def test_kettle_temperature_reading(self):
        self.assertEqual(self.ask("какая температура чайника"),
                         "Чайник Температура: 90 градусов")

    def test_room_humidity(self):
        self.assertEqual(self.ask("какая влажность в комнате"),
                         "Влажность (Комната): 55 процентов")

    def test_room_temperature(self):
        self.assertEqual(self.ask("какая температура в комнате"),
                         "Температура (Комната): 25,9 градуса")

    def test_kitchen_temperature(self):
        self.assertEqual(self.ask("какая температура на кухне"),
                         "Температура (Кухня): 24 градуса")

    def test_temperature_without_room_uses_station_area(self):
        # три датчика «Температура»; без комнаты берём ту, где стоит колонка
        self.assertEqual(self.ask("какая температура"),
                         "Температура (Комната): 25,9 градуса")

    # --- не наше ---

    def test_unknown_device(self):
        self.assertIsNone(self.ask("включи лампочку 1"))

    def test_music_not_ours(self):
        self.assertIsNone(self.ask("включи музыку"))
        self.assertEqual(self.ha.calls, [])

    def test_weather_not_ours(self):
        self.assertIsNone(self.ask("какая погода"))

    def test_chatter_not_ours(self):
        self.assertIsNone(self.ask("расскажи что-нибудь интересное"))

    # --- инвентарь из реестров ---

    def test_default_area_from_station(self):
        run(self.route._refresh(force=True))
        self.assertEqual(self.route.default_area, "spalnia")

    def test_unexposed_entity_ignored(self):
        self.ha.reg["exposed"]["exposed_entities"]["light.lustra"] = {"conversation": False}
        self.route._cached_at = 0.0
        self.assertIsNone(self.ask("выключи люстру"))


if __name__ == "__main__":
    unittest.main()
