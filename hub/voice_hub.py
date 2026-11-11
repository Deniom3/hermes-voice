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

HTTP рядом: GET /health, POST /route (диагностика быстрого маршрута).
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import random
import socket
import sys
import time
import wave

from aiohttp import WSMsgType, web

try:                      # запуск как модуль
    from . import config
    from . import intents
    from . import voice_capture
    from . import voice_dialog
    from . import replies
    from . import mqtt_bridge
    from . import wake_probe
    from . import metrics
    from . import ma_control
    from . import sources
    from .metrics import METRICS
    from .voice_core import CoreConfig, DetectorLevel, LocalWakeGate, SpeechCore, State
    from .voice_loop import AGC, transcribe
except ImportError:       # запуск как скрипт
    import config
    import intents
    import voice_capture
    import voice_dialog
    import replies
    import mqtt_bridge
    import wake_probe
    import metrics
    import ma_control
    import sources
    from metrics import METRICS
    from voice_core import CoreConfig, DetectorLevel, LocalWakeGate, SpeechCore, State
    from voice_loop import AGC, transcribe

log = logging.getLogger("voice-hub")

CONNS: dict[str, "Conn"] = {}

# MQTT-мост поднимается в startup приложения, а нужен и в обработчике фразы:
# распознанный текст уходит в HA отдельным топиком. Держим ссылку здесь,
# чтобы `_on_phrase` не искал приложение по цепочке объектов.
MQTT_BRIDGE = None


def match_intent(text: str, clean: str | None = None) -> str | None:
    """Каноническая фраза реестра, если распознанное совпало с интентом.

    Один матчер на два потребителя: ядро (`SpeechCore.is_intent`) спрашивает
    его, чтобы не отбрасывать фразу без имени, и `_on_phrase` — чтобы уйти в
    Home Assistant вместо LLM. Две копии сравнения разошлись бы: ядро
    пропустило бы фразу, а хаб её не узнал.
    """
    clean = voice_dialog.strip_wake(text) if clean is None else clean
    if not clean:
        return None
    rules = config.intents()
    matched = intents.match(
        clean, rules,
        max_distance=config.as_int("match", "max_distance", default=2),
        use_anchors=config.as_bool("match", "anchors", default=True),
        anchor_max_words=config.as_int("match", "anchor_max_words", default=4))
    if matched and not intents.is_excluded(matched, rules):
        return matched
    return None


class WakeArbitration:
    """Одна фраза — одна колонка.

    Колонки могут стоять рядом и слышать одно и то же обращение. Без выбора
    обе запускают ход: два кольца, две паузы музыки и двойное выполнение
    команды («прибавь громкость» прибавит дважды). Здесь заявки собираются
    в короткий раунд (settle), победитель определяется по уровню сигнала:
    громче — значит ближе к говорящему. Проигравший снимает активацию, не
    трогая ни ответ, ни музыку.
    """

    def __init__(self, settle: float = 0.30):
        self.settle = settle
        self._claims: dict[str, float] = {}
        self._winners: dict[int, str] = {}
        self._round = 0
        self._round_until = 0.0
        self._last_winner = ""
        self._last_win_at = 0.0
        self._lock = asyncio.Lock()

    async def claim(self, name: str, power: float) -> bool:
        stations = _station_conns()
        if len(stations) <= 1:
            return True
        # Тот же ход уже отдан другой станции несколько мгновений назад:
        # это эхо той же фразы, перебивать нельзя. Окно короткое, чтобы
        # вторая колонка в другой комнате могла начать свой ход.
        now = asyncio.get_running_loop().time()
        if (self._last_winner and self._last_winner != name
                and now - self._last_win_at <= self.settle + 0.7):
            return False
        async with self._lock:
            now = asyncio.get_running_loop().time()
            if self._round and now < self._round_until:
                self._claims[name] = float(power)
                my_round = self._round
            else:
                self._round += 1
                self._round_until = now + self.settle
                self._claims = {name: float(power)}
                my_round = self._round
        await asyncio.sleep(self.settle)
        async with self._lock:
            best = self._winners.get(my_round)
            if best is None:
                best = max(self._claims.items(), key=lambda kv: kv[1])[0]
                self._winners[my_round] = best
                self._claims = {}
                # Держим только свежие раунды: старые заявки не должны
                # влиять на следующий ход.
                for r in [r for r in self._winners if r < my_round - 8]:
                    self._winners.pop(r, None)
            if best != name:
                log.info("%s: активацию уступаю станции %s (сигнал громче)", name, best)
            else:
                self._last_winner = name
                self._last_win_at = asyncio.get_running_loop().time()
            return best == name


WAKE_ARB = WakeArbitration()
OUT_DIR = "/tmp/station-raw"
# Сколько ждать после IDLE, прежде чем вернуть музыку на колонку. Пауза нужна,
# чтобы «окей, Гермес» и следующая за ним фраза не накладывались на музыку.
RESUME_DELAY = config.as_float("dialog", "resume_delay", default=3.0)
# Сколько секунд после кнопки глушить натив, который поднимает maind.
BUTTON_GUARD = config.as_float("dialog", "button_guard", default=2.5)
# Сколько держать намерение, пока MA подтверждает pause/play (по логу 2-3 с).
MA_CONFIRM = config.as_float("dialog", "ma_confirm", default=3.0)
# Запас на хвост воспроизведения: данные чанка уходят в сокет раньше, чем
# колонка доигрывает. Без этого запаса кольцо гасло и музыка возвращалась
# на последних секундах длинной фразы.
PLAY_TAIL_S = config.as_float("dialog", "play_tail_s", default=0.6)

# Сколько игнорировать `play_done` после перебивания: хвост уже отправленного
# звука доигрывает на колонке и не должен гасить кольцо нового хода.
BARGE_GUARD_S = config.as_float("dialog", "barge_guard_s", default=3.0)

# Сколько максимум ждать фактического конца озвучки перед возвратом музыки.
# Фраза на 2000 символов — это около двух минут речи, поэтому предел с запасом;
# нужен только как страховка от залипшего флага «ещё звучит».
MAX_SPEECH_WAIT = config.as_float("dialog", "max_speech_wait", default=180.0)


# Порт, на который агент шлёт broadcast-поиск хаба. Фиксирован: это адрес
# обнаружения, а не настройка. mDNS не годится — на колонке нет avahi и
# nss-mdns, а агент собран с CGO_ENABLED=0 (свой резолвер, только обычный DNS).
DISCOVERY_PORT = 8797
DISCOVERY_QUERY = b"HERMES-HUB?"


def normalize_mac(value) -> str:
    """MAC в виде `aa:bb:cc:dd:ee:ff`; пустая строка, если это не MAC."""
    if not value:
        return ""
    text = str(value).strip().lower().replace("-", ":").replace(".", "").replace(":", "")
    if len(text) != 12 or any(c not in "0123456789abcdef" for c in text):
        return ""
    return ":".join(text[i:i + 2] for i in range(0, 12, 2))


def station_by_mac(mac: str) -> str | None:
    """Имя станции из `wake.stations` по MAC.

    MAC — физический признак: он не зависит от того, что написано в
    `agent.env`, и переживает переустановку агента. Имя нужно как ключ для
    порогов и промахов, поэтому ищем его здесь, а не полагаемся на устройство.
    """
    mac = normalize_mac(mac)
    if not mac:
        return None
    stations = config.get("wake", "stations", default={})
    if not isinstance(stations, dict):
        return None
    for name, opts in stations.items():
        if isinstance(opts, dict) and normalize_mac(opts.get("mac")) == mac:
            return str(name)
    return None


def station_sendspin(name: str) -> dict | None:
    """Конфиг Sendspin для станции из hub.json.

    Глобальные значения — секция `sendspin`, станционные —
    `wake.stations.<name>.sendspin`. Станционные главнее. Если `enabled`
    выключен на любом уровне или нет URL, клиент на колонке не запускается.
    """
    common = config.get("sendspin", default={})
    if not isinstance(common, dict):
        common = {}
    own = config.get("wake", "stations", name, "sendspin", default={})
    if not isinstance(own, dict):
        own = {}
    if own.get("enabled") is False or common.get("enabled") is False:
        return None
    url = own.get("url") or common.get("url")
    if not url:
        return None
    return {
        "url": str(url),
        "name": str(own.get("name") or f"{name} (Hermes)"),
        "state_dir": str(own.get("state_dir") or
                        common.get("state_dir") or
                        "/data/opt/hermes-station/sendspin"),
        "output": str(own.get("output") or common.get("output") or "default"),
    }


