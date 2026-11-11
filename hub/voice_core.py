#!/usr/bin/env python3
"""Единое речевое ядро: поток PCM → распознанная фраза.

Одна логика для колонки и панели. Эталон — колонка: фиксированное усиление
перед детектором, порог и усиление из `hub.json` (по станции), pre-roll 3 с,
900 мс тишины конца фразы, окно диалога 5 с (12 с после вопроса).

Ядро не знает ни про колонку, ни про браузер. Оно получает PCM и отдаёт:
  * переходы состояния (idle / collecting / dialog_wait / busy);
  * готовый текст фразы — дальше адаптер сам решает, звать агента и озвучивать.

Транспорт, индикация, пауза музыки и воспроизведение — в адаптерах.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional

import numpy as np

try:                      # запуск как модуль
    from . import wake_names
except ImportError:       # запуск как скрипт
    import wake_names

from voice_loop import PhraseCollector

log = logging.getLogger("voice-core")


class State(str, Enum):
    IDLE = "idle"                # ждём ключевое слово
    COLLECTING = "collecting"    # собираем фразу
    DIALOG_WAIT = "dialog_wait"  # ответили, ждём продолжение без слова
    BUSY = "busy"                # распознаём / агент думает / говорит


@dataclass
class CoreConfig:
    """Параметры тракта. Значения — из колонки, решение пользователя 31.10.2026."""

    wake_threshold: float = 0.95
    pre_roll_s: float = 3.0
    silence_ms: int = 500           # пауза «замолчал»; Smart Turn решает, конец ли
    min_voice_ms: int = 350
    max_phrase_s: float = 15.0
    dialog_wait_s: float = 5.0
    dialog_wait_question_s: float = 12.0
    followup_wait_voice_s: float = 6.0
    followup_max_s: float = 8.0
    dialog_max_s: float = 8.0
    hard_silence_ms: int = 2000
    smart_turn: bool = True
    # Перебивание голосом: ключевое слово во время хода агента обрывает ответ.
    barge_in: bool = True


def name_like(text: str) -> bool:
    """Похоже ли начало фразы на обращение к ассистенту.

    Критерий общий со `strip_wake`: иначе «Дермис» признаётся обращением,
    но не срезается, и фраза-триггер уходит в LLM с именем в начале.
    """
    return wake_names.name_like(text)


class WakeGate:
    """Интерфейс детектора слова. Реализации — локальная и Wyoming."""

    async def feed(self, pcm: bytes) -> list[tuple[float, float]]:
        raise NotImplementedError

    def reset(self) -> None:
        pass


class LocalWakeGate(WakeGate):
    """Наш штатный детектор (pymicro_wakeword) в процессе."""

    def __init__(self, threshold: float = 0.95):
        from voice_loop import WakeDetector

        self._det = WakeDetector(threshold=threshold)
        # Максимум балла за последний чанк: замер «почти срабатываний» читает его
        # из ядра, не залезая внутрь детектора.
        self.last_max = 0.0

    async def feed(self, pcm: bytes) -> list[tuple[float, float]]:
        hits = self._det.feed(pcm)
        self.last_max = self._det.take_max()
        return hits

    def reset(self) -> None:
        self._det.reset()


class DetectorLevel:
    """Фиксированное усиление до уровня, на котором обучалась модель.

    Замер на живой записи колонки (24 мин, 31.10.2026): сырой поток имеет
    rms 1.5–14 (речь) при тишине 1.4–1.7, и модель на ×10 давала максимум
    0.814 — ниже порога 0.92, то есть слово не ловилось вообще.
    При ×250 та же фраза даёт 0.96, а за 24 минуты — одно срабатывание
    (само обращение), ложных ноль. Клиппинг 0.003% сэмплов.
    Динамическое усиление (AGC/нормировка по блокам) здесь не годится:
    модуляция сигнала внутри фразы роняет вероятность до 0.64–0.69.
    """

    def __init__(self, gain: float = 250.0):
        self.gain = gain

    def process(self, pcm: bytes) -> bytes:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * self.gain
        return np.clip(x, -32768, 32767).astype(np.int16).tobytes()

    def reset(self) -> None:
        pass


StateCb = Callable[[State, Optional[str], Optional[float]], Awaitable[None]]
PhraseCb = Callable[[str, str, bool], Awaitable[None]]
AsrFn = Callable[[np.ndarray], Awaitable[str]]


class SpeechCore:
    """Речевой автомат. Один на соединение/устройство."""

    def __init__(
        self,
        *,
        wake: WakeGate,
        asr: AsrFn,
        on_state: StateCb,
        on_phrase: PhraseCb,
        on_barge: Callable[[], Awaitable[None]] | None = None,
        cfg: CoreConfig | None = None,
    ):
        self.cfg = cfg or CoreConfig()
        self.wake = wake
        self.level = DetectorLevel(gain=250.0)
        self.asr = asr
        self.on_state = on_state
        self.on_phrase = on_phrase
        self.on_barge = on_barge
        # Политика «эта фраза — команда из реестра» живёт в хабе. Ядру она
        # нужна в одном месте: проверка обращения по тексту не должна
        # отбрасывать фразу-триггер, если детектор уже подтвердил активацию,
        # а ASR не расслышал имя (окно начинается с первого кадра речи и
        # тихое «окей, Гермес» в него не попадает).
        self.intent_of: Callable[[str], str | None] = lambda _t: None
        # Перебивание в работе: отменённая задача распознавания не должна
        # вернуть IDLE и сбить только что начатый сбор новой фразы.
        self._barging = False

        # Семантический конец реплики: 8 МБ ONNX, ru в списке языков.
        self.smart_turn = None
        if self.cfg.smart_turn:
            try:
                from smart_turn import SmartTurn
                self.smart_turn = SmartTurn()
                log.info("Smart Turn подключён: порог %s", self.smart_turn.threshold)
            except Exception as e:  # noqa: BLE001
                log.warning("Smart Turn недоступен, работаем по тишине: %s", e)

        self.state: State = State.IDLE
        self.origin: str | None = None
        # Последний распознанный текст: нужен хабу, чтобы положить в метаданные
        # события то, что реально услышала колонка (иначе негатив обезличен).
        self.last_text = ""
        # Максимум балла детектора за последний чанк (для замера промахов).
        self.last_score = 0.0
        # Уровень сигнала перед детектором. Нужен арбитражу между колонками:
        # когда одна и та же фраза слышна двум станциям, отвечать должна та,
        # у которой сигнал громче (она ближе к говорящему).
        self.last_rms = 0.0
        self.wake_rms = 0.0
        self.follow_up = False
        self.dialog_active = False
        self.dialog_until = 0.0
        self.dialog_collect_done = False

        # Хаб выставляет сюда проверку «колонка ещё говорит ответ». Пока она
        # истинна, окно продолжения не закрывается: иначе таймер окна истекал
        # во время речи, состояние уходило в IDLE и музыка возвращалась поверх
        # недоговорённого ответа (проверено 02.11: окно закрыто в 18:42:08,
        # звук доигран в 18:42:15, музыка вернулась в 18:42:11).
        self.output_busy: Callable[[], bool] = lambda: False

        self.coll: PhraseCollector | None = None
        self.prefix = b""
        self.pre = bytearray()
        self.pre_bytes = int(self.cfg.pre_roll_s * 32000)
        self._task: asyncio.Task | None = None
        self._closed = False

    # ---------- вход ----------

    async def feed(self, raw: bytes, data: bytes) -> None:
        """Один кусок потока: raw — для детектора, data — после AGC."""
        if self._closed:
            return
        self.pre += data
        if len(self.pre) > self.pre_bytes:
            del self.pre[: len(self.pre) - self.pre_bytes]

        hits = await self.wake.feed(self.level.process(raw))
        # Балл последнего чанка: его читает замер промахов в хабе.
        self.last_score = getattr(self.wake, "last_max", 0.0)
        # Сглаженный уровень сигнала: арбитраж сравнивает станции по нему.
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        if x.size:
            self.last_rms = self.last_rms * 0.7 + float(np.sqrt(np.mean(x * x))) * 0.3

        # окно истекает независимо от того, что делает сборщик, — но не пока
        # колонка говорит ответ: иначе окно закрывается посреди речи
        if self.dialog_expired():
            await self._close_dialog("тишина")

        # Ключ во время хода агента — это перебивание, а не пропуск: человек
        # назвал имя, значит хочет остановить ответ и сказать своё. Детектор
        # работает всегда; до 06.11 результат в BUSY просто выбрасывался.
        if hits:
            if self.state is State.IDLE:
                await self._activate(hits[0])
            elif self.state is State.DIALOG_WAIT:
                # Ответ уже синтезирован и окно продолжения открыто, но
                # колонка ещё доигрывает звук — это то же перебивание.
                # Если звук кончился, ключ просто закрывает окно и начинает
                # новый ход обычной активацией.
                if self.cfg.barge_in and self.output_busy():
                    await self.barge_in(hits[0])
                else:
                    await self._activate(hits[0])
            elif self.state is State.BUSY and self.cfg.barge_in:
                await self.barge_in(hits[0])

        if self.state is State.IDLE and self.follow_up:
            self.follow_up = False
            await self._start_collector(
                "followup",
                wait_voice_s=self.cfg.followup_wait_voice_s,
                max_s=self.cfg.followup_max_s,
            )
        elif (self.state is State.DIALOG_WAIT and self.coll is None
              and not self.dialog_collect_done):
            await self._start_collector(
                "dialog",
                wait_voice_s=self.cfg.dialog_wait_s + 2,
                max_s=self.cfg.dialog_max_s,
            )

        if self.coll is not None:
            await self._feed_collector(data, raw)

    # ---------- переходы ----------

    def dialog_expired(self, now: float | None = None) -> bool:
        """Пора закрывать окно продолжения по тишине?

        Пока колонка говорит ответ (`output_busy`), окно не закрывается: иначе
        таймер истекает посреди речи, состояние уходит в IDLE и музыка
        возвращается поверх недоговорённой фразы.
        """
        if not self.dialog_active or self.state is State.BUSY:
            return False
        if self.output_busy():
            return False
        return (now if now is not None else time.time()) > self.dialog_until


    async def _set_state(self, state: State, origin: str | None = None,
                         score: float | None = None) -> None:
        if state is self.state and origin is None and score is None:
            return
        self.state = state
        try:
            await self.on_state(state, origin, score)
        except Exception as e:  # noqa: BLE001
            # str(e) у таймаута пустой: пишем тип, иначе в журнале
            # остаётся «on_state(idle):» без причины.
            log.warning("on_state(%s): %s: %s", state.value, type(e).__name__, e)

    async def _activate(self, hit: tuple[float, float]) -> None:
        self.dialog_until = 0.0
        # Ключ при открытом окне — новый ход: окно закрываем, иначе
        # dialog_expired() закроет уже начатый сбор (dialog_until=0).
        self.dialog_active = False
        self.dialog_collect_done = False
        self.follow_up = False
        self.prefix = bytes(self.pre)
        self.origin = "wake"
        self.wake_rms = self.last_rms
        self.coll = PhraseCollector(
            skip_ms=0,
            silence_ms=self.cfg.silence_ms,
            min_voice_ms=self.cfg.min_voice_ms,
            max_s=self.cfg.max_phrase_s,
            end_detector=self.smart_turn,
            hard_silence_ms=self.cfg.hard_silence_ms,
        )
        log.info("активация %.2f с peak=%.3f", hit[0], hit[1])
        # Окна проверки нет: имя проверяется по тексту готовой фразы (name_like),
        # поэтому распознавание одно, а не два. Всё, что уже сказано до
        # срабатывания детектора, сразу отдаём сборщику — иначе хвост теряется.
        await self._set_state(State.COLLECTING, origin="wake", score=hit[1])
        prefix, self.prefix = self.prefix, b""
        await self._seed_collector(prefix)

    async def barge_in(self, hit: tuple[float, float]) -> None:
        """Перебивание голосом: ключ прозвучал, пока идёт ход агента.

        Порядок важен: состояние и сброс задачи делаются ДО первого await,
        иначе отменённая задача распознавания успевает выполнить свой
        `finally` и вернуть IDLE поверх только что начатого сбора.
        """
        log.info("перебивание %.2f с peak=%.3f", hit[0], hit[1])
        self._barging = True
        self.state = State.COLLECTING
        self.dialog_active = False
        self.dialog_until = 0.0
        self.dialog_collect_done = False
        self.follow_up = False
        self.coll = None
        self.prefix = b""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        # Адаптер глушит уже отправленный звук и отменяет ход агента.
        if self.on_barge is not None:
            try:
                await self.on_barge()
            except Exception as e:  # noqa: BLE001
                log.warning("on_barge: %s: %s", type(e).__name__, e)
        # Детектор после срабатывания держит refractory и вернул бы тот же
        # пик ещё раз; для нового сбора он должен начать с чистого листа.
        self.wake.reset()
        self.prefix = bytes(self.pre)
        self.origin = "barge"
        self.wake_rms = self.last_rms
        self.coll = PhraseCollector(
            skip_ms=0,
            silence_ms=self.cfg.silence_ms,
            min_voice_ms=self.cfg.min_voice_ms,
            max_s=self.cfg.max_phrase_s,
            end_detector=self.smart_turn,
            hard_silence_ms=self.cfg.hard_silence_ms,
        )
        await self._set_state(State.COLLECTING, origin="barge", score=hit[1])
        prefix, self.prefix = self.prefix, b""
        self._barging = False
        await self._seed_collector(prefix)

    async def _seed_collector(self, prefix: bytes) -> None:
        """Отдать сборщику уже сказанное (окно проверки ключа).

        Кормим блоками по 32 мс: признак речи считается по окну 0.2 с, и один
        кусок на 2.5 с даёт его только по последним кадрам — если там тишина,
        сборщик не увидит речь, вернёт 'cancel', и сказанная фраза потеряется.

        seed=True: активацию подтвердил детектор, поэтому префикс попадает во
        фразу целиком, независимо от VAD. Без этого тихое имя отбрасывалось,
        и ASR получал обрезанное начало реплики.
        """
        if self.coll is None or not prefix:
            return
        for i in range(0, len(prefix), 1024):
            if self.coll is None:
                return
            piece = prefix[i:i + 1024]
            await self._feed_collector(piece, piece, seed=True)

    async def begin(self, origin: str = "button") -> None:
        """Явный старт хода (кнопка/орб): ключевое слово не требуется.

        Origin «button» важен: проверка обращения по тексту (`name_like`) делается
        только для origin «wake». Иначе нажатие кнопки в панели и следующий за ним
        вопрос без «окей, Гермес» отбрасывались бы как «обращения нет».
        """
        if self.state is not State.IDLE:
            return
        self.dialog_until = 0.0
        await self._start_collector(
            origin,
            wait_voice_s=self.cfg.followup_wait_voice_s,
            max_s=self.cfg.max_phrase_s,
        )

    async def _start_collector(self, origin: str, wait_voice_s: float,
                               max_s: float) -> None:
        self.origin = origin
        self.prefix = b""
        self.coll = PhraseCollector(
            skip_ms=0,
            silence_ms=self.cfg.silence_ms,
            min_voice_ms=self.cfg.min_voice_ms,
            wait_voice_s=wait_voice_s,
            max_s=max_s,
            end_detector=self.smart_turn,
            hard_silence_ms=self.cfg.hard_silence_ms,
        )
        await self._set_state(State.COLLECTING, origin=origin)

    async def _close_dialog(self, reason: str) -> None:
        self.coll = None
        self.prefix = b""
        self.origin = None
        self.dialog_until = 0.0
        self.dialog_active = False
        self.dialog_collect_done = False
        self.follow_up = False
        await self._set_state(State.IDLE, origin=None)
        log.info("окно диалога закрыто (%s)", reason)

    async def drop_dialog(self) -> None:
        """«Стоп»: окно продолжения больше не нужно, ждём только ключ."""
        if self.state in (State.DIALOG_WAIT, State.COLLECTING):
            await self._close_dialog("стоп")

    def open_dialog(self, was_question: bool) -> None:
        """Открыть окно продолжения без ключевого слова (после ответа)."""
        self.dialog_collect_done = False
        self.dialog_active = True
        wait = (self.cfg.dialog_wait_question_s if was_question
                else self.cfg.dialog_wait_s)
        self.dialog_until = time.time() + wait
        self.state = State.DIALOG_WAIT
        log.info("окно диалога открыто на %.0f с (%s)", wait,
                 "был вопрос" if was_question else "реплика")

    def expect_followup(self) -> None:
        """Прозвучало только обращение — ждём фразу без нового слова."""
        self.follow_up = True

    # ---------- сбор фразы ----------

    async def _feed_collector(self, data: bytes, raw: bytes = b"",
                              seed: bool = False) -> None:
        phrase = self.coll.feed(data, analysis=raw or None, seed=seed)
        if phrase is None:
            return
        origin, prefix = self.origin or "wake", self.prefix
        self.coll = None
        self.prefix = b""
        self.origin = None

        if isinstance(phrase, str):        # 'cancel' — никто не заговорил
            if origin == "dialog":
                self.state = State.DIALOG_WAIT
                self.dialog_collect_done = True
                await self._set_state(State.DIALOG_WAIT, origin="dialog")
                return
            self.dialog_until = 0.0
            await self._set_state(State.IDLE, origin=None)
            return

        # Фраза собрана — микрофон для этой реплики закрыт. Индикатор «слушаю»
        # гасим сразу, не дожидаясь распознавания.
        await self._set_state(State.BUSY, origin=origin)
        self._task = asyncio.create_task(
            self._recognize(phrase, prefix, origin))

    async def _recognize(self, pcm: np.ndarray, prefix: bytes,
                         origin: str) -> None:
        t0 = time.time()
        # Своя задача: после перебивания в `_task` уже может стоять задача
        # нового сбора, и её нельзя гасить чужим `finally`.
        me = asyncio.current_task()
        try:
            full = pcm if not prefix else np.concatenate(
                [np.frombuffer(prefix, dtype=np.int16), pcm])
            text = await self.asr(full)
            self.last_text = text
            log.info("ASR %.2f с (%.1f с аудио) → %r",
                     time.time() - t0, len(pcm) / 16000, text)
            if not text:
                if origin in ("wake", "barge"):
                    await self.on_state(State.IDLE, origin, None)
                elif origin == "dialog":
                    # Smart Turn закрыл фразу, а распознавать нечего: продолжения
                    # нет. Раньше окно оставалось висеть до таймаута и заново
                    # открывало сбор — со стороны это выглядело как «агент ждёт».
                    await self._close_dialog("тишина")
                else:
                    self.state = State.IDLE
                return
            # Обращение проверяем по готовому тексту всей фразы, а не по
            # вырезанному окну: контекст шире, искажения ASR видны целиком.
            # Фразы-триггеры из реестра — исключение: детектор уже подтвердил
            # активацию, а имя могло остаться вне окна ASR (см. intent_of).
            named = name_like(text)
            trigger = self.intent_of(text) if origin == "wake" else None
            if origin == "wake" and not named and not trigger:
                log.info("обращения в тексте нет (%r) — не отвечаю", text[:70])
                self.state = State.IDLE
                await self.on_state(State.IDLE, "no_wake_name", None)
                return
            if trigger and not named:
                log.info("фраза-триггер %r без обращения в тексте — команда из реестра",
                         trigger)
            await self.on_phrase(text, origin, origin == "wake")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("фраза не обработана: %s", e)
            self.state = State.IDLE
            try:
                await self.on_state(State.IDLE, origin, None)
            except Exception:  # noqa: BLE001
                pass
        finally:
            if (self.state is State.BUSY and not self._barging
                    and self._task in (None, me)):
                self.state = State.IDLE
                try:
                    await self.on_state(State.IDLE, origin, None)
                except Exception:  # noqa: BLE001
                    pass

    # ---------- прерывание/закрытие ----------

    async def process_miss(self, window: bytes, score: float) -> bool:
        """Промах с подтверждённым обращением — это команда, а не пропуск.

        Правило контура: перед активацией проверяем предшествующие записи.
        Если детектор не дотянул до порога, но в окне распознано обращение,
        фраза обрабатывается как обычная. Без этого пользователь повторяет
        её второй и третий раз, а колонка молчит.

        Возвращает True, если окно взято в работу.
        """
        if self.state not in (State.IDLE, State.DIALOG_WAIT):
            return False
        if self.dialog_active or self.output_busy():
            return False
        if window is None or len(window) == 0:
            return False
        if isinstance(window, (bytes, bytearray, memoryview)):
            pcm = np.frombuffer(window, dtype=np.int16)
        else:
            pcm = np.asarray(window, dtype=np.int16)
        if pcm.size < int(0.5 * 16000):
            return False
        self.dialog_until = 0.0
        self.origin = "wake"
        self.wake_rms = self.last_rms
        log.info("промах %.3f → активация по предыдущей записи (%.1f с)",
                 score, pcm.size / 16000)
        await self._set_state(State.BUSY, origin="wake", score=score)
        self._task = asyncio.create_task(
            self._recognize(pcm.copy(), b"", "wake"))
        return True

    async def cancel_wake(self, origin: str = "arbitration") -> None:
        """Снять активацию, не тронув ответ и музыку.

        Вызывается арбитражем, когда ту же фразу лучше слышит другая станция:
        эта колонка не должна ни зажигать кольцо, ни собирать фразу, ни
        выполнять команду. Прерывание ответа (`interrupt`) здесь не годится:
        оно сбрасывает и уже идущий ход.
        """
        if self.state is not State.COLLECTING or self.origin != "wake":
            return
        self.coll = None
        self.prefix = b""
        self.origin = None
        await self._set_state(State.IDLE, origin=origin)

    async def interrupt(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self.coll = None
        self.prefix = b""
        self.origin = None
        self.follow_up = False
        self.dialog_until = 0.0
        self.dialog_collect_done = False
        self.dialog_active = False
        await self._set_state(State.IDLE, origin=None)

    async def close(self) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
