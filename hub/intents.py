#!/usr/bin/env python3
"""Фразы-триггеры: реестр из конфига и сопоставление с распознанным текстом.

Зачем отдельный слой: фраза «спокойной ночи» должна уходить в Home Assistant
и **не** уходить в LLM. Решает это хаб, до вызова агента. Реестр фраз живёт в
`hub.json`, а не в коде — добавил фразу, перезапуск не нужен.

Сопоставление терпит ошибки ASR: GigaAM слышит «Герми» вместо «Гермес» и
«спакойной ночи» вместо «спокойной ночи». Поэтому сравниваем нормализованные
строки по расстоянию Левенштейна, а для коротких фраз допускаем совпадение по
якорю («ноч», «спат»).
"""
from __future__ import annotations

import re

# Всё, что не буква и не цифра, считаем разделителем: пунктуация от ASR
# («спокойной ночи!») не должна мешать точному совпадению.
_JUNK = re.compile(r"[^a-zа-я0-9 ]+")


def normalize(text: str) -> str:
    """Нижний регистр, ё→е, без пунктуации, одиночные пробелы."""
    low = (text or "").lower().replace("ё", "е")
    return " ".join(_JUNK.sub(" ", low).split())


def _lev(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _rule_phrases(canonical: str, rule: dict) -> list[str]:
    phrases = [canonical]
    extra = rule.get("phrases") if isinstance(rule, dict) else None
    if isinstance(extra, list):
        phrases += [str(p) for p in extra if str(p).strip()]
    return phrases


def match(text: str, intents: dict, max_distance: int = 2,
          use_anchors: bool = True, anchor_max_words: int = 4) -> str | None:
    """Каноническая фраза, если текст на неё похож, иначе None.

    Порядок: точное совпадение → Левенштейн в пределах `max_distance` →
    якорь для короткой фразы. Середина длинной фразы якорем не считается:
    «включи ночник» не должен превращаться в «спокойной ночи».
    """
    norm = normalize(text)
    if not norm:
        return None

    best: str | None = None
    best_distance: int | None = None

    for canonical, rule in (intents or {}).items():
        rule = rule if isinstance(rule, dict) else {}
        for phrase in _rule_phrases(canonical, rule):
            pn = normalize(phrase)
            if not pn:
                continue
            if norm == pn:
                return canonical
            distance = _lev(norm, pn)
            if distance <= max_distance and (
                    best_distance is None or distance < best_distance):
                best, best_distance = canonical, distance

    if best is not None:
        return best
    if not use_anchors:
        return None

    words = norm.split()
    if len(words) > anchor_max_words:
        return None

    # Якорь — не самостоятельное доказательство: «включи ночник» содержит
    # «ноч», но к «спокойной ночи» отношения не имеет. Поэтому якорь лишь
    # расширяет допуск по расстоянию (сильные искажения ASR), а близость
    # всё равно проверяется.
    limit = max_distance * 2
    for canonical, rule in (intents or {}).items():
        rule = rule if isinstance(rule, dict) else {}
        anchors = rule.get("anchors")
        if not isinstance(anchors, list):
            continue
        if not any(normalize(str(a)) and normalize(str(a)) in norm for a in anchors):
            continue
        nearest = min(
            (_lev(norm, normalize(p)) for p in _rule_phrases(canonical, rule)
             if normalize(p)),
            default=None)
        if nearest is not None and nearest <= limit and (
                best_distance is None or nearest < best_distance):
            best, best_distance = canonical, nearest
    return best


def is_excluded(canonical: str, intents: dict) -> bool:
    """Фразы с `mqtt: false` не публикуются в HA.

    «Да»/«Нет» живут в интентах Алисы, но в голосовом контуре у них другая
    роль: подтверждение внутри диалога, а не сценарий. Публиковать их в
    MQTT нельзя — обычное «да» в разговоре дёргало бы чужие автоматизации.
    """
    rule = (intents or {}).get(canonical) or {}
    return isinstance(rule, dict) and rule.get("mqtt") is False
