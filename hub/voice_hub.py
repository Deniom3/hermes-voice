#!/usr/bin/env python3
"""Голосовой хаб: единая точка входа для клиентов (колонки, панель).

Один WebSocket на клиента. Внутри — общий речевой автомат voice_core.SpeechCore
и общий слой диалога voice_dialog. Транспорт не знает про устройства, а
устройства не знают про состояния: всё в одном канале.

Протокол (WebSocket, порт 8796, путь /ws):

  клиент → сервер
    {"t":"hello","name":"lite-1","role":"station","threshold":0.95,
     "gain":10.0,"agc":true,"write_audio":true}
    <binary>            PCM s16le, 16000 Гц, моно — микрофон
    {"t":"interrupt"}   прервать ответ
    {"t":"cmd_result","id":"c1","ok":true,"err":null}
    {"t":"pong"}

  сервер → клиент
    <binary>            кусок звука ответа (mp3, как отдаёт TTS)
    {"t":"state","state":"collecting","origin":"wake"}
    {"t":"cmd","id":"c1","cmd":"led","name":"hermes_listening"}
    {"t":"cmd","id":"c2","cmd":"say","volume":4.0}   (звук идёт binary-кадром)
    {"t":"turn_start","text":"..."}
    {"t":"turn_end","text":"...","cancelled":false}
    {"t":"error","what":"llm"|"tts"}
    {"t":"ping"}

HTTP рядом: GET /health, POST /interrupt, POST /cmd (то же, что по WS).
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import random
import time
import wave

from aiohttp import WSMsgType, web

try:                      # запуск как модуль
    from . import voice_capture
    from . import voice_dialog
    from . import voice_fillers
    from . import wake_probe
    from .voice_core import CoreConfig, DetectorLevel, LocalWakeGate, SpeechCore, State
    from .voice_loop import AGC, transcribe
except ImportError:       # запуск как скрипт
    import voice_capture
    import voice_dialog
    import voice_fillers
    import wake_probe
    from voice_core import CoreConfig, DetectorLevel, LocalWakeGate, SpeechCore, State
    from voice_loop import AGC, transcribe

log = logging.getLogger("voice-hub")

CONNS: dict[str, "Conn"] = {}
OUT_DIR = "/tmp/station-raw"
# Сколько ждать после IDLE, прежде чем вернуть музыку на колонку. Пауза нужна,
# чтобы «окей, Гермес» и следующая за ним фраза не накладывались на музыку.
RESUME_DELAY = float(os.environ.get("VOICE_RESUME_DELAY", "3.0"))


def should_resume_music(follow_up: bool, collecting: bool, state) -> bool:
    """Пора ли вернуть музыку на колонку.

    Возвращаем только когда ход действительно закончен: фразу не ждём, сборщик
    не работает, состояние IDLE. Иначе музыка залезет в микрофон посреди фразы.
    """
    return not follow_up and not collecting and state is State.IDLE


def wav_seconds(data: bytes) -> float:
    """Длительность WAV-куска: по ней гасим кольцо, когда речь доиграет."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            rate = w.getframerate() or 1
            return w.getnframes() / rate
    except Exception:  # noqa: BLE001
        return len(data) / 96000.0      # 48 кГц, 16 бит, моно


