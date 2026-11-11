"""Библиотека готовых реплик — один механизм для заполнителей и коротких ответов.

Реплика опознаётся по тексту и голосу: `sha1(текст|голос)[:16]`, имя файла —
`<ключ>.wav`. Готовые записи лежат в `hub/replies/<голос>/`, синтезированные
на ходу — в кэше. Заполнители берут **только готовое**: в паузе ждать синтез
нельзя. Обычные короткие ответы («Включил», «Комната не задана…») берут
готовое, а если записи нет — синтезируются один раз и остаются в кэше.

Раньше это были два разных механизма: `voice_fillers.py` со своим списком
текстов в коде и `voice_dialog.synthesize` без кэша вообще. Тексты были
ключами к аудио, но жили отдельно от него.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random

import config

log = logging.getLogger("replies")

VOICE_DEFAULT = "eugene"

# Длинные ответы модели не кэшируем: каждая фраза уникальна, кэш превратится
# в свалку. Кэшируются короткие реплики — приветствия, подтверждения, отказы.
MAX_CACHE_CHARS = 200

_DECKS: dict[str, list[str]] = {}
_LAST = [""]


def voice() -> str:
    return str(config.get("tts", "voice", default=VOICE_DEFAULT) or VOICE_DEFAULT)


def _repo_dir() -> str:
    base = config.get("replies", "dir", default="")
    if not base:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replies")
    return os.path.join(str(base), voice())


def _cache_dir() -> str:
    base = config.get("replies", "cache", default="/root/.hermes/voice-cache/replies")
    return os.path.join(str(base), voice())


def key(text: str) -> str:
    return hashlib.sha1((text + "|" + voice()).encode()).hexdigest()[:16]


def _manifest() -> dict:
    path = os.path.join(_repo_dir(), "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("нет манифеста библиотеки реплик: %s", path)
    except (OSError, ValueError) as e:  # noqa: BLE001
        log.warning("манифест реплик не читается (%s): %r", path, e)
    return {}


def pool(name: str) -> tuple[str, ...]:
    """Набор фраз по имени (`think`), в порядке манифеста."""
    phrases = (_manifest().get("pools") or {}).get(name) or ()
    return tuple(str(p) for p in phrases)


def take(name: str) -> str:
    """Фраза из набора без повторов, пока набор не кончится."""
    phrases = pool(name)
    if not phrases:
        raise RuntimeError(f"библиотека реплик: набор {name!r} пуст или не описан в манифесте")
    deck = _DECKS.get(name)
    if not deck:
        deck = random.sample(list(phrases), len(phrases))
        if len(deck) > 1 and deck[-1] == _LAST[0]:
            deck[-1], deck[0] = deck[0], deck[-1]
        _DECKS[name] = deck
    phrase = deck.pop()
    _LAST[0] = phrase
    return phrase


def path(text: str) -> str | None:
    """Файл готовой записи: сначала библиотека, потом кэш синтеза."""
    name = key(text) + ".wav"
    for folder in (_repo_dir(), _cache_dir()):
        candidate = os.path.join(folder, name)
        if os.path.exists(candidate):
            return candidate
    return None


def audio(text: str) -> bytes | None:
    """Байты готовой записи. Синтеза здесь нет намеренно."""
    found = path(text)
    if not found:
        return None
    try:
        with open(found, "rb") as f:
            return f.read()
    except OSError as e:  # noqa: BLE001
        log.warning("запись не читается (%s): %r", found, e)
        return None


def cached(text: str) -> bytes | None:
    """Запись для произвольной фразы — только если она уже есть."""
    return audio(text)


def cache_limits() -> tuple[int, float]:
    """Пределы кэша: число файлов и мегабайты."""
    return (config.as_int("replies", "cache_max_files", default=500),
            config.as_float("replies", "cache_max_mb", default=50.0))


def prune_cache() -> int:
    """Убрать старейшие записи, если кэш вышел за пределы.

    Кэш пополняется сам и ничем не ограничен, если его не чистить: новые
    формулировки («Громкость семь», «Свет тридцать») дают новые файлы, и диск
    растёт молча. Вытеснение по времени изменения — самые старые первыми;
    границы задаются в hub.json (`replies.cache_max_files`, `cache_max_mb`).
    """
    folder = _cache_dir()
    try:
        files = [os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(".wav")]
    except OSError:
        return 0
    max_files, max_mb = cache_limits()
    sizes = {}
    total = 0
    for path in files:
        try:
            sizes[path] = os.path.getsize(path)
        except OSError:
            sizes[path] = 0
        total += sizes[path]
    limit_bytes = max_mb * 1024 * 1024
    if len(files) <= max_files and total <= limit_bytes:
        return 0
    files.sort(key=lambda p: os.path.getmtime(p))
    removed = 0
    for path in files:
        if len(files) - removed <= max_files and total <= limit_bytes:
            break
        try:
            os.remove(path)
        except OSError:
            continue
        removed += 1
        total -= sizes[path]
    log.info("кэш реплик: убрано %d старых записей, осталось %d файлов / %.1f МБ",
             removed, len(files) - removed, total / 1048576)
    return removed


def store(text: str, data: bytes) -> None:
    """Сохранить синтезированную короткую реплику в кэш."""
    if not text or not data or len(text) > MAX_CACHE_CHARS:
        return
    folder = _cache_dir()
    target = os.path.join(folder, key(text) + ".wav")
    if os.path.exists(target):
        return
    try:
        os.makedirs(folder, exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
        log.info("реплика в кэше: %r", text[:40])
    except OSError as e:  # noqa: BLE001
        log.warning("кэш реплики не записан (%s): %r", target, e)
        return
    prune_cache()


# ---------- заполнители ----------

def fillers_enabled() -> bool:
    """Заполнители выключены по умолчанию: сначала послушать на живой речи."""
    env = os.environ.get("VOICE_FILLERS")
    default = False if env is None else env.strip().lower() not in ("0", "false", "no", "off", "")
    return config.as_bool("fillers", "enabled", default=default)


def fillers_timing() -> tuple[float, float, float, int]:
    """(первая фраза, минимальная пауза, максимальная пауза, предел числа)."""
    return (
        config.as_float("fillers", "first_s", default=6.0),
        config.as_float("fillers", "min_gap_s", default=7.0),
        config.as_float("fillers", "max_gap_s", default=15.0),
        config.as_int("fillers", "max_count", default=40),
    )