def local_ip_for(peer: str) -> str:
    """Адрес, с которого хаб виден этому пиру (а не 0.0.0.0 из sockname)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((peer, 9))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class HubDiscovery(asyncio.DatagramProtocol):
    """Отвечает на UDP-поиск, чтобы адрес хаба не хранился на колонке.

    DHCP может выдать хабу другой адрес; прописанный вручную адрес после
    этого молча ломает связь со всеми станциями.
    """

    def __init__(self, port: int):
        self.port = port
        self.transport = None

    def connection_made(self, transport):  # noqa: D102 - протокол asyncio
        self.transport = transport
        log.info("поиск хаба: UDP :%d", DISCOVERY_PORT)

    def datagram_received(self, data: bytes, addr):
        if data.strip() != DISCOVERY_QUERY:
            return
        reply = f"HERMES-HUB {local_ip_for(addr[0])} {self.port}".encode()
        self.transport.sendto(reply, addr)


def station_wake(name: str, key: str, default: float) -> float:
    """Параметр детектора для станции: `wake.stations.<name>.<key>` → `wake.<key>`.

    Детектор крутится в хабе (`LocalWakeGate` получает PCM с колонки), поэтому
    порог и усиление — параметры хаба, а не агента. Значение агента в `hello`
    остаётся только для сверки: если оно расходится с файлом, пишем
    предупреждение, но применяем значение файла. Иначе настройка в конфиге
    ничего не решает, а поведение задаёт бинарник на колонке.
    """
    raw = config.get("wake", "stations", name, key, default=None)
    source = f"wake.stations.{name}.{key}"
    if raw is None:
        raw = config.get("wake", key, default=default)
        source = f"wake.{key}"
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("%s: %s=%r — не число, беру %.2f", name, source, raw, default)
        return default


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


def speech_end_at(sent_at: float, chunk_seconds: float, prev_end: float = 0.0) -> float:
    """Момент, когда доиграет этот чанк.

    Куски играют по очереди: пока звучит предыдущий, следующий ждёт в очереди
    агента. Поэтому считать от «сейчас» нельзя — иначе на четырёх кусках
    оценка занижается вдвое и музыка возвращается посреди ответа. Берём
    максимум из «сейчас» и конца предыдущего, прибавляем длительность.

    Точный конец отдельно сообщает ``play_done`` от агента.
    """
    if chunk_seconds <= 0:
        return prev_end
    return max(sent_at, prev_end) + chunk_seconds + PLAY_TAIL_S



def should_pause_overlap(native: bool, sendspin: bool, engaged: bool,
                         already: bool = False) -> bool:
    """Пора ли гасить оба источника музыки.

    Нативный mediad может заиграть поверх Sendspin (Алиса запустила радио).
    Срабатываем один раз на эпизод; пока нативный играет, повторно не дёргаем.
    """
    return bool(native and (sendspin or engaged) and not already)


class Conn:
    """Одно подключение клиента: транспорт + речевой автомат."""

    # Тесты собирают Conn через __new__ и не зовут __init__; безопасный
    # дефолт нужен, чтобы проверки не падали на новом поле.
    _ma_paused = False
    _agent_sendspin = False
    _native_overlap = False
    _vol_sync_task = None
    _vol_sync_pending = None
    _suppress_resume = False
    _button_guard_until = 0.0
    # Ожидание подтверждения от MA: (состояние, дедлайн). Пока не истёк,
    # снимок источников держит намерение, а не старое состояние MA.
    _ma_pending: tuple[str, float] | None = None

    def __init__(self, ws, name: str, role: str, cfg: CoreConfig,
                 gain: float, agc: bool, write_audio: bool):
        self.ws = ws
        self.name = name
        self.role = role
        self.cfg = cfg
        self.agc = AGC() if agc else None
        self.write_audio = write_audio
        self.audio_bytes = 0
        self.last_audio_at = 0.0
        self._frames = 0
        self.turn = None
        self.started = time.monotonic()
        self._seq = 0
        self._waiters: dict[str, asyncio.Future] = {}
        self._f = None
        self._rawf = None

        # Сбор событий для дообучения: в памяти кольцо 8 с, на диск — только
        # окна вокруг событий. Непрерывный поток пишется лишь при отладке.
        self.ring = voice_capture.Ring(seconds=8.0)
        self.collector = voice_capture.Collector(
            root=config.get("wake", "train", "dir", default="/srv/kws/train"),
            enabled=config.as_bool("wake", "train", "enabled", default=True),
        )
        self.retro = voice_capture.RetroScanner()
        # Последний час сырого потока на диске. Нужен для разбора жалобы
        # «сказал, а колонка промолчала»: окно события в памяти живёт 8 с,
        # по времени его уже не достать. Промахи 0.80–0.95 пишутся отдельно,
        # но сюда попадает и то, что ниже порога сбора.
        self.rec = voice_capture.StreamRing(
            root=config.get("wake", "ring", "dir", default="/srv/kws/ring"),
            name=name,
            keep_s=config.as_float("wake", "ring", "keep_s", default=3600.0),
            chunk_s=config.as_float("wake", "ring", "chunk_s", default=300.0),
            enabled=(role == "station"
                     and config.as_bool("wake", "ring", "enabled", default=True)),
        )
        # Замер «почти срабатываний»: только счётчики в памяти, аудио не пишем.
        # Интервал отчёта в журнал — 10 минут; короче только для разовой проверки.
        self.probe = wake_probe.ScoreProbe(
            report_s=config.as_float("wake", "probe_report_s", default=600.0))
        # Промахи: пик был, порог не превышен — это и есть данные, которых
        # не хватало для дообучения. Пишем в misses, поведение не меняется.
        self.miss = voice_capture.MissWatcher(
            min_score=station_wake(name, "miss_min", 0.65),
            high=cfg.wake_threshold,
            per_hour=config.as_int("wake", "miss_per_hour", default=120),
        )
        self._snap = b""
        self._snap_peaks: list[tuple[float, float]] = []
        self._snap_score = 0.0
        self._resume_task: asyncio.Task | None = None
        # Оценка «конец последнего чанка» и точный факт от агента (`play_done`).
        # Первая ставится при отправке, второй приходит, когда gst доиграл.
        # Пока gst не подтвердил, держим оценку: сокет уходит вперёд звука.
        self._audio_deadline = 0.0
        self._play_done = False
        self._pending_say = False
        # Идёт отправка кусков ответа: play_done от агента в это время значит
        # лишь «очередь опустела», а не «речь кончилась».
        self._sending_audio = False
        # Окно диалога открыто, но микрофон ещё не слушаем: колонка говорит.
        # Иначе микрофон ловит собственный ответ, сборщик собирает «фразу»,
        # автомат уходит в BUSY и перебивает кольцо посреди речи.
        self._dialog_hold = False
        self._led_task: asyncio.Task | None = None
        # Играла ли музыка до активации: play шлём только если было что вернуть.
        self._was_playing = False
        # Очередь MA стоит на паузе именно нашим ходом (а не нативный mediad).
        self._ma_paused = False
        # Агент подтвердил, что пауза была именно Sendspin, а не нативным
        # mediad. Нужно, чтобы после ответа не поднять радио Алисы.
        self._agent_sendspin = False
        # Снимок трёх источников (Алиса, MA, наш Sendspin) и его автомат:
        # единственное место, где решается, кто играет и кого гасить.
        self._sources = sources.Sources()
        # До какого момента `play_done` от оборванного ответа игнорируется:
        # агент может успеть доиграть хвост и сообщить «конец речи» уже во
        # время нового хода, а это вернуло бы музыку поверх вопроса.
        self._barge_guard_until = 0.0
        # После перебивания весь остаток диалога идёт без быстрого дома:
        # человек уже разговаривает, и «а если включить свет?» — это вопрос,
        # а не команда. Сбрасывается при закрытии окна (IDLE без origin).
        self._barge_dialog = False
        # Громкость станции по ALSA (агент сообщает в hello и при изменении).
        self.volume: int | None = None      # уровень 0..10, единый контракт
        self.volume_percent: int | None = None
        self.volume_db: float | None = None

        self.core = SpeechCore(
            wake=LocalWakeGate(threshold=cfg.wake_threshold),
            asr=self._asr,
            on_state=self._on_state,
            on_phrase=self._on_phrase,
            on_barge=self._on_barge,
            cfg=cfg,
        )
        self.core.level = DetectorLevel(gain=gain)
        # Пока колонка говорит ответ, окно продолжения не закрывается по
        # таймеру: см. комментарий у SpeechCore.output_busy.
        self.core.output_busy = self._output_busy
        # Проверка «фраза — команда из реестра»: ядро не должно отбрасывать её
        # как «обращения нет», если имя не попало в окно распознавания.
        self.core.intent_of = match_intent

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

    async def on_play_done(self) -> None:
        """Агент доиграл очередь — но это может быть пауза между кусками.

        Пока хаб ещё отправляет куски, очередь агента пустеет в промежутках
        между синтезом, и `play_done` приходит посреди ответа. Такой сигнал
        игнорируем: настоящий конец — это `play_done`, пришедший после того,
        как мы закончили отправку. Иначе кольцо гасло и музыка возвращалась
        ровно на том же длинном ответе, ради которого всё делалось.
        """
        if self._sending_audio:
            log.debug("%s: play_done в промежутке между кусками — игнор", self.name)
            return
        if asyncio.get_running_loop().time() < getattr(self, "_barge_guard_until", 0.0):
            # Хвост оборванного ответа доиграл уже во время нового хода:
            # гасить кольцо и возвращать музыку по нему нельзя.
            log.info("%s: play_done после перебивания — игнор", self.name)
            return
        self._play_done = True
        self._audio_deadline = 0.0
        log.info("%s: звук доигран (подтверждение колонки)", self.name)
        await self._finish_speech()

    async def command(self, payload: dict, timeout: float = 120.0) -> dict:
        """Команда устройству с подтверждением (id ↔ cmd_result)."""
        if payload.get("cmd") == "led":
            # Каждое переключение кольца — в журнал: без этого спор
            # «кто погасил» решается только догадками.
            log.info("%s: led → %r", self.name, payload.get("name") or "выкл")
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
        try:
            text = await asyncio.to_thread(transcribe, pcm)
        except Exception:
            METRICS.inc("asr_errors_total", {"station": self.name})
            raise
        elapsed = time.time() - t0
        METRICS.observe("asr_seconds", elapsed, {"station": self.name})
        METRICS.observe("asr_audio_seconds", len(pcm) / 16000, {"station": self.name})
        if not text:
            METRICS.inc("asr_empty_total", {"station": self.name})
        # Два разных числа: время работы и длина аудио. Раньше в лог шла только
        # длина, и строка «ASR 2.5 с» читалась как «распознавание заняло 2.5 с».
        log.info("%s: ASR %.2f с (аудио %.1f с) → %r",
                 self.name, elapsed, len(pcm) / 16000, text)
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
        if state is State.COLLECTING and origin == "wake":
            # Новый ход по ключевому слову — новый диалог: запрет быстрого
            # дома после прошлого перебивания здесь уже не действует.
            self._barge_dialog = False
            METRICS.inc("wake_activations_total", {"station": self.name})
            METRICS.observe("wake_score", float(score or 0.0), {"station": self.name})
            # Ту же фразу могут слышать обе колонки. Отвечает одна: та, у
            # которой сигнал громче. Проигравшая снимает активацию до того,
            # как зажечь кольцо и поставить музыку на паузу.
            if not await WAKE_ARB.claim(self.name, getattr(self.core, "wake_rms", 0.0)):
                METRICS.inc("wake_arbitration_lost_total", {"station": self.name})
                await self.core.cancel_wake()
                await self.command({"cmd": "led", "name": ""}, timeout=10)
                return
            if not self._snap:
                self._snap = self.ring.last(6.0)
                self._snap_score = float(score or 0.0)
                self._snap_peaks = []
                self._wake_at = asyncio.get_running_loop().time()
                self.miss.reset()
                asyncio.create_task(self._retro_scan(self._snap))
        if state is State.COLLECTING:
            if origin == "wake":
                # Новый ход по ключевому слову: прошлый ответ обрываем,
                # поэтому сбрасываем и учёт звука.
                self._cancel_led_task()
                self._audio_deadline = 0.0
                self._play_done = False
                self._pending_say = False
                # Музыку НЕ ставим на паузу здесь: детектор может ложно
                # сработать на чужое имя («Алиса, продолжить»), и тогда
                # колонка зря замолкает. Пауза — только после того, как
                # ASR подтвердил обращение к Гермесу (см. _on_phrase).
                self._was_playing = False
                self._music_paused_for_turn = False
                await self.command({"cmd": "sound", "name": "spotter_start"}, timeout=15)
                # Гасим музыку ДО сигнала «слушаю»: pause и запрос к MA
                # занимают ~350 мс, и при обратном порядке музыка звучала
                # уже после загоревшегося кольца.
                await self._pause_music_for_turn()
                await self.command({"cmd": "led", "name": "hermes_listening"}, timeout=15)
            elif origin == "barge":
                # Перебивание: музыка уже на паузе с прошлой активации, и
                # повторный `pause` вернул бы `was_playing=false`, потеряв
                # факт «играла до разговора». Поэтому только индикация.
                METRICS.inc("barge_in_total", {"station": self.name})
                self._cancel_led_task()
                self._audio_deadline = 0.0
                self._play_done = False
                self._pending_say = False
                await self.command({"cmd": "led", "name": "hermes_listening"}, timeout=15)
            elif not self._audio_playing():
                # Продолжение диалога: окно открывается сразу после ответа,
                # но колонка ещё доигрывает буфер. Кольцо «говорит» не трогаем,
                # иначе лампа гаснет/переключается посреди речи — на длинных
                # фразах это выглядит как «выключилось раньше времени».
                await self.command({"cmd": "led", "name": "hermes_listening"}, timeout=15)
        elif state is State.BUSY:
            self._cancel_led_task()
            await self.command({"cmd": "led", "name": "hermes_thinking"}, timeout=15)
        elif state is State.DIALOG_WAIT:
            self._clear_led_after_audio()
        elif state is State.IDLE:
            if origin is None:
                # Окно диалога закрылось — следующий ход снова обычный.
                self._barge_dialog = False
            self._clear_led_after_audio()
            if origin == "no_wake_name":
                METRICS.inc("wake_no_name_total", {"station": self.name})
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
        gst играет из буфера, поэтому гасить по отправке нельзя. Ждём факт
        `play_done` от агента; оценка `_audio_deadline` — страховка, если
        подтверждение не пришло (обрыв, старый агент).
        """
        self._cancel_led_task()
        if (self._play_done
                or getattr(self, "_audio_deadline", 0.0) <= asyncio.get_running_loop().time()):
            # Звука в буфере нет: гасим сразу, без задачи-ожидателя.
            self._led_task = asyncio.create_task(self._finish_speech())
            return
        self._led_task = asyncio.create_task(self._release_after_speech())

    async def _release_after_speech(self) -> None:
        """Незавершённая задача при _on_state: только дождаться звука.

        Гасит кольцо и возвращает музыку сам конец речи (`_finish_speech`) —
        единая точка выхода, иначе кольцо гасло в двух местах и гонялось
        с возвратом музыки.
        """
        left = getattr(self, "_audio_deadline", 0.0) - asyncio.get_running_loop().time()
        if left > 0:
            await asyncio.sleep(left)
        await self._finish_speech()

    def _audio_playing(self) -> bool:
        """Колонка ещё проигрывает ответ: кольцо трогать нельзя."""
        if self._play_done:
            return False
        return asyncio.get_running_loop().time() < getattr(self, "_audio_deadline", 0.0)

    def _output_busy(self) -> bool:
        """Ответ ещё звучит (или его начало ждёт своей очереди)."""
        return self._dialog_hold or self._audio_playing()

    async def _finish_speech(self) -> None:
        try:
            await self.command({"cmd": "led", "name": ""}, timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: led не погашен: %s", self.name, e)
        self._play_done = False
        self._pending_say = False
        # Оценка больше не нужна: речь кончилась по факту, иначе она держала бы
        # флаг «ещё звучит» и откладывала возврат музыки.
        self._audio_deadline = 0.0
        # Звук кончился — можно снова слушать продолжение без ключевого слова.
        if self._dialog_hold:
            self._dialog_hold = False
            # Окно отсчитывалось от конца синтеза и успевало истечь, пока
            # колонка доигрывала ответ. Открываем заново — от реального конца.
            self.core.open_dialog(getattr(self, "_dialog_question", True))
            log.info("%s: окно продолжения переоткрыто после озвучки", self.name)
            try:
                await self.command({"cmd": "led", "name": "hermes_listening"}, timeout=10)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: led listening не показан: %s", self.name, e)
        # Пока звучал ответ, автомат мог уйти в сбор продолжения — кольцо
        # должно вернуться к тому, что происходит на самом деле.
        if self.core.state is State.COLLECTING:
            try:
                await self.command({"cmd": "led", "name": "hermes_listening"}, timeout=10)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: led listening не показан: %s", self.name, e)
        if not self.core.follow_up:
            self._plan_resume_music()

    def _plan_resume_music(self) -> None:
        if getattr(self, "_suppress_resume", False):
            # Кнопка паузы не должна сама возвращать музыку: пользователь
            # именно поставил её на паузу. Флаг снимется при следующем
            # голосовом ходе в _on_phrase.
            log.info("%s: музыку не возвращаю — пауза с кнопки", self.name)
            return
        if self._resume_task is None or self._resume_task.done():
            self._resume_task = asyncio.create_task(self._resume_music())

    async def _pause_music_for_turn(self) -> None:
        """Поставить музыку на паузу перед подтверждённым ходом.

        Вызывается только после проверки имени в ASR, а не на сыром срабатывании
        детектора. Иначе «Алиса, продолжить» (обращение к чужому ассистенту)
        ложно глушит музыку и слышится как рывок.
        """
        if getattr(self, "_music_paused_for_turn", False):
            return
        self._music_paused_for_turn = True
        res = await self.command({"cmd": "pause"}, timeout=15) or {}
        # Запоминаем, играла ли музыка: если нет — возвращать нечего,
        # иначе play включал музыку там, где её изначально не было.
        was_agent = bool(res.get("was_playing"))
        self._agent_sendspin = bool(res.get("sendspin"))
        # Наш поток реально звучал? sendspin (engaged) отвечает «плеер
        # занят», и после нашей же паузы он остаётся true. Для решения,
        # чью паузу запоминать, нужен факт воспроизведения.
        spin_was = bool(res.get("sendspin_playing"))
        native_was = was_agent and not spin_was
        if not getattr(self, "_ma_paused", False):
            # Очередь MA гасим по её собственному состоянию: aplay мог быть
            # мёртв (падал с file already closed), но поток MA при этом идёт,
            # и без паузы очередь продолжит играть.
            # Если звучал нативный плеер, а MA — нет, owner ниже станет alice.
            try:
                self._ma_paused = await ma_control.pause_if_playing(self.name)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: MA-пауза не прошла: %s", self.name, e)
                self._ma_paused = False
        self._was_playing = was_agent or self._ma_paused
        # Снимок для автомата: кто на паузе, кто владелец и почему.
        if self._ma_paused:
            self._sources.ma = sources.PAUSED
            self._sources.owner = sources.MA
        if self._agent_sendspin:
            self._sources.agent = sources.PAUSED
        if native_was:
            # Владелец — Алиса: она реально звучала. Важно ставить это
            # после MA-блока, иначе owner перезапишется на MA.
            self._sources.alice = sources.PAUSED
            self._sources.owner = sources.ALICE
        if self._was_playing:
            self._sources.pause_reason = sources.REASON_HUB
            # Пауза теперь наша, а не кнопочная: запрет «не возвращать после
            # кнопки» к этой тишине не относится. Иначе ложная активация
            # («Алиса, что сейчас играет?») оставляла колонку немой до
            # следующего нажатия.
            self._suppress_resume = False
        log.info("%s: пауза музыки, было воспроизведение=%s (MA=%s, поток=%s, натив=%s)",
                 self.name, self._was_playing, self._ma_paused, spin_was, native_was)
        if res.get("err"):
            log.warning("%s: пауза не подтверждена: %s", self.name, res["err"])

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
        # Ответ ещё звучит или ждёт очереди — ждём его конца, а не включаем
        # музыку поверх речи. Раньше окно продолжения истекало во время речи,
        # состояние уходило в IDLE, и play уходил в 18:42:11 при «звук доигран»
        # в 18:42:15. Предел — страховка от зависшего флага.
        waited = 0.0
        while self._output_busy() and waited < MAX_SPEECH_WAIT:
            await asyncio.sleep(0.25)
            waited += 0.25
        if self._output_busy():
            log.warning("%s: ответ не доиграл за %.0f с — музыку не трогаю",
                        self.name, MAX_SPEECH_WAIT)
            return
        if not should_resume_music(self.core.follow_up, self.core.coll is not None,
                                   self.core.state):
            return
        if not getattr(self, "_was_playing", False):
            # Музыки не было до активации — не включаем её «из ниоткуда».
            log.info("%s: музыку не возвращаю — до активации не играла", self.name)
            return
        if getattr(self, "_ma_paused", False):
            # Сначала снимаем паузу с очереди MA: она снова начнёт отдавать
            # поток с той же позиции. Локальный aplay поднимется командой play
            # ниже, когда поток уже пойдёт.
            try:
                await ma_control.resume_if_paused(self.name)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: MA не вернул музыку: %s", self.name, e)
            self._ma_paused = False
            if not getattr(self, "_agent_sendspin", False):
                # Агент паузу не ставил (Sendspin не был engaged). После
                # resume MA сам пришлёт stream/start и поднимет aplay.
                # Команда play здесь подняла бы нативное радио Алисы.
                self._was_playing = False
                self._sources.ma = sources.PLAYING
                self._sources.agent = sources.STREAMING
                self._sources.owner = sources.MA
                self._sources.pause_reason = "none"
                log.info("%s: музыку вернул MA, агент play не нужен", self.name)
                return
        res = await self.command({"cmd": "play"}, timeout=15) or {}
        if res.get("err"):
            log.warning("%s: музыку вернуть не удалось: %s", self.name, res["err"])
            return
        self._was_playing = False
        self._agent_sendspin = False
        self._sources.ma = sources.PLAYING
        self._sources.agent = sources.STREAMING
        self._sources.owner = sources.MA
        self._sources.pause_reason = "none"
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
        # Текущий пик лежит в конце снимка: он не повтор, его исключаем.
        secs = len(pcm) / 2 / 16000
        earlier = [p for p in peaks if p[0] < secs - 1.2]
        if earlier:
            # Пользователь звал до срабатывания: берём историю, но не больше
            # 3 с. Снимок 6 с целиком раздувал ASR-аудио и модель путалась.
            keep = 3 * 32000
            self.core.prefix = bytes(pcm[-keep:]) + self.core.prefix
            log.info("%s: повтор ключа до активации (%d), префикс %.1f с",
                     self.name, len(earlier),
                     len(self.core.prefix) / 2 / 16000)
        elif len(peaks) >= 2:
            log.info("%s: ретро %d вхождений ключа за 6 с: %s", self.name,
                     len(peaks), ", ".join("%.2fс/%.2f" % (t, p) for t, p in peaks[:5]))

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
            # transpose ожидает numpy-массив (normalize_for_asr читает .size);
            # с bytes падало «'bytes' object has no attribute 'size'», и ни один
            # промах не проверялся распознаванием — датасет near-miss был пуст.
            text = await self._asr(window)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: промах не проверен распознаванием: %s", self.name, e)
            return
        if not voice_dialog.has_wake_name(text or ""):
            log.info("%s: промах пик %.3f — не обращение (%r), не пишу",
                     self.name, peak_score, (text or "")[:60])
            return
        # Обращение в промахе подтверждает только датасет near-miss, но не
        # активирует ответ: порог детектора остаётся единственным условием
        # запуска. Раньше здесь вызывался process_miss(), и фраза ниже порога
        # всё равно уходила в работу — это давало ложные активации.
        meta = {"client": self.name, "text": text, "reason": "near_miss",
                "score": round(peak_score, 3)}
        try:
            path = self.collector.save("misses", snap, peak, meta)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: промах не сохранён: %s", self.name, e)
            return
        if path:
            log.info("%s: промах пик %.3f → %s", self.name, meta["score"], path)
        METRICS.inc("wake_misses_total", {"station": self.name})

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

    async def _music_command(self, action: str, seconds: int | None) -> None:
        try:
            text = await ma_control.control(self.name, action, seconds)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: MA %s не прошёл: %s", self.name, action, e)
            return
        if action == "status" and text:
            try:
                data = await asyncio.to_thread(voice_dialog.synthesize, text)
                await self.command({"cmd": "say", "volume": voice_dialog.SAY_VOLUME},
                                   timeout=10)
                await self.send_binary(data)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: ответ про трек не озвучен: %s", self.name, e)
        elif action in ("play", "pause", "stop", "next", "previous", "seek"):
            log.info("%s: музыка %s%s", self.name, action,
                     f" {seconds}s" if seconds is not None else "")

    async def _on_phrase(self, text: str, origin: str, had_wake: bool):
        log.info("%s: фраза %r (origin=%s, had_wake=%s)", self.name, text, origin, had_wake)
        if origin in ("wake", "barge"):
            # Новый голосовой ход снимает запрет на возврат музыки,
            # поставленный кнопкой паузы.
            self._suppress_resume = False
        # Имя подтверждено ядром (name_like/триггер), значит это действительно
        # наш ход. Только теперь глушим музыку — ложное срабатывание на чужое
        # имя до этой точки музыку не трогает.
        if origin == "wake":
            await self._pause_music_for_turn()
        if origin in ("wake", "barge"):
            # Сохранение не должно задерживать ответ: ждём хвост в фоне.
            asyncio.create_task(self._save_settled(
                voice_capture.classify(text), text, origin))
        if voice_dialog.is_stop_command(text):
            # «Стоп» — команда обрыва, а не вопрос. В LLM её нельзя: модель
            # подумает над словом и снова заговорит через десятки секунд.
            log.info("%s: стоп — обрываю, в LLM не иду", self.name)
            METRICS.inc("stop_total", {"station": self.name})
            # Автомат: оборвать озвучку и вернуть только ту музыку, которую
            # на паузу поставили мы. Чужую паузу (кнопка/приложение) не трогаем.
            await self._reconcile("stop")
            await self.core.drop_dialog()
            return
        if voice_dialog.is_just_wake(text):
            log.info("%s: только обращение, жду фразу", self.name)
            self.core.expect_followup()
            return
        if origin not in ("dialog", "followup", "button", "barge") and not had_wake \
                and not voice_dialog.has_wake_name(text):
            log.info("%s: нет обращения, игнор: %r", self.name, text[:80])
            return
        # Обращение срезаем один раз: этот же текст уходит и в HA, и в LLM.
        clean = voice_dialog.strip_wake(text)
        if clean != text:
            log.info("%s: без обращения %r", self.name, clean)

        mi = ma_control.parse(clean or text)
        if mi:
            log.info("%s: музыка → %s %s", self.name, mi[0], mi[1] or "")
            await self._music_command(mi[0], mi[1])
            return

        # Фраза-триггер из конфига уходит в Home Assistant и НЕ идёт в LLM:
        # иначе сцена выполнится и агент ещё ответит. Сверяем только ход,
        # начатый ключевым словом: «да»/«нет» в окне диалога — это
        # подтверждение, а не сценарий, и в MQTT им нельзя.
        if origin in ("wake", "barge") and clean:
            matched = match_intent(clean)
            if matched:
                sent = (MQTT_BRIDGE is not None
                        and MQTT_BRIDGE.publish_phrase(self.name, matched))
                if sent:
                    METRICS.inc("phrases_total", {"route": "intent", "station": self.name})
                    METRICS.inc("intents_total", {"phrase": matched, "station": self.name})
                    log.info("%s: фраза-триггер %r → HA, в LLM не идём",
                             self.name, matched)
                    return
                # Мост выключен или брокер недоступен: молча проглотить
                # фразу нельзя, иначе сцена не выполнится и агент промолчит.
                log.warning("%s: фраза-триггер %r не отправлена в HA — "
                            "отдаю в LLM", self.name, matched)

        await self._speak(clean or text, origin=origin)

    async def _speak(self, text: str, origin: str | None = None):
        first_audio = asyncio.Event()
        started = time.monotonic()

        async def on_audio(chunk: bytes):
            # Ход отменён (перебивание, «стоп», кнопка): синтез и плеер могут
            # ещё жить своими задачами. Отправлять их звук в колонку нельзя —
            # иначе после команды `stop` прилетают новые кадры и ответ
            # начинает звучать заново уже во время новой фразы.
            if not self._sending_audio:
                return
            if self.role == "station":
                if not first_audio.is_set():
                    # Кольцо должно гореть «говорит», пока звучит ответ,
                    # а не только пока сервер отправляет чанки.
                    try:
                        # timeout -1: без страховочного автосброса 15 с. Длинный
                        # ответ иначе терял кольцо посреди речи.
                        await self.command({"cmd": "led", "name": "hermes_speaking",
                                            "timeout": -1}, timeout=15)
                    except Exception as e:  # noqa: BLE001
                        log.warning("%s: led speaking не показан: %s", self.name, e)
                # Новая порция звука — значит, конец предыдущей ещё не наступил
                # или ещё не наступит: сбрасываем прошлое подтверждение и
                # отодвигаем оценку до конца именно этого чанка.
                self._play_done = False
                self._audio_deadline = speech_end_at(
                    asyncio.get_running_loop().time(), wav_seconds(chunk),
                    self._audio_deadline)
            # Первый звук ответа гасит заполнители: дальше они только мешают.
            if not first_audio.is_set():
                METRICS.observe("answer_seconds", time.monotonic() - started,
                                {"station": self.name})
                METRICS.observe("answer_chars", len(text), {"station": self.name})
                METRICS.inc("phrases_total", {"route": "llm", "station": self.name})
            first_audio.set()
            # Команда «say» нужна только колонке: она поднимает динамик и
            # запускает gst. Шлём один раз на ответ, а не на каждый чанк:
            # каждый say заново стартует плеер и на длинной фразе режет звук.
            if self.role == "station" and not self._pending_say:
                self._pending_say = True
                await self.command({"cmd": "say", "volume": voice_dialog.SAY_VOLUME},
                                   timeout=10)
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
        if replies.fillers_enabled():
            filler_task = asyncio.create_task(self._fill_silence(first_audio))

        # Быстрый маршрут должен знать, какая колонка говорит: громкость
        # и прочие команды применяются к ней, а не к первой подключённой.
        try:
            from . import fast_route
        except ImportError:
            import fast_route
        fast_route.set_station(self.name)
        # Перебивание не должно попадать в быстрый дом: там триггер — любое
        # командное слово в тексте, и разговорная фраза выполнится как команда.
        route_origin = self._route_origin(origin)
        if route_origin != origin:
            log.info("%s: ход после перебивания — быстрый дом выключен", self.name)
        fast_route.set_origin(route_origin)
        self.turn = voice_dialog.DialogTurn(text, on_audio=on_audio,
                                            on_event=on_event)
        self._sending_audio = True
        cancelled = False
        try:
            answer = await self.turn.run()
            silent = getattr(self.turn, "silent", False)
        except asyncio.CancelledError:
            # Перебивание/кнопка: ход отменён намеренно. Это не ошибка LLM,
            # поэтому ни `error`, ни окно диалога дальше не открываем.
            log.info("%s: ход отменён", self.name)
            cancelled = True
            answer = ""
            silent = False
        finally:
            self._sending_audio = False
            if filler_task is not None:
                filler_task.cancel()
            self.turn = None

        if cancelled:
            return
        if not answer:
            if silent:
                METRICS.inc("phrases_total", {"route": "ha", "station": self.name})
                log.info("%s: команда выполнена молча", self.name)
                return
            METRICS.inc("errors_total", {"stage": "llm", "station": self.name})
            await self.send_json({"t": "error", "what": "llm"})
            return
        if self.turn is None and self.core.state is not State.BUSY:
            pass
        self.core.open_dialog(voice_dialog.looks_like_question(text))
        if self._audio_playing():
            # Окно открыто, но микрофон пока не слушаем: колонка доигрывает
            # ответ. Отпустим в _finish_speech, когда закончится звук.
            self._dialog_hold = True
            self._dialog_question = voice_dialog.looks_like_question(text)
            self.core.dialog_collect_done = True
            log.info("%s: сбор продолжения отложен до конца озвучки", self.name)
        # Задача на конец речи создаётся здесь же: после open_dialog состояние
        # уже DIALOG_WAIT, и переход BUSY→IDLE в _recognize не случится —
        # иначе кольцо и музыка ждали бы закрытия окна (12 с).
        self._clear_led_after_audio()

    async def _fill_silence(self, first_audio: asyncio.Event) -> None:
        """Заготовки в тишине, пока агент не выдал первый звук ответа.

        Включается только при VOICE_FILLERS=1: пока не отлажено, по умолчанию
        молчим. Первая фраза — если ответ задержался дольше FIRST секунд,
        дальше пауза случайная, чтобы не тараторить.
        """
        first_s, min_gap, max_gap, max_count = replies.fillers_timing()
        try:
            await asyncio.wait_for(first_audio.wait(), timeout=first_s)
            return
        except asyncio.TimeoutError:
            pass
        said = 0
        while said < max_count and not first_audio.is_set():
            phrase = replies.take("think")
            data = replies.audio(phrase)
            if data:
                if self.role == "station":
                    await self.command({"cmd": "say",
                                        "volume": voice_dialog.SAY_VOLUME}, timeout=30)
                await self.send_binary(data)
                said += 1
                log.info("%s: заполнитель %r", self.name, phrase)
            gap = random.uniform(min_gap, max_gap)
            try:
                await asyncio.wait_for(first_audio.wait(), timeout=gap)
                return
            except asyncio.TimeoutError:
                continue

    def _route_origin(self, origin: str | None) -> str | None:
        """Куда идти фразе. После перебивания — только агенту, до конца диалога."""
        if origin in ("wake", "button"):
            # Явное обращение с нуля всегда начинает новый диалог.
            return origin
        return "barge" if getattr(self, "_barge_dialog", False) else origin

    async def _on_barge(self) -> None:
        """Перебивание голосом: оборвать звук и текущий ход агента.

        Вызывается ядром до начала сбора новой фразы. Музыку не возвращаем:
        она остаётся на паузе до конца нового хода.
        """
        self._cancel_led_task()
        self._sending_audio = False
        self._dialog_hold = False
        self._audio_deadline = 0.0
        self._play_done = False
        self._pending_say = False
        self._barge_guard_until = asyncio.get_running_loop().time() + BARGE_GUARD_S
        self._barge_dialog = True
        turn, self.turn = self.turn, None
        if turn is not None:
            turn.cancel()
        if self.role != "station":
            return
        try:
            # Очередь уже отправленных кусков доиграет без этой команды:
            # сервер шлёт звук пачками, а gst играет из буфера.
            await self.command({"cmd": "stop"}, timeout=5)
            log.info("%s: звук остановлен (перебивание)", self.name)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: stop не подтверждён: %s", self.name, e)

    async def interrupt(self, long: bool = False):
        # Это сообщение приходит от физической кнопки паузы. Музыку после
        # такого прерывания не возвращаем автоматически: иначе через
        # RESUME_DELAY срабатывает play и кнопка «включает» радио обратно.
        # Но флаг ставим не здесь, а по факту действий автомата ниже: если
        # нажатие наоборот включило источник, возвращать нечего.
        self._suppress_resume = False
        resume = getattr(self, "_resume_task", None)
        if resume is not None and not resume.done():
            resume.cancel()
        self._resume_task = None
        if self.turn is not None:
            self.turn.cancel()
        self.turn = None
        await self.core.interrupt()
        if self.role == "station":
            # Кнопка — toggle владельца; длинное нажатие — явное «Алиса».
            # Перед решением снимаем факты: пауза могла быть поставлена из
            # приложения/с ПК, и локальный снимок о ней не знает.
            await self._sync_sources_snapshot()
            # Снимок в лог: без него не видно, почему кнопка решила «тишина»,
            # когда музыка играет.
            log.info("%s: кнопка, снимок %s", self.name, self._sources)
            actions = await self._reconcile("switch_source" if long else "button")
            # Запрет на автовозврат нужен только когда кнопка действительно
            # поставила паузу. Если она вернула владельца (play_*), музыка уже
            # звучит, и запрет лишь оставил бы её немой после ложной активации.
            self._suppress_resume = any(
                a in actions for a in (sources.PAUSE_ALICE, sources.PAUSE_MA,
                                       sources.PAUSE_AGENT, sources.PAUSE_SPIN))
            # maind на то же нажатие поднимает свой источник, даже если мы
            # поставили паузу. Глушим натив сразу, не дожидаясь watchdog:
            # он даёт ~0.2 с слышимой Алисы. Окно приоритета не даёт maind
            # поднять её повторно.
            # Если нажатие намеренно отдано maind (тишина, пустая очередь) —
            # ничего не глушим, Алиса там и должна играть.
            # Натив гасим только когда его паузу решил автомат (PAUSE_ALICE).
            # Кнопка натив не трогает: у maind своё toggle-состояние, и наши
            # команды сбивают его — тогда пауза начинает требовать двух нажатий.
            if sources.PAUSE_ALICE in actions:
                self._button_guard_until = time.monotonic() + BUTTON_GUARD
                await self.command({"cmd": "native_pause"}, timeout=5)
            elif not long and sources.PLAY_MA in actions:
                self._button_guard_until = time.monotonic() + BUTTON_GUARD
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
                # AGC намеренно не применяем: усиление должно быть только
                # после ключевого слова, в normalize_for_asr перед отправкой
                # в распознавание. Иначе тихая речь искажается ещё до ASR.
                data = raw
                self.ring.add(raw)
                self.rec.write(raw)
                if self._f is not None:
                    self._f.write(data)
                if self._rawf is not None:
                    self._rawf.write(raw)
                self.audio_bytes += len(data)
                self.last_audio_at = time.time()
                METRICS.inc("station_audio_bytes_total", {"station": self.name}, len(data))
                METRICS.set("station_last_audio_timestamp_seconds",
                            self.last_audio_at, {"station": self.name})
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
        ma_control.forget(self.name)
        self._q: asyncio.Queue = asyncio.Queue(maxsize=256)
        worker = asyncio.create_task(self._audio_worker())
        probe = asyncio.create_task(self._probe_loop())
        overlap = asyncio.create_task(self._overlap_watch())
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
            overlap.cancel()
            vol_sync = getattr(self, "_vol_sync_task", None)
            if vol_sync is not None:
                vol_sync.cancel()
            if self.turn is not None:
                self.turn.cancel()
            await self.core.interrupt()
            self.rec.close()
            for f in (self._f, self._rawf):
                if f is not None:
                    try:
                        f.close()
                    except Exception:  # noqa: BLE001
                        pass
            log.info("%s: отключён, аудио %.1f МБ", self.name,
                     self.audio_bytes / 1048576)
            METRICS.set("station_online", 0, {"station": self.name})

    async def _overlap_watch(self) -> None:
        """Последний источник музыки выигрывает.

        Sendspin и нативный mediad могут зазвучать одновременно: MA уже
        играет, а Алиса запустила радио (или наоборот). При старте Sendspin
        агент сам глушит mediad, но обратного события у него нет. Поэтому
        раз в полсекунды спрашиваем состояние и, если нативный плеер заиграл
        при живом Sendspin, ставим на паузу оба источника. MA — через
        ma_control, локальный aplay — командой pause агенту.
        """
        while True:
            await asyncio.sleep(0.5)
            if self.role != "station":
                continue
            try:
                res = await self.command({"cmd": "state"}, timeout=2)
            except Exception:  # noqa: BLE001 — станция могла отвалиться
                continue
            if not res.get("ok"):
                continue
            await self._handle_overlap(
                bool(res.get("native_playing")),
                bool(res.get("sendspin_playing")),
                bool(res.get("sendspin_engaged")),
            )

    async def _apply(self, actions: list[str]) -> None:
        """Исполнить решение автомата. Сбой одного действия не рвёт остальные."""
        for act in actions:
            try:
                if act == sources.PAUSE_MA:
                    ok = await ma_control.pause_if_playing(self.name)
                    # MA применяет команду за 2-3 с. Намерение держим только
                    # когда пауза действительно поставлена: иначе снимок
                    # начнёт врать про MA, который и не играл.
                    if ok:
                        self._ma_pending = ("paused", time.monotonic() + MA_CONFIRM)
                elif act == sources.PLAY_MA:
                    ok = await ma_control.resume_if_paused(self.name)
                    if not ok:
                        ok = await ma_control.play_forced(self.name)
                    if ok:
                        self._ma_pending = ("playing", time.monotonic() + MA_CONFIRM)
                elif act == sources.PAUSE_AGENT:
                    await self.command({"cmd": "pause"}, timeout=5)
                elif act == sources.PAUSE_SPIN:
                    await self.command({"cmd": "sendspin_pause"}, timeout=5)
                elif act == sources.PLAY_AGENT:
                    await self.command({"cmd": "play"}, timeout=10)
                elif act == sources.PAUSE_ALICE:
                    await self.command({"cmd": "native_pause"}, timeout=5)
                elif act == sources.PLAY_ALICE:
                    await self.command({"cmd": "native_play"}, timeout=5)
                elif act == sources.STOP_AGENT:
                    await self.command({"cmd": "stop"}, timeout=5)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: действие %s не выполнено: %s",
                            self.name, act, e)

    async def _sync_sources_snapshot(self) -> None:
        """Снять факты у MA перед решением (кнопка, возврат музыки).

        Локальный снимок знает только то, что делали мы. Пауза из
        приложения/с ПК видна лишь в самом MA. Состояние Алисы здесь не
        нужно: её плеером занимается maind, мы кнопку в его режиме не трогаем.
        """
        try:
            st = await ma_control.player_state(self.name)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: состояние MA не получено: %s", self.name, e)
            return
        if st == "playing":
            self._sources.ma = sources.PLAYING
            self._sources.agent = sources.STREAMING
        elif st == "paused":
            self._sources.ma = sources.PAUSED
        elif st in ("idle", "off"):
            # idle после pause с ПК тоже значит «продолжать есть что».
            if self._sources.ma == sources.PLAYING:
                self._sources.ma = sources.PAUSED
        # MA отвечает не сразу: пока ждём подтверждения, держим намерение,
        # иначе быстрый повторный клик увидит старое состояние.
        pend = self._ma_pending
        if pend:
            want, until = pend
            if st == want or time.monotonic() > until:
                self._ma_pending = None
            elif want == "paused":
                self._sources.ma = sources.PAUSED
            else:
                self._sources.ma = sources.PLAYING
                self._sources.agent = sources.STREAMING
        try:
            n = await ma_control.queue_len(self.name)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: очередь MA не получена: %s", self.name, e)
            n = -1
        if n == 0:
            self._sources.ma_queue = "empty"
        elif n > 0:
            self._sources.ma_queue = "filled"

    async def _reconcile(self, event: str) -> list[str]:
        """Пропустить событие через автомат и исполнить решение."""
        dec = sources.decide(self._sources, event)
        self._sources = dec.sources
        if dec.actions:
            log.info("%s: %s → %s", self.name, event, ", ".join(dec.actions))
        await self._apply(dec.actions)
        # Держим старые флаги в согласии со снимком: на них ещё завязаны
        # возврат музыки и выбор команды play/pause.
        self._ma_paused = dec.sources.ma == sources.PAUSED
        self._agent_sendspin = dec.sources.agent == sources.PAUSED
        return dec.actions

    async def _handle_overlap(self, native: bool, spin: bool,
                              engaged: bool) -> None:
        """Один замер трёх источников → событие автомата.

        `native` — нативный mediad (Алиса), `spin` — наш Sendspin-поток,
        `engaged` — плеер MA подключён. Решение принимает `sources.decide`,
        поэтому «глушу оба» и повторные срабатывания невозможны: второй
        источник гасится один раз на переходе.
        """
        s = self._sources
        prev_native = getattr(self, "_prev_native", False)
        prev_spin = getattr(self, "_prev_spin", False)
        self._prev_native, self._prev_spin = native, spin

        event: str | None = None
        if native:
            s.alice = sources.PLAYING
            if not prev_native:
                event = "alice_started"
        elif s.alice == sources.PLAYING:
            s.alice = sources.PAUSED

        # `spin` — это наш aplay, а не «MA играет»: после pause поток может
        # доигрывать буфер. Поэтому MA помечаем играющим только на переходе,
        # иначе watchdog снова и снова «видит» конфликт и глушит по кругу.
        if spin:
            s.agent = sources.STREAMING
            if not prev_spin:
                s.ma = sources.PLAYING
                if native and event is None:
                    event = "ma_started"
        else:
            if s.agent == sources.STREAMING:
                s.agent = sources.PAUSED
            if s.ma == sources.PLAYING:
                s.ma = sources.PAUSED

        if event is None and native and spin:
            event = "poll"
        if event is None:
            return

        # Идёт наш ответ: натив поднялся сам (maind на кнопку) — гасим его,
        # а не музыку, которую вернём после хода. Иначе агент и Алиса
        # говорят одновременно.
        if event == "alice_started" and s.pause_reason == sources.REASON_HUB:
            log.info("%s: натив заиграл во время ответа — глушу натив", self.name)
            try:
                await self.command({"cmd": "native_pause"}, timeout=5)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: натив во время ответа не заглушен: %s", self.name, e)
            return

        # Кнопка только что вернула MA: maind на то же нажатие поднимает
        # свой источник. Гасим натив, пока окно приоритета MA открыто.
        if event == "alice_started" and time.monotonic() < self._button_guard_until:
            log.info("%s: натив заиграл после кнопки — глушу только натив",
                     self.name)
            try:
                await self.command({"cmd": "native_pause"}, timeout=5)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: натив после кнопки не заглушен: %s", self.name, e)
            return

        await self._reconcile(event)

    def _queue_volume_sync(self, percent: int | None) -> None:
        """Поставить в очередь актуальную громкость для MA.

        Ползунок HA может прислать пачку событий; последовательные MCP-вызовы
        шли бы вразнобой. Держим только последнее значение: пока идёт вызов,
        новые события заменяют ожидающее, а не плодят задачи.
        """
        if percent is None:
            return
        self._vol_sync_pending = int(percent)
        task = getattr(self, "_vol_sync_task", None)
        if task is None or task.done():
            self._vol_sync_task = asyncio.create_task(self._volume_sync_loop())

    async def _volume_sync_loop(self) -> None:
        while True:
            pct = self._vol_sync_pending
            self._vol_sync_pending = None
            if pct is None:
                return
            try:
                ok = await ma_control.set_volume(self.name, pct, force=True)
                if not ok:
                    log.warning("%s: MA-плеер не найден для громкости %d%%",
                                self.name, pct)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: громкость в MA не отправлена: %s", self.name, e)
            if self._vol_sync_pending is None:
                return

    async def _probe_loop(self) -> None:
        """Раз в минуту проверяем, не пора ли отдать строку замера в журнал."""
        try:
            metrics.scan_dataset(self.collector.root)
            while True:
                await asyncio.sleep(60)
                line = self.probe.report()
                if line:
                    log.info("%s: %s", self.name, line)
                metrics.scan_dataset(self.collector.root)
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
            asyncio.create_task(self.interrupt(bool(msg.get("long"))))
        elif kind == "play_done":
            # Колонка доиграла ответ: только теперь можно гасить кольцо и
            # возвращать музыку. Отправка в сокет — не конец звука.
            asyncio.create_task(self.on_play_done())
        elif kind == "volume":
            # Громкость поменяли голосом Алисы или кнопкой — мост подхватит
            # на следующем опросе состояния.
            try:
                lvl = msg.get("level")
                self.volume = int(lvl) if lvl is not None else self.volume
                pct = msg.get("percent")
                self.volume_percent = int(pct) if pct is not None else self.volume_percent
            except (TypeError, ValueError):
                self.volume = None
            db = msg.get("db")
            self.volume_db = float(db) if isinstance(db, (int, float)) else None
            # Единственный канал синхронизации: ALSA изменилась (кнопка,
            # Алиса или наша команда) — сообщаем MA через хаб. Отдельного
            # sendspin-канала больше нет. Если изменение пришло из MA
            # (`from_ma`), обратно его не отправляем — иначе громкость
            # мигает между двумя уровнями.
            if (getattr(self, "role", "") == "station"
                    and self.volume_percent is not None
                    and not bool(msg.get("from_ma"))):
                self._queue_volume_sync(self.volume_percent)
        elif kind == "start":
            # Явный старт хода без ключевого слова: кнопка/орб панели.
            asyncio.create_task(self.core.begin())
        elif kind == "text" and msg.get("text"):
            # отладка и серверные уведомления: синтезировать текст без ASR
            asyncio.create_task(self._speak(str(msg["text"])))
        elif kind == "ping":
            await self.send_json({"t": "pong"})


