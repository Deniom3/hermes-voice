#!/usr/bin/env python3
"""Детектор активации + VAD + ASR: из потока PCM в текст запроса.

Используется и в живом сервисе, и офлайн для проверки на записях.
Запускать интерпретатором, где есть pymicro_wakeword: /opt/hermes-voice/.venv-wake/bin/python
"""
import io
import json
import logging
import os
import sys
import time
import urllib.request
import uuid
import wave

import numpy as np

sys.path.insert(0, '/opt/hermes-voice')
os.environ.setdefault('KWS_MODEL_JSON', '/srv/kws/models/wakeword_e90e6e54.json')


class AGC:
    """Тот же AGC, что и в сервисе: единая шкала уровней.

    ЗАФИКСИРОВАНО: target=1000, max_gain_db=36, smooth=0.25 подобраны под
    тракт колонки (сырой rms 20 → после AGC 748 при нулевом клиппинге).
    Параметры не перекручивать под каждую неудачную фразу — лечить моделью.
    """

    def __init__(self, target=1000.0, max_gain_db=36.0, smooth=0.25):
        self.target = target
        self.max_gain = 10 ** (max_gain_db / 20)
        self.gain = 1.0
        self.smooth = smooth

    def process(self, data: bytes) -> bytes:
        x = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        if x.size == 0:
            return data
        rms = float(np.sqrt(np.mean(x * x)))
        want = self.gain
        if rms > 1.0:
            want = max(1.0, min(self.target / rms, self.max_gain))
        self.gain = self.gain * (1 - self.smooth) + want * self.smooth
        y = np.clip(x * self.gain, -32768, 32767).astype(np.int16)
        return y.tobytes()


class WakeDetector:
    """Потоковый детектор слова активации: подаём чанки, получаем события."""

    FRAME = 320  # 10 мс при 16 кГц s16le

    def __init__(self, threshold=0.95, refractory=1.5):
        import wake_gate
        wake_gate.load()
        self._model = wake_gate._model
        self._features = wake_gate._features
        self._model.reset()
        self._features.reset()
        self.threshold = threshold
        self.refractory = refractory
        self._last_hit = -1e9
        self._t = 0.0
        self._tail = b""
        # Максимум балла с прошлого take_max(): нужен замеру «почти срабатываний».
        # Считаем всегда, а не только при p >= threshold, иначе промахи не видно.
        self.max_prob = 0.0

    def feed(self, chunk: bytes):
        """Возвращает список моментов срабатывания (секунды от начала потока)."""
        buf = self._tail + chunk
        hits = []
        n = len(buf) - (len(buf) % self.FRAME)
        for i in range(0, n, self.FRAME):
            frame = buf[i:i + self.FRAME]
            for feats in self._features.process_streaming(frame):
                p = self._model.process_streaming_prob(feats)
                if p is not None and p > self.max_prob:
                    self.max_prob = float(p)
                if p is not None and p >= self.threshold:
                    if self._t - self._last_hit >= self.refractory:
                        hits.append((round(self._t, 2), round(float(p), 3)))
                        self._last_hit = self._t
            self._t += self.FRAME / 32000
        self._tail = buf[n:]
        return hits

    def reset(self):
        self._model.reset()
        self._features.reset()
        self._tail = b""

    def take_max(self) -> float:
        """Вернуть максимум балла с прошлого вызова и обнулить его.

        Отдельный метод, а не поле: потребителю нужен именно интервал между
        вызовами, иначе накопленный исторический максимум ничего не говорит.
        """
        p = self.max_prob
        self.max_prob = 0.0
        return p


