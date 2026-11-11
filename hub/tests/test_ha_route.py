"""Тесты быстрого домашнего маршрута: команды и показания без LLM.

Клиент HA подменён: состояния, реестры и экспозиция — фикстура, сеть не нужна.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ha_route  # noqa: E402
from ha_route import HomeRoute  # noqa: E402


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
            "number.hermes_lite_1_volume": state(
                "number.hermes_lite_1_volume", "Громкость станции — управление", "40", "%"),
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
                {"entity_id": "number.hermes_lite_1_volume", "device_id": "d_station", "name": None},
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
        if eid in self.data and service == "set_value":
            self.data[eid]["state"] = str(data.get("value"))
        if domain == "yandex_station" and data.get("command") == "setVolume":
            # Колонка применяет громкость сама; мост обновит сущность опросом.
            pct = round(float(data.get("volume", 0)) * 100)
            for key in self.data:
                if key.startswith("number.") and key.endswith("_volume"):
                    self.data[key]["state"] = str(pct)
        return True


def run(coro):
    return asyncio.run(coro)


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.ha = FakeHA()
        self.route = HomeRoute(self.ha)

    def ask(self, text, area=None):
        # Станция приходит из хаба всегда; комната берётся из конфига по имени.
        return run(self.route.route(text, area, station="lite-1"))

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
        """Комната колонки — из `wake.stations.<имя>.area`."""
        run(self.route.route("сколько времени", station="lite-1"))
        self.assertEqual(self.route.default_area, "spalnia")

    def test_station_without_area_has_none(self):
        """Станция без комнаты в конфиге: не угадываем чужую."""
        run(self.route.route("сколько времени", station="lite-9"))
        self.assertEqual(self.route.default_area, "")

    def test_unknown_area_is_rejected(self):
        """area, которой нет в реестре HA, — ошибка конфига, а не подсказка."""
        with mock.patch.object(ha_route, "station_area_for", lambda name: "garazh"):
            run(self.route.route("сколько времени", station="lite-1"))
        self.assertEqual(self.route.default_area, "")

    def test_no_area_hint_for_readings(self):
        """Нет комнаты и датчики в разных комнатах — говорим, а не молчим."""
        answer = run(self.route.route("какая температура", station="lite-9"))
        self.assertIn("Комната не задана", answer)

    def test_area_answers_without_hint(self):
        answer = run(self.route.route("какая температура", station="lite-1"))
        self.assertIn("Температура", answer)
        self.assertNotIn("Комната не задана", answer)

    def test_no_hint_when_device_is_unique(self):
        """Уникальное устройство без комнаты — обычный путь, а не подсказка."""
        answer = run(self.route.route("выключи кондиционер", station="lite-9"))
        self.assertNotIn("Комната не задана", answer or "")

    def test_unexposed_entity_ignored(self):
        self.ha.reg["exposed"]["exposed_entities"]["light.lustra"] = {"conversation": False}
        self.route._cached_at = 0.0
        self.assertIsNone(self.ask("выключи люстру"))


class VolumeRouteTest(unittest.TestCase):
    """Громкость станции голосом: 10 уровней и мьют, без LLM."""

    def setUp(self):
        self.ha = FakeHA()
        self.route = HomeRoute(self.ha)
        self.ha.data["number.hermes_lite_1_volume"]["state"] = "4"
        self.set_levels = []

        # Единственный путь установки — агент станции. Заглушка повторяет его
        # контракт: уровень уходит в maind, а HA-сущность обновляется следом.
        async def setter(level, station=None):
            self.set_levels.append((level, station))
            if station == "lite-1":
                self.ha.data["number.hermes_lite_1_volume"]["state"] = str(level)
            return True

        async def source(station=None):
            return int(self.ha.data["number.hermes_lite_1_volume"]["state"])

        self.route.volume_setter = setter
        self.route.volume_source = source

    def ask(self, text):
        # Станция в проде всегда приходит из хаба (имя из hello), поэтому и в
        # тестах её надо передавать явно: без имени громкость не меняется.
        return run(self.route.route(text, station="lite-1"))

    def volume(self):
        return int(self.ha.data["number.hermes_lite_1_volume"]["state"])

    def test_number_as_word(self):
        # ASR часто отдаёт число словом — «Громкость два» должно работать
        self.assertEqual(self.ask("громкость два"), "Громкость 2")
        self.assertEqual(self.volume(), 2)

    def test_number_as_word_with_step(self):
        self.assertEqual(self.ask("тише на два"), "Громкость 2")
        self.assertEqual(self.volume(), 2)

    def test_word_number_out_of_scale(self):
        self.assertEqual(self.ask("громкость двадцать"), "")
        self.assertEqual(self.volume(), 4)

    def test_volume_goes_through_agent_only(self):
        # Единый путь: ядро → агент станции, в HA ничего не пишем.
        self.assertEqual(self.ask("громкость 4"), "Громкость 4")
        self.assertEqual(self.set_levels[-1], (4, "lite-1"))
        self.assertEqual([c for c in self.ha.calls if c[0] == "number"], [])

    def test_volume_without_agent_is_not_applied(self):
        # Без агента громкость не ставится: `number.hermes_*_volume` — наш же
        # MQTT-мост, команда вернулась бы в station_set_volume, то есть в того
        # же агента. Фолбек через HA только добавлял круг и врал об успехе.
        self.route.volume_setter = None
        self.assertEqual(self.ask("громкость 4"), None)
        self.assertEqual(self.volume(), 4)

    def test_question_right_after_change_uses_new_value(self):
        # Источник — агент: он читает микшер напрямую, поэтому сразу после
        # команды отдаёт новое значение, даже если в HA ещё старое.
        async def source(station=None):
            return int(self.ha.data["number.hermes_lite_1_volume"]["state"])
        self.route.volume_source = source
        self.assertEqual(self.ask("громкость 5"), "Громкость 5")
        self.assertEqual(self.ask("какая громкость"), "Громкость 5")

    def test_agent_fact_beats_cache_after_alice_change(self):
        # Алиса убавила голосом между командами: кэш «8» не должен перебивать
        # факт «3» от агента.
        async def source(station=None):
            return 3
        self.route.volume_source = source
        self.assertEqual(self.ask("громкость 8"), "Громкость 8")
        self.assertEqual(self.ask("какая громкость"), "Громкость 3")

    def test_volume_question_is_answered_fast(self):
        # «какая громкость» — быстрый ответ, значение не меняется
        before = self.volume()
        self.assertEqual(self.ask("какая громкость сейчас"), "Громкость 4")
        self.assertEqual(self.volume(), before)
        self.assertEqual([c for c in self.ha.calls if c[0] == "yandex_station"], [])

    def test_volume_and_brightness_are_different_scales(self):
        # Одно и то же число: для громкости 50 недопустимо, для яркости — норма.
        self.assertEqual(self.ask("громкость пятьдесят"), "")
        self.assertEqual(self.volume(), 4)
        self.assertEqual(self.ask("яркость люстры пятьдесят"),
                         "Яркость 50 процентов: Люстра")
        self.assertEqual(self.ha.calls[-1], ("light", "turn_on",
                                             {"entity_id": "light.lustra",
                                              "brightness_pct": 50}))

    def test_only_levels_in_speech(self):
        # Ответ никогда не в процентах: 40% — это «4 из 10».
        self.assertEqual(self.ask("громкость 6"), "Громкость 6")
        self.assertEqual(self.volume(), 6)

    def test_absolute_level(self):
        # «громкость 4» — уровень 4 из 10, то есть 40%
        self.assertEqual(self.ask("громкость 4"), "Громкость 4")
        self.assertEqual(self.volume(), 4)

    def test_numbers_above_ten_do_nothing(self):
        # Контракт как у Алисы: только 1–10. Недопустимое — не меняем и молчим.
        self.assertEqual(self.ask("поставь громкость 70"), "")
        self.assertEqual(self.volume(), 4)

    def test_level_one_is_ten_percent(self):
        self.assertEqual(self.ask("громкость 1"), "Громкость 1")
        self.assertEqual(self.volume(), 1)

    def test_step_up(self):
        self.assertEqual(self.ask("прибавь"), "Громкость 5")
        self.assertEqual(self.volume(), 5)

    def test_step_down(self):
        self.assertEqual(self.ask("тише"), "Громкость 3")
        self.assertEqual(self.volume(), 3)

    def test_step_with_levels(self):
        # «тише на 2» — два уровня вниз
        self.assertEqual(self.ask("тише на 2"), "Громкость 2")
        self.assertEqual(self.volume(), 2)

    def test_mute(self):
        self.assertEqual(self.ask("выключи звук"), "Звук выключен")
        self.assertEqual(self.volume(), 0)

    def test_clamped_at_hundred(self):
        self.ha.data["number.hermes_lite_1_volume"]["state"] = "9"
        self.assertEqual(self.ask("громче"), "Громкость 10")
        self.assertEqual(self.volume(), 10)

    def test_clamped_at_zero(self):
        self.ha.data["number.hermes_lite_1_volume"]["state"] = "1"
        self.assertEqual(self.ask("тише"), "Звук выключен")
        self.assertEqual(self.volume(), 0)

    def test_fresh_volume_beats_stale_ha_state(self):
        # HA-number отстаёт на опрос моста; относительную команду считаем
        # от фактического значения, которое отдал агент.
        async def fresh(station=None):
            return 3

        async def setter(level, station=None):
            self.ha.data["number.hermes_lite_1_volume"]["state"] = str(level)
            return True

        route = HomeRoute(self.ha, volume_source=fresh,
                          volume_setter=setter)
        self.ha.data["number.hermes_lite_1_volume"]["state"] = "4"
        self.assertEqual(run(route.route("прибавь", station="lite-1")), "Громкость 4")
        self.assertEqual(self.volume(), 4)

    def test_other_command_does_not_touch_volume(self):
        self.ask("включи чайник")
        self.assertEqual(self.volume(), 4)


if __name__ == "__main__":
    unittest.main()