# ---------- HTTP ----------

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

    role = str(hello.get("role") or "station")
    mac = normalize_mac(hello.get("mac"))
    sent_name = str(hello.get("name") or "")
    by_mac = station_by_mac(mac) if role == "station" else None
    if by_mac and sent_name and by_mac != sent_name:
        log.warning("%s: MAC %s описан как %s — беру имя из конфига",
                    sent_name, mac, by_mac)
    if by_mac:
        name = by_mac
    elif sent_name:
        name = sent_name
    elif mac:
        # Станция не описана: работаем, но пороги у неё общие, а не свои.
        name = "mac-" + mac.replace(":", "")[-8:]
        log.warning("станция с MAC %s не описана в wake.stations — имя %s, пороги общие",
                    mac, name)
    else:
        name = f"client-{int(time.time())}"
    # Порог и усиление — из hub.json: детектор работает здесь, а не на колонке.
    # Старый агент присылает свои значения; они больше не применяются, но
    # расхождение видно в журнале — иначе непонятно, почему «настройка не работает».
    wake_threshold = station_wake(name, "threshold", 0.95)
    gain = station_wake(name, "gain", 10.0)
    for key, from_hub in (("threshold", wake_threshold), ("gain", gain)):
        sent = hello.get(key)
        try:
            if sent is not None and abs(float(sent) - from_hub) > 1e-6:
                log.warning("%s: агент сообщил %s=%s, в hub.json %.2f — применяю значение хаба",
                            name, key, sent, from_hub)
        except (TypeError, ValueError):
            pass
    cfg = CoreConfig(
        wake_threshold=wake_threshold,
        # Пауза перед проверкой конца и страховка по тишине: на 0.5 с колонка
        # закрывала фразу, пока человек ещё не договорил, и команда уходила
        # в дом мгновенно — быстрее, чем он успевал продолжить.
        silence_ms=config.as_int("dialog", "silence_ms", default=900),
        hard_silence_ms=config.as_int("dialog", "hard_silence_ms", default=3500),
        # 2.0 с: пик детектора приходит с задержкой, и при 1.6 с обращение
        # срезалось на грани. 3.0 с не нужны — разбор шёл только из-за «ь»
        # в «дерьмя», после нормализации хватает и 2.0.
        pre_roll_s=config.as_float("dialog", "pre_roll_s", default=3.0),
        # Перебивание голосом: ключ во время ответа обрывает ход. Флаг нужен,
        # чтобы отключать без правки кода (HUB_DIALOG_BARGE_IN=0 или hub.json).
        barge_in=config.as_bool("dialog", "barge_in", default=True),
    )
    conn = Conn(
        ws, name, role, cfg,
        gain=gain,
        agc=bool(hello.get("agc", role == "station")),
        write_audio=bool(hello.get("write_audio", True)),
    )
    CONNS[name] = conn
    try:
        if hello.get("level") is not None:
            conn.volume = int(hello["level"])
        elif hello.get("volume") is not None:
            conn.volume = max(0, min(10, round(int(hello["volume"]) / 10)))
        if hello.get("volume") is not None:
            conn.volume_percent = int(hello["volume"])
        conn.volume_db = (float(hello["volume_db"])
                          if hello.get("volume_db") is not None else None)
    except (TypeError, ValueError):
        conn.volume = None
        conn.volume_db = None
    log.info("%s: hello %s", name, hello)
    await conn.send_json({"t": "hello_ok", "name": name})
    if role == "station":
        sp = station_sendspin(name)
        if sp:
            await conn.send_json({"t": "sendspin", "config": sp})
            log.info("%s: sendspin config отправлен (name=%s)", name, sp["name"])
        else:
            log.info("%s: sendspin выключен в hub.json", name)
    try:
        await conn.run()
    finally:
        CONNS.pop(name, None)
        await ws.close()
    return ws


