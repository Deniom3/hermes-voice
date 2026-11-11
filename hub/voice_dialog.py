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
import subprocess
import time
import urllib.error
import urllib.request

try:                      # запуск как модуль
    from . import answer_filter
    from . import fast_route
    from . import config
    from . import replies
    from . import wake_names
except ImportError:       # запуск как скрипт
    import answer_filter
    import fast_route
    import config
    import replies
    import wake_names

log = logging.getLogger("voice-dialog")

# Маркер «действие выполнено, говорить нечего»: быстрые домашние команды
# не озвучиваются, но и ошибкой LLM не считаются.
SILENT = "\x00__silent__"

_TTS = config.section("tts")
TTS_URL = _TTS.get("url") or os.environ.get(
    "TTS_URL", "http://127.0.0.1:8000/v1/audio/speech")
TTS_VOICE = _TTS.get("voice") or "eugene"
SAY_VOLUME = float(_TTS.get("volume", 1.0))
SPEAK_MAX_CHARS = int(_TTS.get("speak_max_chars", 2000))

# Громкость озвучки. Шкала колонки (Master) действует и на музыку, и на
# gst-пайплайн агента, поэтому она — единственная ручка, а усиление в плеере
# равно 1.0. Уровень приводим здесь: у голоса eugene большой пик-фактор
# (−22.6 LUFS при true peak −0.2 dBTP), и одно усиление клиппует. Сначала
# компрессор, затем лимитер; поправка считается по замеру, потому что короткие
# реплики без второй итерации выходили на 2 дБ тише длинных.
SPEECH_TARGET_LUFS = -13.0
SPEECH_CHAIN = "acompressor=threshold=-20dB:ratio=3:attack=5:release=80"
SPEECH_LIMIT = 0.84  # −1.5 dBFS


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


def _ffmpeg(args: list[str], data: bytes) -> bytes:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", *args],
                       input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or b"").decode("utf-8", "replace")[:200])
    return p.stdout


def _lufs(data: bytes, af: str = "") -> float | None:
    """Integrated loudness по ffmpeg loudnorm; None — не удалось измерить."""
    chain = (af + "," if af else "") + "loudnorm=print_format=json"
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", "pipe:0",
                        "-af", chain, "-f", "null", "-"],
                       input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    m = re.search(r'\{[^{}]*"input_i"[^{}]*\}',
                  p.stderr.decode("utf-8", "replace"), re.S)
    if not m:
        return None
    try:
        return float(json.loads(m.group(0))["input_i"])
    except (ValueError, KeyError):
        return None


