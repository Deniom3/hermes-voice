"""Метрики голосового контура в формате Prometheus.

Без внешних зависимостей: счётчики, датчики и гистограммы в памяти, отдача
текстом на `/metrics`. Этого достаточно Prometheus и Grafana.

Правила:
- имена с префиксом `hermes_voice_`, метки — только устойчивые значения
  (станция, маршрут, стадия), без текста фраз и id;
- гистограммы фиксированных бакетов, чтобы запросы были предсказуемыми;
- рендер дешёвый: Prometheus скрейпит раз в 5–15 с.
"""
from __future__ import annotations

import os
import threading

PREFIX = "hermes_voice_"

# Границы бакетов: вероятности детектора, секунды, символы.
SCORE_BUCKETS = (0.5, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0)
SECONDS_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 60)
CHARS_BUCKETS = (40, 80, 160, 320, 640, 1280, 2560)

_HELP = {
    "up": "Хаб отвечает на скрейп",
    "station_online": "Колонка подключена к хабу",
    "station_audio_bytes_total": "Получено аудио от колонки, байт",
    "station_last_audio_timestamp_seconds": "Время последнего аудио от колонки, unix",
    "wake_activations_total": "Срабатываний слова активации",
    "wake_misses_total": "Промахов детектора (пик ниже порога)",
    "wake_no_name_total": "Срабатываний без обращения в тексте",
    "wake_arbitration_lost_total": "Активаций, уступленных другой колонке",
    "wake_score": "Пик вероятности детектора",
    "asr_seconds": "Время распознавания",
    "asr_audio_seconds": "Длина аудио на распознавании",
    "asr_empty_total": "Пустой текст от ASR",
    "asr_errors_total": "Ошибки ASR",
    "phrases_total": "Фразы по маршрутам",
    "intents_total": "Срабатывания фраз-триггеров",
    "answer_seconds": "От фразы до первого звука ответа",
    "answer_chars": "Длина ответа в символах",
    "llm_seconds": "Время ответа агента",
    "tts_seconds": "Время синтеза",
    "tts_chunks_total": "Кусков синтеза",
    "dataset_files": "Файлов в наборе обучения",
    "dataset_bytes": "Размер набора обучения, байт",
    "dataset_new_total": "Добавлено файлов в набор",
    "errors_total": "Ошибки по стадиям",
    "mqtt_connected": "MQTT-мост подключён",
}

COUNTERS = {
    "station_audio_bytes_total", "wake_activations_total", "wake_misses_total",
    "wake_no_name_total", "wake_arbitration_lost_total", "asr_empty_total",
    "asr_errors_total", "phrases_total", "intents_total", "tts_chunks_total",
    "dataset_new_total", "errors_total",
}
HISTOGRAMS = {
    "wake_score": SCORE_BUCKETS,
    "asr_seconds": SECONDS_BUCKETS,
    "asr_audio_seconds": SECONDS_BUCKETS,
    "answer_seconds": SECONDS_BUCKETS,
    "llm_seconds": SECONDS_BUCKETS,
    "tts_seconds": SECONDS_BUCKETS,
    "answer_chars": CHARS_BUCKETS,
}


def _labels_key(labels: dict | None) -> tuple:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _render_labels(key: tuple, extra: tuple | None = None) -> str:
    # le ставим первым: так строка читается как в документации Prometheus.
    items = list(extra or ()) + list(key)
    if not items:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in items) + "}"


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[str, tuple], float] = {}
        self._hists: dict[tuple[str, tuple], list] = {}

    # ---------- запись ----------
    def inc(self, name: str, labels: dict | None = None, value: float = 1.0) -> None:
        key = (name, _labels_key(labels))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def set(self, name: str, value: float, labels: dict | None = None) -> None:
        key = (name, _labels_key(labels))
        with self._lock:
            self._values[key] = float(value)

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        buckets = HISTOGRAMS.get(name)
        if buckets is None:
            raise KeyError(f"неизвестная гистограмма: {name}")
        key = (name, _labels_key(labels))
        with self._lock:
            h = self._hists.get(key)
            if h is None:
                h = self._hists[key] = [0] * len(buckets) + [0.0, 0]
            v = float(value)
            for i, edge in enumerate(buckets):
                if v <= edge:
                    h[i] += 1
            h[-2] += v
            h[-1] += 1

    def get(self, name: str, labels: dict | None = None) -> float:
        with self._lock:
            return self._values.get((name, _labels_key(labels)), 0.0)

    # ---------- чтение ----------
    def render(self) -> str:
        with self._lock:
            values = dict(self._values)
            hists = {k: list(v) for k, v in self._hists.items()}
        lines: list[str] = []
        seen_types: set[str] = set()
        for (name, key), value in sorted(values.items()):
            if name not in seen_types:
                seen_types.add(name)
                lines.append(f"# HELP {PREFIX}{name} {_HELP.get(name, name)}")
                kind = "counter" if name in COUNTERS else "gauge"
                lines.append(f"# TYPE {PREFIX}{name} {kind}")
            lines.append(f"{PREFIX}{name}{_render_labels(key)} {_fmt(value)}")
        for (name, key), h in sorted(hists.items()):
            buckets = HISTOGRAMS[name]
            if name not in seen_types:
                seen_types.add(name)
                lines.append(f"# HELP {PREFIX}{name} {_HELP.get(name, name)}")
                lines.append(f"# TYPE {PREFIX}{name} histogram")
            for i, edge in enumerate(buckets):
                lines.append(f"{PREFIX}{name}_bucket"
                             f"{_render_labels(key, (('le', _fmt(edge)),))} {h[i]}")
            lines.append(f"{PREFIX}{name}_bucket"
                         f"{_render_labels(key, (('le', '+Inf'),))} {h[-1]}")
            lines.append(f"{PREFIX}{name}_sum{_render_labels(key)} {_fmt(h[-2])}")
            lines.append(f"{PREFIX}{name}_count{_render_labels(key)} {h[-1]}")
        return "\n".join(lines) + ("\n" if lines else "")

    def reset(self) -> None:
        with self._lock:
            self._values.clear()
            self._hists.clear()


def _fmt(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


METRICS = Registry()


def render() -> str:
    METRICS.set("up", 1)
    return METRICS.render()



def scan_dataset(root: str) -> None:
    """Обновляет размеры набора обучения: файлы и байты по видам."""
    for kind in ("positives", "negatives", "misses"):
        files = 0
        size = 0
        path = os.path.join(root, kind)
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        if not entry.is_file():
                            continue
                        size += entry.stat().st_size
                        if entry.name.endswith(".wav"):
                            files += 1
                    except OSError:
                        continue
        except FileNotFoundError:
            pass
        METRICS.set("dataset_files", files, {"kind": kind})
        METRICS.set("dataset_bytes", size, {"kind": kind})