async def metrics_view(_request: web.Request) -> web.Response:
    """Экспорт метрик для Prometheus."""
    for conn in CONNS.values():
        METRICS.set("station_online", 1, {"station": conn.name})
        METRICS.set("station_last_audio_timestamp_seconds",
                    getattr(conn, "last_audio_at", 0.0), {"station": conn.name})
    return web.Response(text=metrics.render(),
                        content_type="text/plain; version=0.0.4")


def _local_only(request: web.Request) -> bool:
    """Диагностический `/route` — только с самой машины.

    Маршрут выполняет настоящие команды (чайник, свет), поэтому с LAN он
    должен отвечать 403: иначе любой в сети мог бы дёргать дом. Панель и
    скрипты ходят на `127.0.0.1`, им ограничение не мешает.
    """
    return request.remote in ("127.0.0.1", "::1")


async def health(_request: web.Request) -> web.Response:
    now = time.monotonic()
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


async def station_volume(name: str | None = None) -> int | None:
    """Фактическая громкость станции: спрашиваем агент, а не HA.

    `number.hermes_<station>_volume` в HA обновляется по опросу моста
    (раз в 5 с), поэтому относительные команды («прибавь») считались бы
    от устаревшего значения. Агент читает ALSA у себя и отвечает сразу.
    """
    conn = _station_conn(name)
    if conn is None:
        return None
    try:
        res = await conn.command({"cmd": "volume"}, timeout=3)
    except Exception:  # noqa: BLE001
        return None
    if res.get("level") is not None:
        return int(res["level"])
    vol = res.get("volume")
    # старые агенты отдают проценты — переводим в уровень
    return max(0, min(10, round(int(vol) / 10))) if vol is not None else None


