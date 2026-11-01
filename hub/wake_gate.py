"""Акустический детектор ключевого слова (microWakeWord) для голосового хаба.

Модуль даёт доступ к модели и её признакам; сам поток ведёт `WakeDetector`
в `voice_loop.py`. Порог решения задаётся НЕ здесь, а клиентом: колонка
присылает его в `hello` (сейчас 0.95), хаб передаёт в `WakeDetector`.

Раньше здесь жил второй, самостоятельный путь (`feed()` с порогом 0.45 и
дампами `wakes/rejects`). Он не использовался живым трафиком, но его порог
попадал в документацию и путал: казалось, что детектор работает на 0.45,
хотя фактически решение шло по 0.95. Путь удалён, осталась только модель
и офлайн-проверка `check()` для замеров на записях.

Рантайм — `pymicro_wakeword` в отдельном venv (KWS_VENV), чтобы не трогать
системный питон: системный процесс хаба добавляет эту папку в sys.path сам.

Переменные окружения:
  KWS_ENABLED     = 1|0            — включён ли детектор (по умолчанию включён)
  KWS_MODEL_JSON  = путь к .json   — манифест модели (рядом лежит .tflite)
  KWS_VENV        = путь к venv    — где стоит pymicro_wakeword
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("wake-gate")

SAMPLE_RATE = 16000
FRAME_BYTES = 320          # 10 мс, 16 кГц, s16le

_VENV = os.environ.get("KWS_VENV", "/opt/hermes-voice/.venv-wake")
_MODEL_JSON = os.environ.get("KWS_MODEL_JSON", "/srv/kws/models/wakeword_e90e6e54.json")
_ENABLED = os.environ.get("KWS_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")

_lock = threading.Lock()
_model = None                 # MicroWakeWord
_features = None              # MicroWakeWordFeatures
_loaded = False
_reason = "не инициализирован"


def _add_venv_to_path() -> None:
    """Подключает site-packages отдельного venv к системному интерпретатору."""
    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = Path(_VENV) / "lib" / ver / "site-packages"
    if site.is_dir() and str(site) not in sys.path:
        sys.path.insert(0, str(site))


def load() -> bool:
    """Загружает модель один раз. Возвращает True, если детектор работоспособен."""
    global _model, _features, _loaded, _reason
    with _lock:
        if _loaded:
            return _model is not None
        _loaded = True
        if not _ENABLED:
            _reason = "выключен через KWS_ENABLED"
            log.info("детектор ключевого слова выключен настройкой")
            return False
        manifest = Path(_MODEL_JSON)
        if not manifest.is_file():
            _reason = f"нет манифеста модели: {manifest}"
            log.warning("детектор ключевого слова недоступен: %s", _reason)
            return False
        try:
            _add_venv_to_path()
            from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            _reason = f"нет pymicro_wakeword ({e})"
            log.warning("детектор ключевого слова недоступен: %s", _reason)
            return False
        try:
            _model = MicroWakeWord.from_config(manifest)
            _features = MicroWakeWordFeatures()
            cutoff, window = _load_thresholds()
            log.info("модель ключевого слова: %s (в манифесте порог %.2f, окно %d)",
                     manifest.name, cutoff, window)
        except Exception as e:  # noqa: BLE001
            _model = _features = None
            _reason = f"модель не загрузилась ({e})"
            log.warning("детектор ключевого слова недоступен: %s", _reason)
            return False
        _reason = "готов"
        return True


def available() -> bool:
    return _model is not None and _features is not None


def status() -> dict:
    return {
        "enabled": _ENABLED,
        "available": available(),
        "reason": _reason,
        "model": _MODEL_JSON,
    }


def _load_thresholds() -> tuple[float, int]:
    """cutoff и размер скользящего окна из манифеста модели.

    Это значения, на которых модель обучалась и калибровалась; живой порог
    приходит от клиента в `hello` и может быть ниже — решение принимает хаб.
    """
    try:
        data = json.loads(Path(_MODEL_JSON).read_text(encoding="utf-8"))
        micro = data.get("micro") or {}
        return float(micro.get("probability_cutoff", 0.95)), int(micro.get("sliding_window_size", 5))
    except Exception:  # noqa: BLE001
        return 0.95, 5


_gate_fired = False           # липкий флаг: слово найдено в текущей реплике
_carry = bytearray()          # неполный кадр между вызовами feed()
_peak = 0.0                   # лучший балл уверенности в текущей реплике


def reset() -> None:
    """Обнуляет состояние детектора перед новой репликой."""
    global _gate_fired, _carry, _peak
    with _lock:
        _gate_fired = False
        _carry = bytearray()
        _peak = 0.0
        if _model is not None:
            _model.reset()
        if _features is not None:
            _features.reset()


def last_score() -> float:
    """Лучший балл уверенности в текущей реплике (для журнала и решения)."""
    return _peak


def feed(pcm: bytes, cutoff: float | None = None) -> bool:
    """Кормит детектор потоком (куски любой длины, 16 кГц s16le).

    Возвращает True, как только слово найдено, и держит True до reset().
    """
    global _gate_fired, _peak
    if not available():
        return False
    if cutoff is None:
        cutoff = _load_thresholds()[0]
    with _lock:
        if _gate_fired:
            return True
        _carry.extend(pcm)
        model, features = _model, _features
        if model is None or features is None:
            return False
        fired = False
        offset = 0
        total = len(_carry)
        while offset + FRAME_BYTES <= total:
            frame = bytes(_carry[offset:offset + FRAME_BYTES])
            offset += FRAME_BYTES
            for feats in features.process_streaming(frame):
                prob = model.process_streaming_prob(feats)
                if prob is None:
                    continue
                if prob > _peak:
                    _peak = prob
                if prob > cutoff:
                    fired = True
        if offset:
            del _carry[:offset]
        if fired:
            _gate_fired = True
        return _gate_fired


def fired() -> bool:
    return _gate_fired


def check(pcm: bytes, cutoff: float | None = None) -> dict:
    """Прогоняет реплику через модель для замеров. Состояние не сохраняет.

    `cutoff` — порог для подсчёта срабатываний; по умолчанию берётся из
    манифеста. Возвращает пик уверенности — по нему видно, насколько слово
    не дотянуло до рабочего порога.
    """
    out = {"fired": False, "peak": 0.0, "hits": 0, "windows": 0,
           "seconds": len(pcm) / 2 / SAMPLE_RATE, "wall_ms": 0.0, "cutoff": None}
    if not available():
        return out
    model, features = _model, _features
    if model is None or features is None:      # страховка для типизации
        return out
    if cutoff is None:
        cutoff = _load_thresholds()[0]
    out["cutoff"] = cutoff
    t0 = time.perf_counter()
    with _lock:
        model.reset()
        features.reset()
        hits = 0
        windows = 0
        peak = 0.0
        for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
            for feats in features.process_streaming(pcm[i:i + FRAME_BYTES]):
                windows += 1
                prob = model.process_streaming_prob(feats)
                if prob is None:
                    continue
                if prob > peak:
                    peak = prob
                if prob > cutoff:
                    hits += 1
    out.update(fired=hits > 0, peak=round(float(peak), 4), hits=hits, windows=windows,
               wall_ms=round((time.perf_counter() - t0) * 1000, 1))
    return out