def normalize_speech(data: bytes) -> bytes:
    """Приводит синтез к SPEECH_TARGET_LUFS; компрессор + лимитер, без клиппинга."""
    try:
        cur = _lufs(data, SPEECH_CHAIN)
        if cur is None:
            return data
        chain = [SPEECH_CHAIN]
        for _ in range(2):
            gain = max(-24.0, min(24.0, SPEECH_TARGET_LUFS - cur))
            step = f"volume={gain:.1f}dB,alimiter=limit={SPEECH_LIMIT}:level=false"
            data = _ffmpeg(["-i", "pipe:0", "-af", ",".join(chain + [step]),
                            "-ar", "48000", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"], data)
            cur = _lufs(data)
            if cur is None or abs(SPEECH_TARGET_LUFS - cur) < 0.7:
                break
            # Компрессор уже применён — дальше только коррекция уровня.
            chain = []
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("нормализация озвучки не удалась, отдаю как есть: %s", e)
        return data


def synthesize(text: str) -> bytes:
    """Синтез с двумя попытками и кэшем готовых коротких реплик.

    Готовая запись берётся из библиотеки (`hub/replies/<голос>/`) или из кэша:
    «Включил», «Комната не задана…» звучат без ожидания синтеза. Длинные
    ответы модели не кэшируются — каждая фраза уникальна.

    Второй синтез нужен потому, что движок падает с 500 на разметке; тогда
    текст жёстко вычищается до слов и простой пунктуации.
    """
    ready = replies.cached(text)
    if ready is not None:
        log.debug("реплика из библиотеки: %r", text[:40])
        return ready
    clean = clean_for_tts(text)
    try:
        data = normalize_speech(_tts_post(clean))
    except urllib.error.HTTPError as e:
        if e.code != 500:
            raise
        hard = re.sub(r"[^\w\s.,!?;:()\-—–]", " ", clean)
        hard = re.sub(r"\s{2,}", " ", hard).strip()
        log.warning("TTS 500 на очищенном тексте, повтор: %r", hard[:60])
        data = normalize_speech(_tts_post(hard))
    replies.store(text, data)
    return data


# ---------- текстовая ступень ----------

_QUESTION_WORDS = tuple(config.as_list("phrases", "question_words", default=(
    "что", "как", "где", "когда", "почему", "зачем", "сколько",
    "кто", "какой", "какая", "какие", "куда", "откуда", "чем", "ли ")))


def looks_like_question(text: str) -> bool:
    """Реплика — вопрос? Тогда окно диалога длиннее: человек может думать."""
    low = text.lower()
    if "?" in text:
        return True
    words = low.split()
    return bool(words) and words[0] in _QUESTION_WORDS


_WAKE_WORDS = tuple(config.as_list("wake", "words", default=(
    "окей", "ок", "okay", "ok", "гермес", "гермис", "гермиос", "герме",
    "гермеса", "дермес", "дёрмес", "вернее", "ермес",
    "girmes", "germes", "germany", "germani", "hermes", "garmin", "s")))

_WAKE_NAME_RE = re.compile(wake_names.pattern())


def has_wake_name(text: str) -> bool:
    """Есть ли в тексте именно ИМЯ, а не просто «окей».

    Ловим любой вариант имени по корню «герм»: GigaAM выдаёт «гермез»,
    «гермяс», «гермион», «гермаз» — все считаются обращением.
    """
    return bool(_WAKE_NAME_RE.search(text.lower()))


# «гермес» / «гермиона» — до 8 букв. Более длинное слово с корнем «герм» —
# это склейка имени со следующим: «Гермес, стоп» ASR отдаёт как «герместоп».
# Считать такую склейку обращением нельзя: срезается вся фраза вместе с
# командой, и человек не понимает, почему его проигнорировали (07.11, 02:46).
_MAX_NAME_LEN = 8


def _name_stem(word: str) -> bool:
    return wake_names.is_stem(word) and len(word) <= _MAX_NAME_LEN


def _is_wake_token(word: str) -> bool:
    return word in _WAKE_WORDS or _name_stem(word)


def is_just_wake(text: str) -> bool:
    """True, если распознано только обращение без самой фразы."""
    # апостроф внутри «о'кей» иначе рвёт слово на «о» и «кей»
    low = text.lower().replace("'", "").replace("’", "")
    words = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in low).split()
    rest = [w for w in words if not _is_wake_token(w)]
    return len("".join(rest)) <= 3


# Фразы «замолчи». Во время ответа это команда обрыва, а не вопрос агенту:
# отправлять её в LLM — значит заставить модель думать над «стоп» и снова
# заговорить через несколько секунд (07.11, 03:08: «Очень гермест.» -> 24 с).
_STOP_WORDS = frozenset((
    "стоп", "stop", "stopp", "хватит", "замолчи", "молчи", "помолчи", "тихо",
    "отмена", "отмени", "отставить", "достаточно", "стоять", "умолкни",
    "заткнись", "halt", "enough", "quiet", "cancel"))

# Обвесы/междометия, которые не несут смысла команды. Это не фильтр мата:
# список нужен только для формы «Стоп, блядь. Стоп. Стоп.» — такие слова
# не должны превращать команду в обычную фразу с содержанием.
_STOP_FILLERS = frozenset((
    "пожалуйста", "пжлст", "ну", "уже", "все", "ладно", "да", "ой",
    "блядь", "блять", "бля", "сука", "нахуй", "хуй", "пиздец",
))


def _normalize_words(text: str) -> list[str]:
    low = text.lower().replace("ё", "е").replace("'", "").replace("’", "")
    return "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in low).split()


def _is_name_noise(word: str) -> bool:
    """Искажённое имя от ASR: «Dermest», «гермез», «germes»."""
    return len(word) <= 10 and ("erm" in word or "ерм" in word)


def _is_stop_token(word: str) -> bool:
    if word in _STOP_WORDS:
        return True
    # «Гермес, стоп» ASR склеивает в «герместоп», а саму команду иногда
    # пишет латиницей: «О'кей, Germes stop» (07.11, 03:46).
    return ((word.startswith("герм") and word.endswith("стоп"))
            or (word.startswith("germ") and word.endswith("stop")))


def is_stop_command(text: str) -> bool:
    """Фраза — команда «замолчи», а не предложение со словом «стоп».

    После обращения и незначащих обвесов не должно остаться ничего, кроме
    стоп-слов: «Стоп, блядь. Стоп. Стоп.» — команда, а «стоп, а который час?»
    и «включи свет, стоп» — нет, там есть содержание. Одиночный «стоп» в
    середине такой фразы не обрывает ход.
    """
    words = _normalize_words(text)
    rest = [w for w in words
            if not _is_wake_token(w)
            and not (_is_name_noise(w) and not _is_stop_token(w))
            and w not in _STOP_FILLERS]
    return bool(rest) and all(_is_stop_token(w) for w in rest)


# Слово для срезания обращения. Кириллица, латиница, без цифр: «окей2»
# обращением не считается.
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+(?:['’][A-Za-zА-Яа-яЁё]+)*")

# Что trimming считает обращением помимо _WAKE_WORDS. Отдельный набор, потому
# что _WAKE_WORDS собран для is_just_wake и содержит мусорные «вернее» и «s»:
# срезать их из начала фразы нельзя.
_LEAD_NAME_FORMS = ("hermes", "germes", "girmes", "ермес", "хермес")


def _strip_punct() -> str:
    """Символы, которые срезаются между словами обращения и после него."""
    return " \t\r\n,.;:!?—–-…\"'«»()"


def _lead_wake(word: str) -> bool:
    """Похоже ли слово на обращение в начале фразы.

    Имя проверяется тем же критерием, что и `voice_core.name_like`
    (wake_names.is_name): ASR-искажение «Дермис» должно срезаться, иначе
    фраза-триггер уходит в реестр вместе с именем и не совпадает.
    """
    w = word.lower().replace("ё", "е").replace("'", "").replace("’", "")
    if len(w) < 2:
        return False
    return (w in _WAKE_WORDS or _name_stem(w) or w in _LEAD_NAME_FORMS
            or wake_names.is_name(word))


def strip_wake(text: str) -> str:
    """Срезать ведущее обращение и вернуть остаток фразы.

    Режем только начало: «Окей, Гермес, спокойной ночи» → «спокойной ночи».
    Середину и конец не трогаем — «включи трек Гермес» должно остаться как
    есть. Не больше трёх слов: столько же, сколько проверяет обращение в
    `voice_core.name_like`.

    Обращение в HA и в LLM должно уходить одним и тем же текстом, поэтому
    функция одна на оба потребителя.
    """
    s = text or ""
    punct = _strip_punct()
    pos = 0
    cut = 0                 # конец последнего срезанного обращения
    n = len(s)
    for _ in range(3):
        start = pos
        while pos < n and s[pos] in punct:
            pos += 1
        m = _WORD_RE.match(s, pos)
        if not m or not _lead_wake(m.group(0)):
            pos = start
            break
        pos = m.end()
        cut = pos
    return s[cut:].lstrip(punct).strip()


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
        self.stats = {"first_audio": None, "spoken": 0, "tts": 0.0,
                      "chunks": 0, "suppressed": 0}

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
        try:
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
                        if answer_filter.is_technical(seg):
                            # Служебный текст шлюза («Operation interrupted…»)
                            # голосом не произносим.
                            stats["suppressed"] += 1
                            continue
                        stats["spoken"] += len(seg)
                        await text_q.put(seg)
                tail = buf.strip()
                if tail and stats["spoken"] < limit:
                    tail = tail[:limit - stats["spoken"]]
                    if answer_filter.is_technical(tail):
                        stats["suppressed"] += 1
                    else:
                        stats["spoken"] += len(tail)
                        await text_q.put(tail)
                if stats["suppressed"] and not stats["spoken"]:
                    await text_q.put("Не получилось ответить, попробуй ещё раз.")
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

            await asyncio.gather(syn, play)
        finally:
            # Отмена хода (перебивание, «стоп», кнопка) не должна оставлять
            # живые задачи синтеза и плеера: до `gather` управление уже не
            # дойдёт, и они продолжат отдавать звук в отменённый ход.
            for t in (syn, play):
                if not t.done():
                    t.cancel()

        await self._emit("turn_end", text=self.answer,
                         cancelled=self.cancelled,
                         chunks=stats["chunks"])
        log.info("озвучено кусков %d, TTS суммарно %.2f с, цикл %.2f с",
                 stats["chunks"], stats["tts"], time.time() - t0)
        return self.answer