def _station_conn(name: str | None = None):
    """Подключение колонки: по имени, а без имени — единственная.

    Если имя задано и такой колонки нет, возвращаем None: молча применить
    команду к другой колонке хуже, чем не применить вовсе.
    """
    conns = _station_conns()
    if not conns:
        return None
    if name:
        return next((c for c in conns if c.name == name), None)
    return conns[0] if len(conns) == 1 else None


async def station_set_volume(level: int, name: str | None = None) -> bool:
    """Поставить громкость через агент колонки — единственный путь для всех источников.

    Голос, MQTT-мост и HA-слайдер приходят сюда, а агент применяет значение
    у себя (ALSA). Никаких команд из облака.
    """
    conn = _station_conn(name)
    if conn is None:
        return False
    try:
        res = await conn.command({"cmd": "volume_set", "level": max(0, min(10, int(level)))},
                                 timeout=5)
    except Exception as e:  # noqa: BLE001
        log.warning("громкость: агент %s не ответил на volume_set: %s", conn.name, e)
        return False
    if not res.get("ok"):
        log.warning("громкость: агент %s отказал в volume_set: %r", conn.name, res)
        return False
    return True


def _station_conns() -> list:
    """Все подключённые колонки: мост строит сущности на каждую."""
    return [c for c in CONNS.values() if getattr(c, "role", "") == "station"]


