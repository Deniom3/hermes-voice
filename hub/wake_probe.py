"""Замер «почти срабатываний» детектора ключевого слова.

Зачем: порог отсекает всё ниже себя, и мы не знаем, сколько раз слово было
сказано, а детектор не дотянул. Без этих цифр нельзя решить, нужен ли сбор
промахов (near-miss) — можно месяцами собирать шум или, наоборот, не собирать
то, чего почти не бывает.

Как устроено: балл детектора идёт окнами по 10 мс. Мы держим максимум за
скользящее окно 2 с и раскладываем его по полосам. Ничего не пишем на диск и
не трогаем аудио — только счётчики в памяти. Раз в N секунд отдаём строку в
журнал.

Что означает полоса:

  * `0.7-0.9` — «похоже, но не дотянул». Скорее всего фон, но именно здесь
    может оказаться промах, если человек сказал имя далеко от микрофона;
  * `0.9-0.95` — почти порог. Такие окна и есть самые вероятные промахи;
  * `>=0.95` — срабатывание (это уже пишется как активация, здесь только счёт);
  * `<0.5` — фон. Собирать его бессмысленно: это не «чуть-чуть не долетел».
"""
from __future__ import annotations

import time

# Верхняя граница полосы и её подпись. Порядок важен: первая подходящая.
BANDS: tuple[tuple[float, str], ...] = (
    (0.5, "<0.5"),
    (0.7, "0.5-0.7"),
    (0.9, "0.7-0.9"),
    (0.95, "0.9-0.95"),
    (1.01, ">=0.95"),
)


def band_for(score: float) -> str:
    for edge, label in BANDS:
        if score < edge:
            return label
    return BANDS[-1][1]


class ScoreProbe:
    """Счётчики максимумов балла по полосам; отчёт раз в `report_s`."""

    def __init__(self, window_s: float = 2.0, report_s: float = 600.0):
        self.window_s = window_s
        self.report_s = report_s
        self.counts: dict[str, int] = {label: 0 for _, label in BANDS}
        self.windows = 0
        self.peak = 0.0
        self._win_max = 0.0
        # Отсчёт начинаем с первого вызова, а не с конструктора: иначе в тестах
        # с подставленным временем окно и отчёт считаются от реального монотонного.
        self._win_since: float | None = None
        self._since: float | None = None

    def offer(self, score: float, now: float | None = None) -> None:
        """Скормить очередной балл (0..1). Дешёвая операция, вызывается на кадр."""
        now = time.monotonic() if now is None else now
        if self._win_since is None:
            self._win_since = now
        if score > self._win_max:
            self._win_max = score
        if now - self._win_since < self.window_s:
            return
        self.counts[band_for(self._win_max)] += 1
        self.windows += 1
        if self._win_max > self.peak:
            self.peak = round(self._win_max, 3)
        self._win_max = 0.0
        self._win_since = now

    def report(self, now: float | None = None) -> str | None:
        """Строка для журнала, если срок подошёл. Счётчики сбрасываются."""
        now = time.monotonic() if now is None else now
        if self._since is None:
            self._since = now
        if now - self._since < self.report_s:
            return None
        self._since = now
        if self.windows == 0:
            text = "детектор: аудио не приходило"
        else:
            parts = ", ".join(f"{label}={n}" for label, n in
                              ((label, self.counts[label]) for _, label in BANDS))
            text = (f"детектор за {int(self.report_s // 60)} мин: {parts}"
                    f", пик {self.peak:.3f}")
        self.counts = {label: 0 for _, label in BANDS}
        self.windows = 0
        self.peak = 0.0
        return text
