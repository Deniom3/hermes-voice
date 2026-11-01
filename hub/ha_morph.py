#!/usr/bin/env python3
"""Морфологический слой быстрого голосового маршрута.

Зачем: встроенный распознаватель Home Assistant (hassil) сравнивает имена
буквально. «включи настольная лампа» он понимает, «включи настольную лампу» —
уже нет, потому что падежей в шаблонах нет. Здесь словоформы фразы и слова
имён приводятся к основе, поэтому падеж перестаёт иметь значение.

Модуль ничего не знает про сеть, звук и состояния: только текст → найденная
сущность. Список сущностей приходит снаружи (из реестров HA), здесь только
сопоставление.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("ha-morph")

# Окончания от длинных к коротким. Основу короче четырёх букв не трогаем:
# иначе «свет» превращается в «све», а «ночь» — в «ноч».
_ENDINGS = tuple(sorted({
    "иями", "ями", "ами", "ыми", "ими", "ому", "ему", "ется", "ить", "ать",
    "ять", "ешь", "ишь", "ов", "ев", "ий", "ый", "ой", "ая", "ое", "ые",
    "ых", "ую", "юю", "ом", "ем", "ах", "ях", "ам", "ям", "ы", "и",
    "а", "я", "у", "ю", "е", "о", "ь",
}, key=len, reverse=True))

# Числа словами: распознавание часто отдаёт «лампочку один» вместо «1».
_NUM = {
    "один": "1", "одну": "1", "одна": "1", "первый": "1", "первую": "1", "первая": "1",
    "два": "2", "две": "2", "второй": "2", "вторую": "2", "вторая": "2",
    "три": "3", "третий": "3", "третью": "3", "третья": "3",
}

# Служебные слова в именах: «Включение чайника» — это чайник.
_NAME_STOP = {"включение", "выключение", "статус", "питание", "power", "устройство"}

# Предлоги и союзы не участвуют в сопоставлении: «в» есть в «Объем воды
# в чайнике» и без фильтра даёт ложное совпадение с любой фразой.
_WORD_STOP = {
    "в", "во", "на", "с", "со", "и", "а", "но", "к", "ко", "от", "до", "по",
    "за", "из", "у", "о", "об", "же", "ли", "мне", "пожалуйста",
}

_WORD_RE = re.compile(r"[0-9a-zа-я]+")


def normalize(text: str) -> str:
    """Нижний регистр, ё→е, всё кроме букв и цифр — в пробел."""
    text = text.lower().replace("ё", "е")
    return " ".join(_WORD_RE.findall(text))


def stem(word: str) -> str:
    """Основа слова: отсечение русского окончания, если остаётся ≥4 букв."""
    if word in _NUM:
        return _NUM[word]
    for end in _ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= 4:
            return word[:-len(end)]
    return word


def stems(text: str) -> tuple[str, ...]:
    return tuple(stem(w) for w in normalize(text).split())


def match_stems(text: str) -> tuple[str, ...]:
    """Основы без предлогов и союзов — только значимые слова."""
    return tuple(s for s in stems(text) if s not in _WORD_STOP)


@dataclass(frozen=True)
class Device:
    """Сущность HA, разрешённая для голоса.

    role — домен (`light`, `switch`, `climate`, `water_heater`, `vacuum`,
    `sensor`). kind отделяет actuators от показаний, чтобы команда не искала
    устройство среди датчиков и наоборот.
    """

    entity_id: str
    name: str
    area: str = ""
    area_name: str = ""
    synonyms: tuple[str, ...] = field(default_factory=tuple)
    role: str = ""
    kind: str = "actuator"
    unit: str = ""


@dataclass(frozen=True)
class Match:
    device: Device
    cover: float
    variant: str
    hit: tuple[str, ...]


class Matcher:
    """Сопоставление фразы с сущностями по основам слов."""

    MIN_COVER = 0.5
    TIE = 0.01

    def __init__(self, devices: list[Device]):
        self.devices = list(devices)

    def _variants(self, device: Device) -> list[tuple[str, tuple[str, ...]]]:
        out = []
        for variant in (device.name, *device.synonyms):
            # Повторы в имени («Пылесос Робот пылесос») не должны раздувать
            # знаменатель покрытия: иначе «включи пылесос» даёт 1/3 вместо 1/2.
            need = tuple(dict.fromkeys(
                s for s in match_stems(variant) if s not in _NAME_STOP
            ))
            if need:
                out.append((variant, need))
        return out

    def find(self, text: str, role: str | None = None, area: str | None = None,
             kind: str | None = None, limit: int = 8) -> list[Match]:
        phrase = set(match_stems(text))
        if not phrase:
            return []
        found: list[Match] = []
        for device in self.devices:
            if role and device.role != role:
                continue
            if area and device.area != area:
                continue
            if kind and device.kind != kind:
                continue
            best: Match | None = None
            for variant, need in self._variants(device):
                hit = tuple(sorted(set(need) & phrase))
                if not hit:
                    continue
                cover = len(hit) / len(need)
                candidate = Match(device, cover, variant, hit)
                if best is None or (candidate.cover, len(candidate.hit)) > (best.cover, len(best.hit)):
                    best = candidate
            if best is not None:
                found.append(best)
        found.sort(key=lambda m: (m.cover, len(m.hit), len(m.variant)), reverse=True)
        return found[:limit]

    def best(self, text: str, role: str | None = None, area: str | None = None,
             kind: str | None = None) -> Match | None:
        """Лучшая сущность или None, если неоднозначно или слабо.

        Сначала сужаем по роли, комнате и типу, потом сравниваем покрытие.
        Два равных кандидата — не угадываем: пусть решает агент.
        """
        found = self.find(text, role=role, area=area, kind=kind)
        if not found:
            return None
        top = found[0]
        if top.cover < self.MIN_COVER:
            return None
        if len(found) > 1:
            second = found[1]
            same_cover = abs(second.cover - top.cover) < self.TIE
            if second.device.entity_id != top.device.entity_id and same_cover \
                    and len(second.hit) == len(top.hit):
                log.info("неоднозначно: %r → %s / %s", text,
                         top.device.entity_id, second.device.entity_id)
                return None
        return top
