"""Автомат источников звука на колонке.

Три источника, у каждого своё состояние:

  alice — нативный плеер Алисы (`maind`/`mediad`). Мы им не владеем,
          но можем поставить на паузу и продолжить.
  ma    — очередь Music Assistant для этой станции.
  agent — наш Sendspin/aplay: транспорт потока MA на колонке.

`agent` не самостоятельный источник: он играет то, что отдаёт MA. Поэтому
владелец (`owner`) — всегда `alice` или `ma`, а `agent` следует за MA.

Главное правило: **ровно один владелец**. Второй источник гасится один раз
на переходе состояния (edge), а не каждые полсекунды по таймеру. Все прошлые
баги — цикл глушений, «залип автомата», возврат не той музыки — росли из
флагов, которые жили отдельно от фактов.

Модуль чистый: `decide()` не делает I/O, только считает действия. Исполняет
их `voice_hub`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

ALICE = "alice"
MA = "ma"
AGENT = "agent"

IDLE = "idle"
PLAYING = "playing"
PAUSED = "paused"
STREAMING = "streaming"

# Причины паузы. Возвращаем только то, что поставили сами.
REASON_HUB = "hub"        # пауза на время нашего хода/ответа
REASON_BUTTON = "button"  # пользователь нажал кнопку
REASON_OVERLAP = "overlap"  # второй источник заиграл сам
REASON_APP = "app"        # пауза поставлена из приложения/с ПК

# Действия. Строки, чтобы их было видно в логах и тестах.
PAUSE_ALICE = "pause_alice"
PLAY_ALICE = "play_alice"
PAUSE_MA = "pause_ma"
PLAY_MA = "play_ma"
PAUSE_AGENT = "pause_agent"
# Пауза только нашего aplay: PAUSE_AGENT гасит и натив, а при alice_started
# нужен обратный случай — убрать музыку, не тронув Алису.
PAUSE_SPIN = "pause_spin"
PLAY_AGENT = "play_agent"
STOP_AGENT = "stop_agent"


@dataclass
class Sources:
    """Снимок трёх источников. Единственный источник правды."""

    alice: str = IDLE
    ma: str = IDLE
    agent: str = IDLE
    owner: str = "none"
    pause_reason: str = "none"
    # Есть ли что продолжать в MA: filled / empty / unknown.
    ma_queue: str = "unknown"

    def copy(self) -> "Sources":
        return replace(self)


@dataclass
class Decision:
    actions: list[str] = field(default_factory=list)
    sources: Sources = field(default_factory=Sources)


def _owner_of(src: Sources) -> str:
    if src.alice == PLAYING:
        return ALICE
    if src.ma == PLAYING:
        return MA
    return src.owner if src.owner in (ALICE, MA) else "none"


def _finish(acts: list[str], s: Sources) -> Decision:
    """Разрешить конфликт «двое играют» перед возвратом решения.

    Инвариант: владелец ровно один. Если событие не развело источники,
    оставляем владельца, второго гасим один раз.
    """
    if s.alice == PLAYING and s.ma == PLAYING:
        if s.owner == MA:
            acts.append(PAUSE_ALICE)
            s.alice = PAUSED
        else:
            acts.append(PAUSE_MA)
            s.ma = PAUSED
            if s.agent == STREAMING:
                acts.append(PAUSE_SPIN)
                s.agent = PAUSED
        s.pause_reason = REASON_OVERLAP
    return Decision(acts, s)


def decide(src: Sources, event: str) -> Decision:
    """Посчитать действия по матрице состояний.

    События:
      wake          — подтверждено обращение к нам, начинаем ход
      response_end  — ответ доигран, можно возвращать музыку
      button        — физическая кнопка паузы
      alice_started — нативный плеер заиграл
      ma_started    — MA начал отдавать поток
      stop          — голосовое «стоп»
      poll          — периодическая сверка (watchdog)
    """
    s = src.copy()
    acts: list[str] = []

    if event == "wake":
        if s.alice == PLAYING:
            acts += [PAUSE_ALICE]
            s.alice, s.owner, s.pause_reason = PAUSED, ALICE, REASON_HUB
        elif s.ma == PLAYING:
            acts += [PAUSE_MA, PAUSE_AGENT]
            s.ma, s.agent = PAUSED, PAUSED
            s.owner, s.pause_reason = MA, REASON_HUB
        return _finish(acts, s)

    if event == "response_end":
        if s.pause_reason == REASON_HUB:
            if s.owner == ALICE and s.alice == PAUSED:
                acts.append(PLAY_ALICE)
                s.alice = PLAYING
            elif s.owner == MA and s.ma == PAUSED:
                acts += [PLAY_MA, PLAY_AGENT]
                s.ma, s.agent = PLAYING, STREAMING
        s.pause_reason = "none"
        return _finish(acts, s)

    if event == "button":
        # Кнопка наша целиком: агент захватывает evdev через EVIOCGRAB, maind
        # нажатие не видит. Иначе получаются два toggle на одно нажатие —
        # «первый раз не гаснет, второй гасит».
        if s.pause_reason == REASON_HUB:
            # Звучал наш ответ. Пауза = тишина: обрываем речь и НЕ поднимаем
            # музыку. Владельца помним — следующее нажатие его вернёт.
            acts.append(STOP_AGENT)
            s.pause_reason = "none"
            return _finish(acts, s)

        if s.alice == PLAYING or s.ma == PLAYING or s.agent == STREAMING:
            # Пауза — всегда тишина: гасим всё, что может звучать.
            if s.alice == PLAYING:
                acts.append(PAUSE_ALICE)
                s.alice = PAUSED
                s.owner = ALICE
            else:
                s.owner = MA
            acts += [PAUSE_MA, PAUSE_SPIN]
            s.ma, s.agent = PAUSED, PAUSED
            s.pause_reason = REASON_BUTTON
            return _finish(acts, s)

        # Тишина — включаем того, кто был владельцем. Приоритет у Алисы:
        # очередь MA могла просто стоять в фоне.
        if s.owner == MA and s.ma_queue != "empty":
            acts += [PLAY_MA, PLAY_AGENT]
            s.ma, s.agent = PLAYING, STREAMING
            s.pause_reason = "none"
            return _finish(acts, s)
        acts.append(PLAY_ALICE)
        s.alice = PLAYING
        s.owner, s.pause_reason = ALICE, "none"
        return _finish(acts, s)

    if event in ("switch_source", "switch_alice"):
        # Длинное нажатие: переключить источник на другой.
        # MA → Алиса и Алиса → MA одним и тем же жестом.
        if s.pause_reason == REASON_HUB:
            # Если звучал наш ответ — сначала оборвать его, иначе он
            # доиграет поверх нового источника.
            acts.append(STOP_AGENT)
            s.pause_reason = "none"
        to_ma = s.alice == PLAYING or (s.owner == ALICE and s.alice == PAUSED)
        if to_ma:
            if s.ma_queue == "empty":
                # Продолжать нечего: не глушим Алису зря.
                return _finish(acts, s)
            if s.alice == PLAYING:
                acts.append(PAUSE_ALICE)
            s.alice = PAUSED
            acts += [PLAY_MA, PLAY_AGENT]
            s.ma, s.agent = PLAYING, STREAMING
            s.owner, s.pause_reason = MA, "none"
            return _finish(acts, s)
        # В сторону Алисы: снимаем MA и наш поток, поднимаем натив.
        if s.ma == PLAYING:
            acts.append(PAUSE_MA)
        if s.agent == STREAMING:
            acts.append(PAUSE_AGENT)
            s.agent = PAUSED
        if s.ma != IDLE:
            s.ma = PAUSED
        acts.append(PLAY_ALICE)
        s.alice = PLAYING
        s.owner, s.pause_reason = ALICE, "none"
        return _finish(acts, s)

    if event == "alice_started":
        s.alice = PLAYING
        if s.ma == PLAYING:
            # Алиса перебила MA: гасим очередь и наш поток. Без PAUSE_SPIN
            # aplay доигрывает буфер, и музыка звучит поверх Алисы.
            acts.append(PAUSE_MA)
            s.ma = PAUSED
        if s.agent == STREAMING:
            acts.append(PAUSE_SPIN)
            s.agent = PAUSED
        if s.ma == PAUSED:
            s.pause_reason = REASON_OVERLAP
        s.owner = ALICE
        return _finish(acts, s)

    if event == "ma_started":
        s.ma, s.agent = PLAYING, STREAMING
        if s.alice == PLAYING:
            acts.append(PAUSE_ALICE)
            s.alice, s.pause_reason = PAUSED, REASON_OVERLAP
        s.owner = MA
        return _finish(acts, s)

    if event == "stop":
        acts.append(STOP_AGENT)
        if s.pause_reason == REASON_HUB:
            if s.owner == ALICE and s.alice == PAUSED:
                acts.append(PLAY_ALICE)
                s.alice = PLAYING
            elif s.owner == MA and s.ma == PAUSED:
                acts += [PLAY_MA, PLAY_AGENT]
                s.ma, s.agent = PLAYING, STREAMING
        s.pause_reason = "none"
        return _finish(acts, s)

    if event == "poll":
        # Сверка фактов: два источника играют одновременно. Оставляем
        # владельца, второго гасим ровно один раз — после этого он уже
        # не playing, и следующая сверка ничего не делает.
        if s.alice == PLAYING and s.ma == PLAYING:
            if s.owner == ALICE:
                acts.append(PAUSE_MA)
                s.ma = PAUSED
                if s.agent == STREAMING:
                    acts.append(PAUSE_SPIN)
                    s.agent = PAUSED
            else:
                acts.append(PAUSE_ALICE)
                s.alice = PAUSED
            s.pause_reason = REASON_OVERLAP
        if s.ma == PLAYING and s.agent != STREAMING:
            acts.append(PLAY_AGENT)
            s.agent = STREAMING
        return _finish(acts, s)

    return _finish(acts, s)