async def _mqtt_startup(app: web.Application) -> None:
    global MQTT_BRIDGE
    MQTT_BRIDGE = mqtt_bridge.MqttBridge(_station_conns)
    app["mqtt"] = MQTT_BRIDGE
    await MQTT_BRIDGE.start()


async def _mqtt_cleanup(app: web.Application) -> None:
    global MQTT_BRIDGE
    bridge = app.get("mqtt")
    if bridge is not None:
        await bridge.stop()
    MQTT_BRIDGE = None


async def route_probe(request: web.Request) -> web.Response:
    """Что ответит быстрый маршрут на фразу — без озвучки.

    Нужен, чтобы на «почему ушло в LLM» отвечать фактом, а не догадкой.
    Внимание: маршрут выполняется по-настоящему, действие произойдёт.
    """
    # Маршрут выполняет настоящие команды (чайник, свет), поэтому только
    # с самой машины: иначе любой в сети мог бы дёргать дом.
    if not _local_only(request):
        return web.json_response({"error": "только локально"}, status=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)
    text = str(body.get("text") or "").strip()
    station = body.get("station")
    try:
        from . import fast_route
    except ImportError:
        import fast_route
    fast = fast_route.route()
    if fast is None:
        return web.json_response({"route": "off"})
    info: dict = {}
    try:
        from . import ha_morph
    except ImportError:
        import ha_morph
    info["norm"] = ha_morph.normalize(text)
    info["volume_entity"] = await fast._volume_entity(station)
    info["level_from_agent"] = (await fast.volume_source(station)
                                if fast.volume_source else None)
    try:
        answer = await fast.route(text, station=station)
    except Exception as e:  # noqa: BLE001
        return web.json_response({**info, "error": repr(e)}, status=500)
    return web.json_response({**info, "answer": answer, "kind": fast.last_kind})


def make_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(_mqtt_startup)
    app.on_cleanup.append(_mqtt_cleanup)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", health)
    app.router.add_get("/metrics", metrics_view)
    app.router.add_post("/route", route_probe)
    return app


async def main():
    global OUT_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8796)
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    OUT_DIR = args.out
    os.makedirs(OUT_DIR, exist_ok=True)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        lambda: HubDiscovery(args.port), local_addr=("0.0.0.0", DISCOVERY_PORT))
    runner = web.AppRunner(make_app())
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", args.port)
    await site.start()
    log.info("голосовой хаб на :%d", args.port)
    await asyncio.Event().wait()


if __name__ == "__main__":
    # Хаб запускается скриптом, поэтому его модуль называется __main__.
    # Без этой строки `import voice_hub` из fast_route создаёт ВТОРУЮ копию
    # модуля: у неё нет подключённых колонок, station_set_volume возвращает
    # False, и голосовая громкость молча уходит в LLM.
    sys.modules.setdefault("voice_hub", sys.modules[__name__])
    asyncio.run(main())
