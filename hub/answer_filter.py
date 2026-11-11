"""Отсев служебных сообщений агента перед озвучкой.

При обрыве ответа (кнопка, «стоп», перебивание) шлюз иногда отдаёт не ответ,
а собственный текст ошибки — например «Operation interrupted: retrying API
call after error (retry 1/3)». Такое нельзя произносить голосом: человек
слышит бессмыслицу вместо тишины или короткого «не получилось».

Фильтр нарочно узкий: только явные служебные формулировки, чтобы не съесть
нормальный ответ, в котором просто встретилось слово «ошибка».
"""
from __future__ import annotations

# Явные маркеры служебного текста. Сравнение — по нижнему регистру.
MARKERS = (
    "operation interrupted",
    "retrying api call",
    "api call after error",
    "retry 1/3",
    "retry 2/3",
    "retry 3/3",
    "rate limit exceeded",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "connection reset by peer",
    "traceback (most recent call last)",
    "resource_exhausted",
    "context_length_exceeded",
    "model not found",
    "no response from model",
    "empty response from model",
)

# Ответ целиком из таких кусков не озвучиваем вообще.
MAX_LEN = 300


def is_technical(text: str) -> bool:
    """True, если это служебное сообщение, а не ответ человеку."""
    if not text:
        return False
    low = text.strip().lower()
    if not low:
        return False
    hits = sum(1 for m in MARKERS if m in low)
    if hits:
        return True
    # Голый JSON/трейсбек или строка вида «Error: ...» — тоже не для ушей.
    if low.startswith(("error:", "exception:", "traceback", "{\"")):
        return len(text) <= MAX_LEN
    return False


def clean(text: str, fallback: str = "") -> str:
    """Возвращает текст для озвучки либо fallback, если он служебный."""
    return fallback if is_technical(text) else text