def _speech_ratio(x: np.ndarray, sr: int = 16000) -> float:
    """Доля кадров 20 мс, похожих на речь, а не на щелчок или шум.

    Замер на записи колонки (01.11.2026): у речи энергия ниже 1 кГц — 0.95–0.97
    при ZCR 0.07, у стука клавиатуры — 0.07–0.08 при ZCR 0.29–0.30. AGC раздувает
    уровень стука до «речевого» по RMS, поэтому порог по громкости его не ловит,
    а по спектру разделение чистое.
    """
    frame = 320                     # 20 мс
    frames = [x[i:i + frame].astype(np.float64)
              for i in range(0, len(x) - frame + 1, frame)]
    if not frames:
        return 0.0
    # Порог тишины считаем от самого сигнала, а не абсолютным числом: сырой
    # поток колонки имеет rms 1.5–14 даже на речи, и жёсткое «< 30» глушило
    # все кадры — фраза после ключа не собиралась вообще.
    rms_all = np.array([float(np.sqrt((f ** 2).mean())) for f in frames])
    # Пол считаем только при контрасте: если окно целиком из речи (человек
    # говорит без пауз), percentile(20) равен самой речи и порог floor*1.5
    # отбрасывает все кадры — признак речи обнуляется на ровном сигнале.
    floor = max(float(np.percentile(rms_all, 20)), 1.0)
    total = good = 0
    for f, r in zip(frames, rms_all):
        total += 1
        if r < floor * 1.5:                     # около шумового пола — тишина
            continue
        spec = np.abs(np.fft.rfft(f * np.hanning(frame)))
        freqs = np.fft.rfftfreq(frame, 1 / sr)
        energy = (spec ** 2).sum()
        if not energy:
            continue
        low = (spec[freqs < 1000] ** 2).sum() / energy
        zcr = float(((f[:-1] * f[1:]) < 0).mean())
        if low >= 0.5 and zcr <= 0.22:
            good += 1
    return good / total if total else 0.0


