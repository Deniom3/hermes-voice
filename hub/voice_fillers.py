"""Фразы-заполнители тишины: «секунду, думаю» пока агент не ответил.

Жили в панели; теперь ими пользуется ядро, поэтому одинаково звучат и на
колонке, и в панели. Заготовки синтезируются один раз и лежат wav-файлами в
`voice-fillers/<голос>/`, поэтому в момент паузы ничего ждать не нужно.

ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНО: VOICE_FILLERS=1 включает. Пока это не отлажено на
живом разговоре, тишину лучше оставить тишиной, чем получить наложение
заготовки на первый кусок ответа.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random

log = logging.getLogger("fillers")

VOICE = "eugene"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Заготовки лежат в репозитории, а хаб может работать из другого каталога
# (/srv/hermes-station/server). Поэтому смотрим несколько мест, а не одно:
# env, репозиторий, каталог рядом с хабом.
_REPO_CANDIDATES = [
    os.environ.get("VOICE_FILLER_DIR", ""),
    os.path.join("/opt/hermes-voice", "hub", "fillers", VOICE),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fillers", VOICE),
    os.path.join(BASE_DIR, "hub", "fillers", VOICE),
    os.path.join("/srv/hermes-station", "voice-fillers", VOICE),
]
REPO_DIRS = [p for p in _REPO_CANDIDATES if p and os.path.isdir(p)]
CACHE_DIR = os.environ.get(
    "VOICE_FILLER_CACHE",
    os.path.expanduser("~/.hermes/voice-cache/fillers"))

THINK = [
    "Секунду, думаю.",
    "Момент, подумаю.",
    "Так, собираюсь с мыслями.",
    "Давай подумаю секунду.",
    "Сейчас соображу.",
    "Минуту, обдумываю.",
    "Сейчас разберусь и отвечу.",
    "Так, есть мысль, секунду.",
]
FIRST = float(os.environ.get("VOICE_FILLER_FIRST", "6.0"))
MIN_GAP = float(os.environ.get("VOICE_FILLER_MIN_GAP", "7.0"))
MAX_GAP = float(os.environ.get("VOICE_FILLER_MAX_GAP", "15.0"))
MAX_COUNT = int(os.environ.get("VOICE_FILLER_MAX", "40"))

_DECKS: dict[str, list[str]] = {}
_LAST = [""]


def enabled() -> bool:
    return os.environ.get("VOICE_FILLERS", "0").strip().lower() not in (
        "0", "false", "no", "off", "")


def take(pool: list[str]) -> str:
    """Фраза не повторяется, пока не кончится набор; потом набор мешается заново."""
    deck = _DECKS.get(pool[0])
    if not deck:
        deck = random.sample(pool, len(pool))
        if len(deck) > 1 and deck[-1] == _LAST[0]:
            deck[-1], deck[0] = deck[0], deck[-1]
        _DECKS[pool[0]] = deck
    phrase = deck.pop()
    _LAST[0] = phrase
    return phrase


def key(phrase: str) -> str:
    return hashlib.sha1((phrase + "|" + VOICE).encode()).hexdigest()[:16]


def file_for(phrase: str) -> str | None:
    """Готовая запись: сначала заготовка из репозитория, потом рантайм-кэш."""
    k = key(phrase)
    for folder in REPO_DIRS + [CACHE_DIR]:
        path = os.path.join(folder, k + ".wav")
        if os.path.exists(path):
            return path
    return None


def audio(phrase: str) -> bytes | None:
    """Байты wav заготовки. Синтез «на ходу» здесь не делается намеренно:
    в паузе ждать синтез нельзя, а наборы заготовок лежат в репозитории."""
    path = file_for(phrase)
    if not path:
        log.warning("нет заготовки для фразы %r (%s)", phrase, key(phrase))
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:  # noqa: BLE001
        log.warning("заготовка не читается (%s): %r", path, e)
        return None
