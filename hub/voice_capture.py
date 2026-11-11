#!/usr/bin/env python3
"""Сбор событий ключевого слова для дообучения + ретро-проверка.

Идея: непрерывный поток НЕ пишется на диск. В памяти держится кольцо
последних секунд, а на диск попадают только окна вокруг событий:

  * детектор сработал и имя подтвердилось распознаванием  -> positives;
  * детектор сработал, имени в тексте нет                 -> negatives;
  * сработал, текст пустой/обрыв                          -> unclear;
  * пик был, но порог не превышен (промах)                -> misses.

Ретро-проверка: по снимку кольца прогоняется та же модель с пониженным
порогом. Так видно повторы «Окей, Гермес», которые основной порог не поймал:
человек повторил два-три раза, а колонка молчала.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import wave
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)

SR = 16000
FRAME = 320
TARGET_RMS = 2300.0
PEAK_MAX = 30000



def read_model_json() -> str:
    return os.environ.get("KWS_MODEL_JSON", "/srv/kws/models/wakeword_e90e6e54.json")


class Ring:
    """Кольцевой буфер сырого PCM: последние N секунд, без записи на диск."""

    def __init__(self, seconds: float = 8.0, sample_rate: int = SR):
        self.max_bytes = int(seconds * sample_rate * 2)
        self._buf = bytearray()

    def add(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._buf += pcm
        extra = len(self._buf) - self.max_bytes
        if extra > 0:
            del self._buf[:extra]

    def last(self, seconds: float) -> bytes:
        n = int(seconds * SR * 2)
        return bytes(self._buf[-n:]) if n < len(self._buf) else bytes(self._buf)

    def __len__(self) -> int:
        return len(self._buf)


class StreamRing:
    """Последний час СЫРОГО потока на диске, кусками по chunk_s.

    Окно события живёт в памяти 8 с и исчезает; если человек скажет «в 14:27
    не сработало», разобрать это нечем. Здесь звук лежит час: файлы
    `<name>-<epoch_ms>.raw.pcm`, старые удаляются при ротации. Миллисекунды в
    имени — чтобы два открытия в одну секунду (рестарт, переподключение) не
    перезаписали друг друга.

    Пишется только сырой поток — им питается детектор, второй (AGC) поток
    для разбора не нужен. 16 кГц s16le = 115 МБ в час на колонку.
    """

    BYTES_PER_S = SR * 2

    def __init__(self, root: str, name: str, keep_s: float = 3600.0,
                 chunk_s: float = 300.0, enabled: bool = True,
                 min_free_mb: int = 1500):
        self.root = root
        self.name = name
        self.keep_s = keep_s
        self.chunk_bytes = max(1, int(chunk_s * self.BYTES_PER_S))
        self.enabled = enabled
        self.min_free = min_free_mb * 1024 * 1024
        self._f = None
        self._written = 0
        self._last_flush = 0.0
        self._last_prune = 0.0
        self._blocked_until = 0.0
        self._warned = 0.0
        if self.enabled:
            try:
                os.makedirs(self.root, exist_ok=True)
            except OSError as e:
                log.warning("кольцо: каталог %s не создан (%s), запись выключена",
                            self.root, e)
                self.enabled = False
            self._prune(time.time())

    def write(self, pcm: bytes) -> None:
        if not self.enabled or not pcm:
            return
        now = time.time()
        if now < self._blocked_until:
            return
        if self._f is None or self._written >= self.chunk_bytes:
            if not self._open(now):
                return
        f = self._f
        if f is None:
            return
        try:
            f.write(pcm)
        except OSError as e:
            log.warning("кольцо: запись не удалась (%s), файл закрыт", e)
            self._close()
            return
        self._written += len(pcm)
        if now - self._last_flush >= 5.0:
            try:
                f.flush()
            except OSError:
                pass
            self._last_flush = now
        if now - self._last_prune >= 60.0:
            self._prune(now)

    def close(self) -> None:
        self._close()

    def _open(self, now: float) -> bool:
        self._close()
        try:
            free = shutil.disk_usage(self.root).free
        except OSError as e:
            log.warning("кольцо: место не проверено (%s)", e)
            free = 0
        if free < self.min_free:
            if now - self._warned > 600:
                log.warning("кольцо: свободно %d МБ, запись приостановлена",
                            free // 1048576)
                self._warned = now
            self._blocked_until = now + 60
            return False
        stamp = int(now * 1000)
        path = os.path.join(self.root, f"{self.name}-{stamp}.raw.pcm")
        while os.path.exists(path):
            # два открытия в одну миллисекунду не должны перезаписать файл
            stamp += 1
            path = os.path.join(self.root, f"{self.name}-{stamp}.raw.pcm")
        try:
            self._f = open(path, "wb")
        except OSError as e:
            log.warning("кольцо: файл не открылся (%s)", e)
            self._blocked_until = now + 60
            return False
        self._written = 0
        self._last_flush = now
        self._prune(now)
        return True

    def _close(self) -> None:
        if self._f is None:
            return
        try:
            self._f.flush()
            self._f.close()
        except OSError:
            pass
        self._f = None

    def _prune(self, now: float) -> None:
        self._last_prune = now
        cutoff = now - self.keep_s
        prefix = f"{self.name}-"
        try:
            names = os.listdir(self.root)
        except OSError:
            return
        for fn in names:
            if not (fn.startswith(prefix) and fn.endswith(".raw.pcm")):
                continue
            try:
                started = int(fn[len(prefix):-len(".raw.pcm")]) / 1000.0
            except ValueError:
                continue
            if started < cutoff or started > now + 300:
                # Старше срока — обычная ротация. Метка из будущего (> 5 мин) —
                # след скачка часов: NTP вернул время назад, и такой файл иначе
                # не удаляется никогда, потому что «моложе» окна хранения.
                try:
                    os.unlink(os.path.join(self.root, fn))
                except OSError:
                    pass


class RetroScanner:
    """Второй проход той же моделью по снимку кольца с низким порогом."""

    def __init__(self, model_json: str | None = None):
        self._ok = False
        try:
            from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures
            self.model = MicroWakeWord.from_config(model_json or read_model_json())
            self.feats = MicroWakeWordFeatures()
            self._ok = True
        except Exception:  # noqa: BLE001
            self.model = None
            self.feats = None

    @property
    def available(self) -> bool:
        return self._ok

    def scan(self, pcm: bytes, threshold: float = 0.55,
             gap: float = 1.2) -> list[tuple[float, float]]:
        """Пики вероятности в снимке. Возвращает [(секунда от начала, p)]."""
        if not self._ok or not pcm:
            return []
        y = pcm[: len(pcm) - (len(pcm) % FRAME)]
        self.model.reset()
        self.feats.reset()
        t = 0.0
        raw: list[tuple[float, float]] = []
        for i in range(0, len(y), FRAME):
            for f in self.feats.process_streaming(y[i:i + FRAME]):
                p = self.model.process_streaming_prob(f)
                if p is not None and float(p) >= threshold:
                    raw.append((t, float(p)))
            t += FRAME / (SR * 2)
        events: list[tuple[float, float]] = []
        for t, p in raw:
            if events and t - events[-1][0] <= gap:
                if p > events[-1][1]:
                    events[-1] = (t, p)
            else:
                events.append((t, p))
        return events


def _frames(x: np.ndarray) -> np.ndarray:
    n = len(x) // FRAME
    return np.sqrt((x[: n * FRAME].reshape(n, FRAME) ** 2).mean(axis=1)) if n else np.array([0.0])


def active_rms(x: np.ndarray) -> float:
    r = _frames(x.astype(np.float32))
    thr = max(float(np.percentile(r, 20)) * 2.5, 5.0)
    a = r[r > thr]
    return float(np.sqrt((a * a).mean())) if len(a) else 0.0


def normalize(x: np.ndarray) -> np.ndarray | None:
    """Привести активную речь к 2300 без клиппинга. None — если тишина.

    Порог активной речи низкий (8) намеренно: тракт колонки отдаёт речь
    с RMS 8–60, и порог 100 отбраковывал бы все реальные события.
    """
    ra = active_rms(x)
    if ra < 8.0 or float(np.abs(x).max()) < 40:
        return None
    g = TARGET_RMS / ra
    peak = float(np.abs(x).max())
    if peak * g > PEAK_MAX:
        g = PEAK_MAX / max(peak, 1.0)
    if g > 4000.0:
        return None
    y = x.astype(np.float32) * g
    if float(np.abs(y).max()) > 32767:
        return None
    return np.clip(y, -32768, 32767).astype(np.int16)


def crop_around(pcm: bytes, peak_s: float, left: float = 1.4,
                total: float = 2.4) -> np.ndarray:
    x = np.frombuffer(pcm, dtype=np.int16)
    center = int(peak_s * SR)
    a = max(0, center - int(left * SR))
    b = min(len(x), a + int(total * SR))
    if b - a < int(1.2 * SR):
        a = max(0, b - int(total * SR))
    return x[a:b]


def classify(text: str) -> str:
    """Куда положить событие: positives / negatives / unclear.

    Обращение ищем той же функцией, что и ядро (name_like), а не регуляркой:
    «Германия победила» — это негатив, а не обращение, и попадание такого
    клипа в positives портит обучающий набор.
    """
    t = (text or "").strip()
    if not t:
        return "unclear"
    from voice_core import name_like
    if name_like(t):
        return "positives"
    if len(t) < 4:
        return "unclear"
    return "negatives"


@dataclass
class MissWatcher:
    """Ловит промахи: пик был, а порог не превышен.

    Промахи раньше не попадали в набор вообще — писались только события после
    срабатывания, поэтому модель не видела, где именно она почти справилась.
    Здесь считается эпизод: пик в полосе [min_score, high), после пика прошло
    gap_s и звук упал — эпизод закрыт, время пика отдаётся наружу, чтобы
    вырезать окно из кольца.

    Ограничения от шума: не чаще min_gap_s и не больше per_hour в час.

    Полоса 0.65–0.95. Сначала нижняя граница была 0.80, но живой промах 08.11
    дал пик 0.697: фраза «О'кей, Гермес, включи лампу» распозналась полностью,
    а детектор дал 0.697 против 0.995 на повторе — разница 7 дБ по уровню.
    Такой промах не попадал ни в misses, ни в positives, то есть модель не
    видела именно те случаи, ради которых набор и собирается. Шум отсекает не
    порог, а проверка текста распознаванием: без имени клип не пишется.
    """

    min_score: float = 0.65
    high: float = 0.95
    gap_s: float = 1.5
    min_gap_s: float = 4.0
    per_hour: int = 120
    # После настоящей активации глушим промахи: остаточный пик через полсекунды
    # писал тот же клип в misses, и слово попадало в два класса сразу.
    block_s: float = 3.0

    _peak: float = 0.0
    _peak_at: float = 0.0
    _armed: bool = False
    _block_until: float = 0.0
    _last_save: float = 0.0
    _saved: list = None

    def __post_init__(self) -> None:
        if self._saved is None:
            self._saved = []

    def reset(self) -> None:
        """Сбросить эпизод и заглушить промахи: сработала настоящая активация."""
        self._armed = False
        self._peak = 0.0
        self._block_until = time.monotonic() + self.block_s

    def offer(self, score: float) -> tuple[float, float] | None:
        """Скормить вероятность. Возвращает (время пика, пик) закрытого эпизода."""
        now = time.monotonic()
        if now < self._block_until:
            return None
        if score >= self.high:
            self.reset()
            return None
        if score >= self.min_score:
            if score > self._peak:
                self._peak = float(score)
                self._peak_at = now
            self._armed = True
            return None
        if not self._armed or now - self._peak_at < self.gap_s:
            return None
        peak_at, peak = self._peak_at, self._peak
        self._armed = False
        self._peak = 0.0
        if now - self._last_save < self.min_gap_s:
            return None
        self._saved = [t for t in self._saved if now - t < 3600]
        if len(self._saved) >= self.per_hour:
            return None
        self._last_save = now
        self._saved.append(now)
        return peak_at, peak

    @property
    def peak(self) -> float:
        return self._peak


@dataclass
class Collector:
    """Пишет окна событий и sidecar. Поток не сохраняет."""

    root: str = "/srv/kws/train"
    enabled: bool = True

    def __post_init__(self) -> None:
        for sub in ("positives", "negatives", "unclear", "misses"):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)

    def save(self, cls: str, pcm: bytes, peak_s: float,
             meta: dict) -> str | None:
        if not self.enabled or not pcm:
            return None
        x = crop_around(pcm, peak_s)
        if len(x) < int(1.2 * SR):
            return None
        y = normalize(x)
        if y is None:
            return None
        digest = hashlib.md5(y.tobytes()).hexdigest()
        ts = time.strftime("%Y%m%d-%H%M%S")
        base = f"{ts}-{digest[:8]}"
        d = os.path.join(self.root, cls)
        # дедуп: одинаковый клип уже мог быть сохранён в этом классе
        import glob as _glob
        if _glob.glob(os.path.join(d, f"*-{digest[:8]}.wav")):
            return None
        wav_path = os.path.join(d, base + ".wav")
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(y.tobytes())
        meta = dict(meta)
        meta.update({"class": cls, "sec": round(len(y) / SR, 2),
                     "rms": round(active_rms(y.astype(np.float32))),
                     "peak": int(np.abs(y).max()), "md5": digest})
        with open(os.path.join(d, base + ".json"), "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        return wav_path