class PhraseCollector:
    """Собирает фразу после активации.

    Ключевое отличие от наивной схемы: пауза между «Окей, Гермес» и самой фразой —
    это НОРМА, а не конец реплики. Поэтому сначала ждём начало речи (до wait_voice_s),
    и только потом отсчитываем тишину конца фразы. Короткий обрывок (< min_voice_ms)
    фразой не считается — продолжаем ждать.
    """

    def __init__(self, sr=16000, silence_ms=700, max_s=15.0, min_rms=250,
                 skip_ms=900, wait_voice_s=6.0, min_voice_ms=350,
                 end_detector=None, hard_silence_ms=2000):
        self.sr = sr
        self.silence = int(silence_ms * sr / 1000)
        self.max_len = int(max_s * sr)
        self.min_rms = min_rms
        self.skip = int(skip_ms * sr / 1000)      # хвост слова активации
        self.wait_voice = int(wait_voice_s * sr)  # сколько ждать начала фразы
        self.min_voice = int(min_voice_ms * sr / 1000)
        # Семантический конец реплики: вызывается на паузе, возвращает True,
        # если человек договорил. None — обычный порог тишины.
        self.end_detector = end_detector
        self.hard_silence = int(hard_silence_ms * sr / 1000)
        self._last_end_check = 0
        self.buf = np.zeros(0, dtype=np.int16)
        # Окно анализа для признака речи: чанк может быть всего 32 мс,
        # а статистика по 20-мс кадрам на нём недостоверна.
        self._src_buf = np.zeros(0, dtype=np.int16)
        # история сырой громкости: по ней отделяем речь от ровного фона
        self._src_hist: list[float] = []
        self.silent_run = 0
        self.voice_run = 0
        self.voiced = False
        self.seen = 0

    def feed(self, chunk: bytes, analysis: bytes | None = None):
        """Возвращает готовый PCM, когда фраза закончилась, иначе None.
        Строка 'cancel' — за wait_voice никто не заговорил.

        chunk — поток после AGC (из него собирается фраза для ASR),
        analysis — сырой поток: по нему решаем «речь или щелчок», потому что
        AGC раздувает стук клавиатуры до низкочастотного сигнала и признак
        по нему не работает.
        """
        x = np.frombuffer(chunk, dtype=np.int16)
        src = np.frombuffer(analysis, dtype=np.int16) if analysis else x
        if x.size == 0:
            return None
        self.seen += x.size
        if self.skip > 0:
            drop = min(self.skip, x.size)
            self.skip -= drop
            x = x[drop:]
            if x.size == 0:
                return None

        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        src_rms = float(np.sqrt(np.mean(src.astype(np.float64) ** 2)))
        # «голос» = спектр речи, а не громкость: AGC делает стук клавиатуры
        # громким, но низких частот у него почти нет (проверено по записи).
        # Признак считаем по окну 0.2 с: на одиночном 32-мс чанке статистики нет.
        self._src_buf = np.concatenate([self._src_buf, src])[-3200:]
        ratio = _speech_ratio(self._src_buf) if self._src_buf.size >= 1600 else 0.0
        # Второй, независимый признак — сырая громкость. AGC выравнивает
        # уровень, поэтому по нему речь и фон неразличимы, а сырой поток
        # колонки даёт на речи 10–12 против 3–4 в фоне. Порог ведём от
        # шумового пола: вторая колонка может быть громче или тише.
        self._src_hist.append(src_rms)
        if len(self._src_hist) > 200:
            del self._src_hist[:-200]
        # Пока истории мало, перцентиль нестабилен: он считается по самой же
        # речи и порог floor*1.7 уходит выше неё (речь перестаёт детектироваться
        # через полсекунды после начала). До 20 значений пол считаем нулевым.
        floor = (float(np.percentile(self._src_hist, 10))
                 if len(self._src_hist) >= 20 else 0.0)
        speech = (src_rms >= max(5.0, floor * 1.7)
                  and ratio >= 0.08
                  and rms >= self.min_rms * 0.5)

        if not self.voiced:
            # ждём начало фразы
            if speech:
                self.voiced = True
                self.voice_run = x.size
                self.silent_run = 0
                self.buf = np.concatenate([self.buf, x])
            elif self.seen >= self.wait_voice:
                # никто не заговорил — молча ждём следующего обращения
                self.buf = np.zeros(0, dtype=np.int16)
                return 'cancel'
            return None

        # собираем фразу
        self.buf = np.concatenate([self.buf, x])
        if speech:
            self.voice_run += x.size
            self.silent_run = 0
        else:
            self.silent_run += x.size

        if self.silent_run >= self.silence:
            if self.voice_run >= self.min_voice:
                # Пауза достигла порога, но решает модель: если это пауза
                # внутри фразы — продолжаем сбор. Проверяем не чаще 4 раз/с.
                if (self.end_detector is not None
                        and self.silent_run < self.hard_silence
                        and self.seen - self._last_end_check >= int(0.25 * self.sr)):
                    self._last_end_check = self.seen
                    try:
                        complete = bool(self.end_detector(self.buf))
                    except Exception:      # noqa: BLE001 — модель не должна вешать сбор
                        complete = True
                    if not complete:
                        return None
                data = self.buf[: len(self.buf) - self.silent_run + int(0.15 * self.sr)]
                self.buf = np.zeros(0, dtype=np.int16)
                self.voiced = False
                self.silent_run = 0
                self.voice_run = 0
                self._last_end_check = self.seen
                return data
            # обрывок короче min_voice — считаем, что фраза ещё не началась
            self.voiced = False
            self.silent_run = 0
            self.voice_run = 0
            self.buf = np.zeros(0, dtype=np.int16)
            return None

        if len(self.buf) > self.max_len:
            data = self.buf[: self.max_len]
            self.buf = np.zeros(0, dtype=np.int16)
            self.voiced = False
            self.silent_run = 0
            self.voice_run = 0
            return data
        return None

    def flush(self):
        if self.voiced and self.voice_run >= self.min_voice:
            data = self.buf
            self.buf = np.zeros(0, dtype=np.int16)
            self.voiced = False
            return data
        self.buf = np.zeros(0, dtype=np.int16)
        return None


