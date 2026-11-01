#!/usr/bin/env python3
"""Общий слой диалога голосового контура.

Здесь живёт всё, что одинаково для колонки и панели: очистка текста для TTS,
синтез через шлюз 139, разбиение ответа агента на предложения и сам ход
«текст → агент → поток аудио».

Транспорт снаружи: ход не знает, куда отдавать звук — он вызывает on_audio().
Так один и тот же код обслуживает и WebSocket колонки, и WebSocket панели.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

try:                      # запуск как модуль
    from . import fast_route
except ImportError:       # запуск как скрипт
    import fast_route

log = logging.getLogger("voice-dialog")

# Маркер «действие выполнено, говорить нечего»: быстрые домашние команды
# не озвучиваются, но и ошибкой LLM не считаются.
SILENT = "\x00__silent__"

TTS_URL = os.environ.get("TTS_URL", "http://127.0.0.1:8000/v1/audio/speech")
TTS_VOICE = "eugene"
SAY_VOLUME = 4.0
SPEAK_MAX_CHARS = 400


# ---------- TTS ----------

def clean_for_tts(text: str) -> str:
    """Убирает markdown: движок падает с 500 «Некорректная разметка эмфазы»
    на **жирном**, списках, заголовках и ссылках."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_]{1,3}", "", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s{0,3}[-•*+]\s+", "", text)
    text = re.sub(r"(?m)^\s{0,3}>\s*", "", text)
    text = re.sub(r"(?<=[^.!?:;])\s*\n\s*", ". ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _tts_post(text: str) -> bytes:
    body = json.dumps({
        "model": "silero", "voice": TTS_VOICE, "input": text,
        "response_format": "mp3",
    }).encode()
    req = urllib.request.Request(TTS_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def synthesize(text: str) -> bytes:
    """Синтез с двумя попытками: движок падает на разметке (500), поэтому второй
    раз текст жёстко вычищается до слов и простой пунктуации."""
    clean = clean_for_tts(text)
    try:
        return _tts_post(clean)
    except urllib.error.HTTPError as e:
        if e.code != 500:
            raise
        hard = re.sub(r"[^\w\s.,!?;:()\-—–]", " ", clean)
        hard = re.sub(r"\s{2,}", " ", hard).strip()
        log.warning("TTS 500 на очищенном тексте, повтор: %r", hard[:60])
        return _tts_post(hard)


# ---------- текстовая ступень ----------

_QUESTION_WORDS = ("что", "как", "где", "когда", "почему", "зачем", "сколько",
                   "кто", "какой", "какая", "какие", "куда", "откуда", "чем", "ли ")


def looks_like_question(text: str) -> bool:
    """Реплика — вопрос? Тогда окно диалога длиннее: человек может думать."""
    low = text.lower()
    if "?" in text:
        return True
    words = low.split()
    return bool(words) and words[0] in _QUESTION_WORDS


_WAKE_WORDS = ("окей", "ок", "okay", "ok", "гермес", "гермис", "гермиос", "герме",
               "гермеса", "дермес", "дёрмес", "вернее", "ермес",
               "girmes", "germes", "germany", "germani", "hermes", "garmin", "s")

_WAKE_NAME_RE = re.compile(r"герм|hermes|germes|girmes|ермес")


def has_wake_name(text: str) -> bool:
    """Есть ли в тексте именно ИМЯ, а не просто «окей».

    Ловим любой вариант имени по корню «герм»: GigaAM выдаёт «гермез»,
    «гермяс», «гермион», «гермаз» — все считаются обращением.
    """
    return bool(_WAKE_NAME_RE.search(text.lower()))


def _is_wake_token(word: str) -> bool:
    return word in _WAKE_WORDS or word.startswith("герм")


def is_just_wake(text: str) -> bool:
    """True, если распознано только обращение без самой фразы."""
    # апостроф внутри «о'кей» иначе рвёт слово на «о» и «кей»
    low = text.lower().replace("'", "").replace("’", "")
    words = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in low).split()
    rest = [w for w in words if not _is_wake_token(w)]
    return len("".join(rest)) <= 3


_SENT_RE = re.compile(r"[.!?…](?=\s|$)")


def split_sentences(buf: str, soft: int = 220):
    """Отрезать от буфера готовое к озвучке предложение.

    Возвращает (кусок, остаток); пустой кусок — резать нечего.
    Слишком короткие обрывки («Ок.») не отдаём: ждём продолжения.
    """
    for m in _SENT_RE.finditer(buf):
        end = m.end()
        if end >= 20:
            return buf[:end].strip(), buf[end:].lstrip()
    if len(buf) >= soft:
        cut = max(buf.rfind(", "), buf.rfind(" — "), buf.rfind("; "), buf.rfind(": "))
        if cut >= 80:
            return buf[:cut + 1].strip(), buf[cut + 1:].lstrip()
        sp = buf.rfind(" ", 80)
        if sp > 0:
            return buf[:sp].strip(), buf[sp + 1:].lstrip()
    return "", buf


# ---------- ход диалога ----------

def accepts_events(fn) -> bool:
    """Принимает ли функция потокового ответа колбэк событий.

    Нужно, потому что в тестах и старых вызовах stream_fn принимает только текст.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "on_event" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class DialogTurn:
    """Один ход: вопрос уже распознан, осталось получить ответ и озвучить.

    Транспорт задаётся колбэком on_audio(chunk) — он получает готовые куски
    звука по мере синтеза. События (начало/конец) — через on_event.
    """

    def __init__(self, text: str, on_audio=None, on_event=None,
                 max_chars: int = SPEAK_MAX_CHARS, stream_fn=None,
                 tts_fn=None):
        self.text = text
        self.on_audio = on_audio
        self.on_event = on_event
        self.max_chars = max_chars
        self.stream_fn = stream_fn or fast_route.stream
        self.tts_fn = tts_fn or synthesize
        self.cancelled = False
        self.task = None
        self.answer = ""
        # Быстрая команда выполнена, но озвучивать её не нужно.
        self.silent = False
        self.stats = {"first_audio": None, "spoken": 0, "tts": 0.0, "chunks": 0}

    def cancel(self):
        self.cancelled = True
        if self.task is not None and not self.task.done():
            self.task.cancel()

    async def _emit(self, kind: str, **data):
        if self.on_event is None:
            return
        try:
            await self.on_event({"type": kind, **data})
        except Exception as e:  # noqa: BLE001
            log.warning("on_event(%s): %s", kind, e)

    async def _agent_event(self, ev: dict) -> None:
        """Событие от агента превращаем в «что делаю» для колонки и панели."""
        if ev.get("type") == "tool":
            await self._emit("tool", name=ev.get("name"), id=ev.get("id"),
                             state=ev.get("state"))

    async def run(self) -> str:
        self.task = asyncio.current_task()
        t0 = time.time()
        log.info("фраза → %r", self.text)
        if len(self.text.strip()) < 2:
            return ""

        text_q: asyncio.Queue = asyncio.Queue()
        audio_q: asyncio.Queue = asyncio.Queue()
        stats = self.stats
        limit = self.max_chars

        async def synthesizer():
            while True:
                seg = await text_q.get()
                if seg is None:
                    await audio_q.put(None)
                    return
                t1 = time.time()
                try:
                    audio = await asyncio.to_thread(self.tts_fn, seg)
                except Exception as e:  # noqa: BLE001
                    log.warning("TTS недоступен на куске %r: %s", seg[:40], e)
                    continue
                stats["tts"] += time.time() - t1
                stats["chunks"] += 1
                await audio_q.put(audio)

        async def player():
            while True:
                audio = await audio_q.get()
                if audio is None:
                    return
                if stats["first_audio"] is None:
                    stats["first_audio"] = time.time()
                    log.info("первый звук через %.2f с от вопроса",
                             stats["first_audio"] - t0)
                if self.on_audio is not None:
                    await self.on_audio(audio)

        syn = asyncio.create_task(synthesizer())
        play = asyncio.create_task(player())
        parts: list[str] = []
        buf = ""
        await self._emit("turn_start", text=self.text)
        stream = self.stream_fn
        if accepts_events(stream):
            pieces = stream(self.text, on_event=self._agent_event)
        else:
            pieces = stream(self.text)
        try:
            async for piece in pieces:
                if piece == SILENT:
                    self.silent = True
                    continue
                parts.append(piece)
                if stats["spoken"] >= limit:
                    continue
                buf += piece
                while stats["spoken"] < limit:
                    seg, buf = split_sentences(buf)
                    if not seg:
                        break
                    seg = seg[:limit - stats["spoken"]]
                    stats["spoken"] += len(seg)
                    await text_q.put(seg)
            tail = buf.strip()
            if tail and stats["spoken"] < limit:
                tail = tail[:limit - stats["spoken"]]
                stats["spoken"] += len(tail)
                await text_q.put(tail)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("агент: %s", e)
        finally:
            text_q.put_nowait(None)

        self.answer = "".join(parts).strip()
        if self.answer:
            log.info("ответ %.2f с, %d симв. → %r", time.time() - t0,
                     len(self.answer), self.answer[:200])

        try:
            await asyncio.gather(syn, play)
        finally:
            for t in (syn, play):
                if not t.done():
                    t.cancel()

        await self._emit("turn_end", text=self.answer,
                         cancelled=self.cancelled,
                         chunks=stats["chunks"])
        log.info("озвучено кусков %d, TTS суммарно %.2f с, цикл %.2f с",
                 stats["chunks"], stats["tts"], time.time() - t0)
        return self.answer