class Conn:
    """Одно подключение клиента: транспорт + речевой автомат."""

    def __init__(self, ws, name: str, role: str, cfg: CoreConfig,
                 gain: float, agc: bool, write_audio: bool):
        self.ws = ws
        self.name = name
        self.role = role
        self.cfg = cfg
        self.agc = AGC() if agc else None
        self.write_audio = write_audio
        self.audio_bytes = 0
        self._frames = 0
        self.turn = None
        self.started = time.time()
        self._seq = 0
        self._waiters: dict[str, asyncio.Future] = {}
        self._f = None
        self._rawf = None

        # Сбор событий для дообучения: в памяти кольцо 8 с, на диск — только
        # окна вокруг событий. Непрерывный поток пишется лишь при отладке.
        self.ring = voice_capture.Ring(seconds=8.0)
        self.collector = voice_capture.Collector(
            root=os.environ.get("KWS_TRAIN_DIR", "/srv/kws/train"),
            enabled=os.environ.get("KWS_TRAIN", "1") != "0",
        )
        self.retro = voice_capture.RetroScanner()
        # Замер «почти срабатываний»: только счётчики в памяти, аудио не пишем.
        # Интервал отчёта в журнал — 10 минут; короче только для разовой проверки.
        self.probe = wake_probe.ScoreProbe(
            report_s=float(os.environ.get("WAKE_PROBE_REPORT_S", "600")))
        # Промахи: пик был, порог не превышен — это и есть данные, которых
        # не хватало для дообучения. Пишем в misses, поведение не меняется.
        self.miss = voice_capture.MissWatcher(
            min_score=float(os.environ.get("KWS_MISS_MIN", "0.90")),
            high=cfg.wake_threshold,
            per_hour=int(os.environ.get("KWS_MISS_PER_HOUR", "120")),
        )
        self._snap = b""
        self._snap_peaks: list[tuple[float, float]] = []
        self._snap_score = 0.0
        self._resume_task: asyncio.Task | None = None
        # До какого момента колонка ещё проигрывает ответ (monotonic-время).
        # Кольцо нельзя гасить по отправке последнего чанка: звук идёт из буфера.
        self._audio_until = 0.0
        self._led_task: asyncio.Task | None = None
        # Играла ли музыка до активации: play шлём только если было что вернуть.
        self._was_playing = False

        self.core = SpeechCore(
            wake=LocalWakeGate(threshold=cfg.wake_threshold),
            asr=self._asr,
            on_state=self._on_state,
            on_phrase=self._on_phrase,
            cfg=cfg,
        )
        self.core.level = DetectorLevel(gain=gain)

        if self.write_audio and os.environ.get("VOICE_DUMP_STREAM") == "1":
            os.makedirs(OUT_DIR, exist_ok=True)
            path = os.path.join(OUT_DIR, f"{name}-{int(time.time())}.pcm")
            self._f = open(path, "wb")
            self._rawf = open(path.replace(".pcm", ".raw.pcm"), "wb")
            log.info("%s: аудио (отладка) → %s", name, path)

    # ---------- отправка ----------

    async def send_json(self, obj: dict) -> None:
        try:
            await self.ws.send_str(json.dumps(obj, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            log.debug("%s: не отправилось %s: %s", self.name, obj.get("t"), e)

    async def send_binary(self, data: bytes) -> None:
        try:
            await self.ws.send_bytes(data)
        except Exception as e:  # noqa: BLE001
            log.debug("%s: звук не отправился: %s", self.name, e)

    async def command(self, payload: dict, timeout: float = 120.0) -> dict:
        """Команда устройству с подтверждением (id ↔ cmd_result)."""
        self._seq += 1
        cid = f"c{self._seq}"
        fut = asyncio.get_running_loop().create_future()
        self._waiters[cid] = fut
        await self.send_json(dict(payload, t="cmd", id=cid))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiters.pop(cid, None)

    # ---------- голосовое ядро ----------

    async def _asr(self, pcm):
        t0 = time.time()
        text = await asyncio.to_thread(transcribe, pcm)
        # Два разных числа: время работы и длина аудио. Раньше в лог шла только
        # длина, и строка «ASR 2.5 с» читалась как «распознавание заняло 2.5 с».
        log.info("%s: ASR %.2f с (аудио %.1f с) → %r",
                 self.name, time.time() - t0, len(pcm) / 16000, text)
        return text

    async def _on_state(self, state, origin, score):
        await self.send_json({"t": "state", "state": state.value,
                              "origin": origin, "score": score})
        # Панель — такой же клиент ядра, но без индикации и без сбора датасета:
        # команды устройству (led/sound/pause) и снимки кольца ей не нужны,
        # а command() ждал бы ответа 15 секунд и тормозил весь ход.
        if self.role != "station":
            return
        # Снимок последних секунд кольца — источник окна события для сбора.
        # Делается на активации, то есть при входе в COLLECTING (состояния
        # «проверка ключа» больше нет: имя проверяется по тексту фразы).
        if state is State.COLLECTING and origin == "wake" and not self._snap:
            self._snap = self.ring.last(6.0)
            self._snap_score = float(score or 0.0)
            self._snap_peaks = []
            self._wake_at = asyncio.get_running_loop().time()
            self.miss.reset()
            asyncio.create_task(self._retro_scan(self._snap))
        if state is State.COLLECTING:
            self._cancel_led_task()
            self._audio_until = 0.0
            if origin == "wake":
                res = await self.command({"cmd": "pause"}, timeout=15) or {}
                # Запоминаем, играла ли музыка: если нет — возвращать нечего,
                # иначе play включал музыку там, где её изначально не было.
                self._was_playing = bool(res.get("was_playing"))
                log.info("%s: пауза музыки, было воспроизведение=%s",
                         self.name, self._was_playing)
                await self.command({"cmd": "sound", "name": "spotter_start"}, timeout=15)
            await self.command({"cmd": "led", "name": "hermes_listening"}, timeout=15)
        elif state is State.BUSY:
            self._cancel_led_task()
            await self.command({"cmd": "led", "name": "hermes_thinking"}, timeout=15)
        elif state is State.DIALOG_WAIT:
            self._clear_led_after_audio()
        elif state is State.IDLE:
            self._clear_led_after_audio()
            if origin == "no_wake_name":
                # Детектор сработал, но обращения в тексте нет — это ложное
                # срабатывание, то есть самый ценный hard-негатив: акустически
                # похоже на имя, но имени не было. Кладём именно в negatives;
                # в unclear попадает только то, что не удалось разобрать.
                # Текст обязателен: без него по файлу не понять, на чём
                # сработало, и негатив теряет смысл при разборе датасета.
                # Музыку при этом обязательно возвращаем: её поставили на паузу
                # при активации, и без этого ложное срабатывание глушило её навсегда.
                asyncio.create_task(self._save_settled(
                    "negatives", self.core.last_text, "no_name"))
                await self._return_music()
                return
            if not self.core.follow_up:
                # Не возвращаем музыку сразу: если человек сказал только «окей,
                # Гермес» и продолжил фразу, музыка вернулась бы ему в микрофон
                # через полсекунды, фраза утонула бы, а ответа не было.
                self._plan_resume_music()

    def _cancel_led_task(self) -> None:
        task = getattr(self, "_led_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._led_task = None

    def _clear_led_after_audio(self) -> None:
        """Погасить кольцо, когда колонка доиграет ответ.

        Последний чанк уходит на устройство раньше, чем заканчивается звук:
        gst играет из буфера, поэтому гасить по отправке нельзя.
        """
        self._cancel_led_task()
        loop = asyncio.get_running_loop()
        delay = max(0.0, getattr(self, "_audio_until", 0.0) - loop.time())

        async def wait() -> None:
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await self.command({"cmd": "led", "name": ""}, timeout=10)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: led не погашен: %s", self.name, e)

        self._led_task = asyncio.create_task(wait())

    def _plan_resume_music(self) -> None:
        if self._resume_task is None or self._resume_task.done():
            self._resume_task = asyncio.create_task(self._resume_music())

    async def _return_music(self) -> None:
        """Музыка после ложной активации: пауза была, фразы не будет."""
        if not self.core.follow_up:
            self._plan_resume_music()

    async def _resume_music(self) -> None:
        """Вернуть музыку после паузы — только если ход действительно кончился.

        Условия: ожидание фразы не выставлено, сборщик не работает, состояние
        всё ещё IDLE. Если за это время человек начал говорить, музыка остаётся
        на паузе и в микрофон не лезет.
        """
        await asyncio.sleep(RESUME_DELAY)
        if not should_resume_music(self.core.follow_up, self.core.coll is not None,
                                   self.core.state):
            return
        if not getattr(self, "_was_playing", False):
            # Музыки не было до активации — не включаем её «из ниоткуда».
            log.info("%s: музыку не возвращаю — до активации не играла", self.name)
            return
        await self.command({"cmd": "play"}, timeout=15)
        self._was_playing = False
        log.info("%s: музыка возвращена", self.name)

    async def _retro_scan(self, pcm: bytes) -> None:
        """Ищем повторы ключа в снимке кольца пониженным порогом."""
        if not pcm or not self.retro.available:
            return
        try:
            peaks = await asyncio.to_thread(self.retro.scan, pcm, 0.55)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: ретро-проверка не удалась: %s", self.name, e)
            return
        self._snap_peaks = peaks
        if len(peaks) >= 2:
            log.info("%s: ретро %d вхождений ключа за 6 с: %s", self.name, len(peaks),
                     ", ".join("%.2fс/%.2f" % (t, p) for t, p in peaks[:5]))

    async def _settle_audio(self, need: float = 1.0) -> None:
        """Дать кольцу добрать хвост после пика.

        Снимок берётся в момент активации, то есть пик всегда в его конце:
        окно выходило 1.41 с — 1.4 с до пика и ничего после, и такими же
        уходили все positives. Для обучения нужен баланс «до» и «после».
        """
        at = getattr(self, "_wake_at", 0.0)
        if not at:
            return
        wait = need - (asyncio.get_running_loop().time() - at)
        if wait > 0:
            await asyncio.sleep(wait)

    def _event_window(self) -> tuple[bytes, float]:
        """Свежее кольцо и позиция пика в нём (секунды от начала)."""
        snap = self.ring.last(6.0) if getattr(self, "ring", None) else b""
        if not snap or len(snap) < 3200:
            snap = self._snap
        if not snap:
            return b"", 0.0
        secs = len(snap) / 2 / 16000
        at = getattr(self, "_wake_at", 0.0)
        if not at:
            # без метки снимок считаем целиком «до пика»
            return snap, max(0.0, secs - 1.0)
        elapsed = max(0.0, asyncio.get_running_loop().time() - at)
        return snap, max(0.0, secs - elapsed)

    async def _save_miss(self, peak_at: float, peak_score: float) -> None:
        """Сохранить окно промаха: пик был, но порог не превышен."""
        snap = self.ring.last(6.0)
        if not snap:
            return
        delay = max(0.0, time.monotonic() - peak_at)
        peak = max(0.0, len(snap) / 2 / 16000 - delay)
        # Пик 0.70–0.95 ловит любой звук, поэтому промах подтверждаем текстом:
        # без обращения в распознанном это посторонняя речь, а не промах слова.
        window = voice_capture.crop_around(snap, peak)
        try:
            text = await self._asr(window.tobytes())
        except Exception as e:  # noqa: BLE001
            log.warning("%s: промах не проверен распознаванием: %s", self.name, e)
            return
        if not voice_dialog.has_wake_name(text or ""):
            log.info("%s: промах пик %.3f — не обращение (%r), не пишу",
                     self.name, peak_score, (text or "")[:60])
            return
        meta = {"client": self.name, "text": text, "reason": "near_miss",
                "score": round(peak_score, 3)}
        try:
            path = self.collector.save("misses", snap, peak, meta)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: промах не сохранён: %s", self.name, e)
            return
        if path:
            log.info("%s: промах пик %.3f → %s", self.name, meta["score"], path)

    async def _save_settled(self, cls: str, text: str, reason: str) -> None:
        """Дождаться хвоста после пика и записать событие — вне пути ответа."""
        await self._settle_audio()
        self._save_event(cls, text, reason)

    def _save_event(self, cls: str, text: str, reason: str) -> None:
        """Сохранить окно события. Непрерывный поток при этом не пишется."""
        snap, peak = self._event_window()
        if not snap:
            return
        meta = {
            "client": self.name,
            "text": text,
            "reason": reason,
            "score": round(self._snap_score, 3),
            "retro_peaks": len(self._snap_peaks),
            "retro": [{"t": round(t, 2), "p": round(p, 3)}
                      for t, p in self._snap_peaks[:6]],
        }
        try:
            path = self.collector.save(cls, snap, peak, meta)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: событие не сохранено: %s", self.name, e)
            return
        if path:
            log.info("%s: событие %s → %s", self.name, cls, path)
        self._snap = b""

    async def _on_phrase(self, text: str, origin: str, had_wake: bool):
        log.info("%s: фраза %r (origin=%s, had_wake=%s)", self.name, text, origin, had_wake)
        if origin == "wake":
            # Сохранение не должно задерживать ответ: ждём хвост в фоне.
            asyncio.create_task(self._save_settled(
                voice_capture.classify(text), text, "wake"))
        if voice_dialog.is_just_wake(text):
            log.info("%s: только обращение, жду фразу", self.name)
            self.core.expect_followup()
            return
        if origin not in ("dialog", "followup", "button") and not had_wake \
                and not voice_dialog.has_wake_name(text):
            log.info("%s: нет обращения, игнор: %r", self.name, text[:80])
            return
        await self._speak(text)

    async def _speak(self, text: str):
        first_audio = asyncio.Event()

        async def on_audio(chunk: bytes):
            if self.role == "station":
                if not first_audio.is_set():
                    # Кольцо должно гореть «говорит», пока звучит ответ,
                    # а не только пока сервер отправляет чанки.
                    try:
                        await self.command({"cmd": "led", "name": "hermes_speaking"}, timeout=15)
                    except Exception as e:  # noqa: BLE001
                        log.warning("%s: led speaking не показан: %s", self.name, e)
                self._audio_until = (max(asyncio.get_running_loop().time(), self._audio_until)
                                     + wav_seconds(chunk))
            # Первый звук ответа гасит заполнители: дальше они только мешают.
            first_audio.set()
            # Команда «say» нужна только колонке: она поднимает динамик.
            # Панель просто получает звук бинарным кадром.
            if self.role == "station":
                await self.command({"cmd": "say", "volume": voice_dialog.SAY_VOLUME},
                                   timeout=180)
            await self.send_binary(chunk)

        async def on_event(ev: dict):
            # Одно событие — два вида индикации: панель рисует его в UI,
            # колонка показывает кольцом. Формат общий, поэтому поведение
            # одинаковое на обоих клиентах.
            await self.send_json({"t": ev.get("type", "event"), **ev})
            if self.role == "station" and ev.get("type") == "tool":
                led = "hermes_tool" if ev.get("state") == "start" else "hermes_thinking"
                try:
                    await self.command({"cmd": "led", "name": led}, timeout=15)
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: led %s не показан: %s", self.name, led, e)

        filler_task = None
        if voice_fillers.enabled():
            filler_task = asyncio.create_task(self._fill_silence(first_audio))

        self.turn = voice_dialog.DialogTurn(text, on_audio=on_audio,
                                            on_event=on_event)
        try:
            answer = await self.turn.run()
            silent = getattr(self.turn, "silent", False)
        except asyncio.CancelledError:
            log.info("%s: ход отменён", self.name)
            answer = ""
            silent = False
        finally:
            if filler_task is not None:
                filler_task.cancel()
            self.turn = None

        if not answer:
            if silent:
                log.info("%s: команда выполнена молча", self.name)
                return
            await self.send_json({"t": "error", "what": "llm"})
            return
        if self.turn is None and self.core.state is not State.BUSY:
            pass
        self.core.open_dialog(voice_dialog.looks_like_question(text))

    async def _fill_silence(self, first_audio: asyncio.Event) -> None:
        """Заготовки в тишине, пока агент не выдал первый звук ответа.

        Включается только при VOICE_FILLERS=1: пока не отлажено, по умолчанию
        молчим. Первая фраза — если ответ задержался дольше FIRST секунд,
        дальше пауза случайная, чтобы не тараторить.
        """
        try:
            await asyncio.wait_for(first_audio.wait(), timeout=voice_fillers.FIRST)
            return
        except asyncio.TimeoutError:
            pass
        said = 0
        while said < voice_fillers.MAX_COUNT and not first_audio.is_set():
            phrase = voice_fillers.take(voice_fillers.THINK)
            data = voice_fillers.audio(phrase)
            if data:
                if self.role == "station":
                    await self.command({"cmd": "say",
                                        "volume": voice_dialog.SAY_VOLUME}, timeout=30)
                await self.send_binary(data)
                said += 1
                log.info("%s: заполнитель %r", self.name, phrase)
            gap = random.uniform(voice_fillers.MIN_GAP, voice_fillers.MAX_GAP)
            try:
                await asyncio.wait_for(first_audio.wait(), timeout=gap)
                return
            except asyncio.TimeoutError:
                continue

    async def interrupt(self):
        if self.turn is not None:
            self.turn.cancel()
        self.turn = None
        await self.core.interrupt()
        if self.role == "station":
            await self.command({"cmd": "led", "name": ""}, timeout=10)

    # ---------- цикл соединения ----------

    async def _audio_worker(self):
        """Отдельная задача: обработка звука не блокирует чтение команд.

        Иначе получается дедлок: on_state ждёт подтверждения команды, а
        подтверждение приходит в том же сокете, который занят обработкой звука.
        """
        while True:
            raw = await self._q.get()
            try:
                data = self.agc.process(raw) if self.agc is not None else raw
                self.ring.add(raw)
                if self._f is not None:
                    self._f.write(data)
                if self._rawf is not None:
                    self._rawf.write(raw)
                self.audio_bytes += len(data)
                self._frames += 1
                if self._frames % 50 == 0:
                    # без сброса хвост записи теряется при обрыве
                    for f in (self._f, self._rawf):
                        if f is not None:
                            try:
                                f.flush()
                            except Exception:  # noqa: BLE001
                                pass
                await self.core.feed(raw, data)
                score = self.core.last_score
                self.probe.offer(score)
                if self.role == "station":
                    miss = self.miss.offer(score)
                    if miss is not None:
                        asyncio.create_task(self._save_miss(*miss))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("%s: ошибка обработки кадра: %s", self.name, e)

    async def run(self):
        log.info("%s (%s): подключён", self.name, self.role)
        self._q: asyncio.Queue = asyncio.Queue(maxsize=256)
        worker = asyncio.create_task(self._audio_worker())
        probe = asyncio.create_task(self._probe_loop())
        try:
            async for msg in self.ws:
                if msg.type == WSMsgType.BINARY:
                    try:
                        self._q.put_nowait(msg.data)
                    except asyncio.QueueFull:
                        log.warning("%s: очередь звука переполнена, кадр отброшен",
                                    self.name)
                elif msg.type == WSMsgType.TEXT:
                    await self._on_text(msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("%s: поток прерван (%s)", self.name, e)
        finally:
            worker.cancel()
            probe.cancel()
            if self.turn is not None:
                self.turn.cancel()
            await self.core.interrupt()
            for f in (self._f, self._rawf):
                if f is not None:
                    try:
                        f.close()
                    except Exception:  # noqa: BLE001
                        pass
            log.info("%s: отключён, аудио %.1f МБ", self.name,
                     self.audio_bytes / 1048576)

    async def _probe_loop(self) -> None:
        """Раз в минуту проверяем, не пора ли отдать строку замера в журнал."""
        try:
            while True:
                await asyncio.sleep(60)
                line = self.probe.report()
                if line:
                    log.info("%s: %s", self.name, line)
        except asyncio.CancelledError:
            raise

    async def _on_text(self, text: str):
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return
        kind = msg.get("t")
        if kind == "cmd_result":
            fut = self._waiters.get(msg.get("id"))
            if fut and not fut.done():
                fut.set_result(msg)
        elif kind == "interrupt":
            asyncio.create_task(self.interrupt())
        elif kind == "start":
            # Явный старт хода без ключевого слова: кнопка/орб панели.
            asyncio.create_task(self.core.begin())
        elif kind == "text" and msg.get("text"):
            # отладка и серверные уведомления: синтезировать текст без ASR
            asyncio.create_task(self._speak(str(msg["text"])))
        elif kind == "ping":
            await self.send_json({"t": "pong"})


# ---------- HTTP ----------

def _pick(request: web.Request) -> Conn | None:
    name = request.query.get("name")
    if name:
        return CONNS.get(name)
    return next(iter(CONNS.values()), None)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8 * 1024 * 1024)
    await ws.prepare(request)

    hello: dict = {}
    try:
        first = await asyncio.wait_for(ws.receive(), timeout=10)
        if first.type is WSMsgType.TEXT:
            hello = json.loads(first.data)
        elif first.type is WSMsgType.BINARY:
            hello = {}
    except (asyncio.TimeoutError, json.JSONDecodeError):
        hello = {}

    name = str(hello.get("name") or f"client-{int(time.time())}")
    role = str(hello.get("role") or "station")
    cfg = CoreConfig(
        wake_threshold=float(hello.get("threshold", 0.95)),
        # Пауза перед проверкой конца и страховка по тишине: на 0.5 с колонка
        # закрывала фразу, пока человек ещё не договорил, и команда уходила
        # в дом мгновенно — быстрее, чем он успевал продолжить.
        silence_ms=int(os.environ.get("VOICE_SILENCE_MS", "900")),
        hard_silence_ms=int(os.environ.get("VOICE_HARD_SILENCE_MS", "3500")),
    )
    conn = Conn(
        ws, name, role, cfg,
        gain=float(hello.get("gain", 10.0)),
        agc=bool(hello.get("agc", role == "station")),
        write_audio=bool(hello.get("write_audio", True)),
    )
    CONNS[name] = conn
    log.info("%s: hello %s", name, hello)
    await conn.send_json({"t": "hello_ok", "name": name})
    try:
        await conn.run()
    finally:
        CONNS.pop(name, None)
        await ws.close()
    return ws


async def health(_request: web.Request) -> web.Response:
    now = time.time()
    return web.json_response({
        "ok": True,
        "clients": [
            {"name": c.name, "role": c.role, "state": c.core.state.value,
             "audio_mb": round(c.audio_bytes / 1048576, 2),
             "uptime_s": round(now - c.started)}
            for c in CONNS.values()
        ],
        "online": bool(CONNS),
    })


async def interrupt(request: web.Request) -> web.Response:
    conn = _pick(request)
    if conn is None:
        return web.json_response({"ok": False, "err": "нет подключённых клиентов"})
    asyncio.create_task(conn.interrupt())
    return web.json_response({"ok": True, "name": conn.name})


async def cmd(request: web.Request) -> web.Response:
    conn = _pick(request)
    if conn is None:
        return web.json_response({"ok": False, "err": "нет подключённых клиентов"})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        res = await conn.command(body, timeout=30)
        return web.json_response({"ok": True, "result": res})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "err": str(e)})


async def state(request: web.Request) -> web.Response:
    """Состояние музыки на колонке по событиям mediad (не по ALSA)."""
    conn = _pick(request)
    if conn is None:
        return web.json_response({"ok": False, "err": "нет подключённых клиентов"})
    try:
        res = await conn.command({"cmd": "state"}, timeout=5)
        return web.json_response({"ok": True, "name": conn.name, "result": res})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "err": str(e)})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", health)
    app.router.add_get("/state", state)
    app.router.add_post("/interrupt", interrupt)
    app.router.add_post("/cmd", cmd)
    return app


async def main():
    global OUT_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8796)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--threshold", type=float, default=0.95)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    OUT_DIR = args.out
    os.makedirs(OUT_DIR, exist_ok=True)
    runner = web.AppRunner(make_app())
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", args.port)
    await site.start()
    log.info("голосовой хаб на :%d, порог по умолчанию %.2f", args.port, args.threshold)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