def pcm_to_wav(pcm: np.ndarray, sr=16000) -> bytes:
    bio = io.BytesIO()
    with wave.open(bio, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return bio.getvalue()


ASR_URL = os.environ.get(
    "ASR_URL", "http://127.0.0.1:8000/v1/audio/transcriptions")  # единый шлюз (ASR и TTS)

log = logging.getLogger(__name__)


def normalize_for_asr(pcm: np.ndarray) -> np.ndarray:
    """Подтянуть громкость перед распознаванием.

    Колонка отдаёт очень тихий поток (пик 100–400 из 32767), и на нём ASR
    путает имя: «Гермес» превращается в «капец»/«Кербец». Выравнивание пика
    до 26000 возвращает нормальное распознавание; громкие записи не трогаем,
    чтобы не вносить клиппинг.
    """
    if pcm.size == 0:
        return pcm
    peak = int(np.abs(pcm.astype(np.int32)).max())
    # 20 — ниже этого только шум квантования; речь колонки даёт пик 80–150
    if peak < 20 or peak >= 26000:
        return pcm
    gain = 26000.0 / peak
    return np.clip(pcm.astype(np.float64) * gain, -32768, 32767).astype(np.int16)


def transcribe(pcm: np.ndarray, url: str | None = None,
               model='gigaam-asr', timeout=60) -> str:
    """POST multipart через stdlib в единый шлюз :8000.

    Второго адреса нет намеренно. Раньше при 502 шлюза запрос молча уходил
    напрямую в audio.cpp на :8010: одна и та же фраза распознавалась по-разному
    в зависимости от того, сработал обход или нет, а сам обход нигде не
    логировался. Ошибка шлюза должна быть видна, а не заглушаться вторым путём.
    """
    target = url or ASR_URL
    pcm = normalize_for_asr(pcm)
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            return _post(target, pcm, model, timeout)
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("ASR: шлюз %s не ответил (%s), попытка %d из 2", target, e, attempt)
            time.sleep(0.3)
    raise last if last else RuntimeError('ASR недоступен')


def _post(url: str, pcm: np.ndarray, model: str, timeout: int) -> str:
    wav = pcm_to_wav(pcm)
    boundary = '----hermes' + uuid.uuid4().hex
    body = b''
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'.encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="phrase.wav"\r\n'
             f'Content-Type: audio/wav\r\n\r\n').encode()
    body += wav + b'\r\n'
    body += f'--{boundary}--\r\n'.encode()
    req = urllib.request.Request(
        url, data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return (json.loads(r.read().decode()).get('text') or '').strip()


def run_file(path, threshold=0.95, verbose=True):
    """Офлайн: прогон файла через детектор+сборщик+ASR."""
    with wave.open(path) as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    det = WakeDetector(threshold=threshold)
    coll = PhraseCollector(sr=sr)
    results = []
    step = 1600  # 100 мс
    waiting = False
    agc = AGC()

    for i in range(0, len(pcm), step):
        raw = pcm[i:i + step].tobytes()
        chunk = agc.process(raw)
        hits = det.feed(chunk)
        for t, p in hits:
            if verbose:
                print(f'  активация {t:7.2f} с  peak={p}')
            if not waiting:
                waiting = True
                coll = PhraseCollector(sr=sr)

        if waiting:
            phrase = coll.feed(chunk)
            if isinstance(phrase, str) and phrase == 'cancel':
                if verbose:
                    print('    (тишина после активации — отмена)')
                waiting = False
                continue
            if phrase is not None and len(phrase) > sr // 4:
                text = transcribe(phrase)
                dur = len(phrase) / sr
                if verbose:
                    print(f'    фраза {dur:.1f} с → {text!r}')
                results.append((t if hits else i / sr, dur, text))
                waiting = False

    return results


if __name__ == '__main__':
    for f in sys.argv[1:]:
        print(f'=== {f}')
        run_file(f)
