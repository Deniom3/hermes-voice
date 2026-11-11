"""Единый критерий «это обращение к ассистенту».

`voice_core.name_like` решает, отвечать ли на фразу, а `voice_dialog.strip_wake`
срезает обращение перед реестром интентов и LLM. Пока критерии были разными,
ASR-искажение («Дермис») признавалось обращением, но не срезалось: в реестр
уходило «Дермис, спокойной ночи», совпадения не было, и фраза-триггер
уезжала в LLM вместо Home Assistant.

Держим сравнение в одном месте: нормализация мягкого знака, ограничение по
длине и расстояние Левенштейна. Формы имён — из `wake.names` в hub.json.
"""
from __future__ import annotations

import re

import config

_WORD_RE = re.compile(r"[а-яa-z]+")

# Формы, которые должны узнаваться даже если их нет в конфиге: латиница и
# частые искажения ASR.
_EXTRA = ("hermes", "germes", "girmes", "ермес", "хермес")


def forms() -> tuple[str, ...]:
    base = tuple(config.as_list(
        "wake", "names", default=("гермес", "гермиона", "hermes", "ермес")))
    return tuple(dict.fromkeys(base + _EXTRA))


def roots() -> tuple[str, ...]:
    """Корни форм имени: «гермес» → «герм», «ермес» → «ерме».

    ASR выдаёт «гермез», «гермяс», «гермион» — проверять полное совпадение
    бессмысленно. Корень берётся от конфига, а не пишется в коде отдельно.
    """
    out: list[str] = []
    for form in forms():
        stem = form[:4] if len(form) > 4 else form
        if stem not in out:
            out.append(stem)
    return tuple(out)


def pattern() -> str:
    """Регулярка по корням — для поиска имени внутри готового текста."""
    return "|".join(re.escape(r) for r in sorted(roots(), key=len, reverse=True))


def is_stem(word: str) -> bool:
    """Начинается ли слово с корня имени."""
    return any(word.startswith(r) for r in roots())


def normalize(word: str) -> str:
    # Мягкий знак ASR вставляет произвольно: «дерьмя» вместо «дермес».
    # Без нормализации расстояние до «гермес» выходит 4 и имя теряется.
    return (word or "").lower().replace("ё", "е").replace("ь", "").replace("ъ", "")


def lev(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def is_name(word: str) -> bool:
    """Похоже ли слово на имя ассистента (с учётом искажений ASR)."""
    w = normalize(word)
    if len(w) < 5:
        return False
    return any(abs(len(w) - len(f)) <= 1 and lev(w, f) <= 3
               for f in forms())


def name_like(text: str) -> bool:
    """Есть ли обращение в первых трёх словах фразы."""
    t = (text or "").lower().replace("ё", "е")
    return any(is_name(w) for w in _WORD_RE.findall(t)[:3])
