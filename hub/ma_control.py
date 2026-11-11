"""Управление Music Assistant из хаба.

Единственная точка управления воспроизведением: голос приходит в хаб, хаб
вызывает MA, а колонка остаётся тонким Sendspin-клиентом. Агент по-прежнему
умеет локальный stop на случай недоступности MA, но очередь и позиция живут
здесь.

MCP-сервер MA запускается как дочерний процесс и speaks JSON-RPC по stdio.
Так хаб не тянет в свой venv зависимости music-assistant-client и использует
тот же проверенный путь, что и Hermes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

log = logging.getLogger("ma_control")

MCP_BIN = os.environ.get("MA_MCP_BIN", "/root/.local/bin/music-assistant-mcp")


class MAClient:
    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._ready = False

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        url = env.get("MUSIC_ASSISTANT_URL") or env.get("MA_URL")
        if not url:
            try:
                from . import config
            except ImportError:  # запуск как скрипт
                import config
            url = config.get("music_assistant", "url", default="")
        token = (env.get("MUSIC_ASSISTANT_TOKEN") or env.get("MA_TOKEN") or "")
        if url:
            env["MUSIC_ASSISTANT_URL"] = str(url)
        if token:
            env["MUSIC_ASSISTANT_TOKEN"] = token
        return env

    async def start(self) -> bool:
        if self.proc is not None and self.proc.returncode is None and self._ready:
            return True
        if self.proc is not None:
            await self.close()
        if not os.path.exists(MCP_BIN):
            log.warning("MA: нет %s — управление музыкой выключено", MCP_BIN)
            return False
        env = self._env()
        if not env.get("MUSIC_ASSISTANT_URL") or not env.get("MUSIC_ASSISTANT_TOKEN"):
            log.warning("MA: не заданы URL/токен — управление музыкой выключено")
            return False

        self.proc = await asyncio.create_subprocess_exec(
            MCP_BIN, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr = asyncio.create_task(self._drain_stderr())
        try:
            await self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hermes-hub", "version": "1"},
            }, timeout=15)
            await self._notify("notifications/initialized", {})
        except Exception:
            await self.close()
            raise
        self._ready = True
        log.info("MA: MCP-клиент подключён")
        return True

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = msg.get("id")
            fut = self._pending.pop(mid, None) if mid is not None else None
            if fut is not None and not fut.done():
                fut.set_result(msg)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("MA MCP закрылся"))
        self._pending.clear()
        self._ready = False

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                log.debug("MA stderr: %s", text)

    async def _notify(self, method: str, params: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params,
        }) + "\n").encode())
        await self.proc.stdin.drain()

    async def _request(self, method: str, params: dict, timeout: float = 20) -> dict:
        assert self.proc and self.proc.stdin
        self._next_id += 1
        mid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        self.proc.stdin.write((json.dumps({
            "jsonrpc": "2.0", "id": mid, "method": method, "params": params,
        }) + "\n").encode())
        await self.proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(mid, None)

    async def call(self, name: str, arguments: dict) -> dict:
        async with self._lock:
            if not await self.start():
                raise RuntimeError("MA недоступен")
            msg = await self._request("tools/call", {
                "name": name, "arguments": arguments,
            })
        if "error" in msg:
            raise RuntimeError(str(msg["error"]))
        result = msg.get("result") or {}
        if result.get("isError"):
            text = " ".join(
                c.get("text", "") for c in result.get("content", [])
                if isinstance(c, dict)
            )
            raise RuntimeError(text or "MA вернул ошибку")
        return result.get("structuredContent") or {}

    async def close(self) -> None:
        self._ready = False
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._stderr is not None:
            self._stderr.cancel()
            self._stderr = None
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
        self.proc = None

    async def players(self) -> list[dict]:
        res = await self.call("ma_list_players", {})
        return res.get("players", []) if isinstance(res, dict) else []


async def set_volume(station: str, percent: int, force: bool = False) -> bool:
    """Синхронизировать громкость MA с громкостью колонки.

    Вызывается из того же события `volume`, которым агент сообщает об
    изменении кнопкой или Алисой. Отдельного канала синхронизации нет:
    хаб — единственная точка, которая знает и ALSA, и MA-плеер.

    Если MA уже показывает тот же уровень, команду не отправляем: иначе
    ответ MA на нашу команду снова приходит событием `volume` и получается
    петля «колонка → MA → колонка». `force=True` нужен для события от
    агента: кэш MA мог не обновиться, и сравнение с ним пропускало реальное
    изменение.
    """
    p = await _player(station)
    if not p:
        return False
    level = max(0, min(100, int(percent)))
    current = p.get("volume_level")
    if not force and current is not None:
        try:
            if int(current) == level:
                return False
        except (TypeError, ValueError):
            pass
    await MA.call("ma_volume", {"params": {
        "player_id": p["player_id"], "level": level,
    }})
    return True


MA = MAClient()

# Станции, очередь которых поставил на паузу именно хаб. После команды
# `ma_playback pause` MA может показывать `idle`, а не `paused`; без этого
# флага `resume_if_paused()` не находил паузу и музыка не возвращалась.
_PAUSED_BY_HUB: set[str] = set()


def _norm(text: str) -> str:
    t = text.lower().replace("ё", "е")
    # Пунктуация врозь: «Гермес, поставь на паузу» — запятая прилипает
    # к слову и мешает отбросить обращение.
    for ch in ",.!?;:—–":
        t = t.replace(ch, " ")
    # Вежливые вставки и обращения не должны мешать команде.
    words = [w for w in t.split() if w not in _FILLER]
    return " ".join(words).strip()


# Слова, которые не меняют смысл музыкальной команды.
_FILLER = {
    "пожалуйста", "плиз", "гермес", "алиса", "окей", "ok", "hey",
    "ну", "давай", "мне", "пожалуиста", "будь", "добр",
}


def parse(text: str) -> tuple[str, int | None] | None:
    """Разобрать музыкальную команду. Возвращает (действие, секунды)."""
    t = _norm(text)
    if not t:
        return None
    if t in ("стоп", "останови", "остановись", "выключи музыку", "хватит музыки"):
        return ("stop", None)
    if t in ("пауза", "поставь на паузу", "приостанови", "приостанови музыку"):
        return ("pause", None)
    if t in ("продолжи", "продолжай", "играй", "возобнови", "включи музыку",
             "играй музыку"):
        return ("play", None)
    if t in ("дальше", "следующий", "следующая", "следующий трек", "next"):
        return ("next", None)
    if t in ("назад", "предыдущий", "предыдущая", "предыдущий трек", "previous"):
        return ("previous", None)
    if t in ("что играет", "какой трек", "что за трек", "что сейчас играет"):
        return ("status", None)
    # «перемотай на 30 секунд», «перемотай на 2 минуты», «в начало»
    if t in ("в начало", "с начала", "перемотай в начало", "перемотай на начало"):
        return ("seek", 0)
    m = re.search(r"перемот\w*\s+(?:вперед\s+|назад\s+)?на\s+(\d+)\s*(секунд|сек|минут|мин)", t)
    if m:
        n = int(m.group(1))
        if m.group(2).startswith("мин"):
            n *= 60
        return ("seek", n)
    return None


def _station_name(station: str) -> str:
    try:
        from . import config
    except ImportError:
        import config
    return str(config.get("wake", "stations", station, "sendspin", "name",
                          default=f"{station} (Hermes)"))


async def _player(station: str) -> dict | None:
    name = _station_name(station)
    for p in await MA.players():
        if p.get("name") == name:
            return p
    log.warning("%s: MA-плеер %r не найден", station, name)
    return None


async def pause_if_playing(station: str) -> bool:
    """Поставить очередь MA на паузу, только если она реально играет.

    Возвращает True, если пауза поставлена именно нами: хаб запомнит это и
    вернёт музыку после ответа. Если играет нативный Яндекс-плеер, наш
    MA-плеер idle — вернём False и не будем трогать чужую очередь.
    """
    p = await _player(station)
    if not p or p.get("state") != "playing":
        return False
    await MA.call("ma_playback", {"params": {
        "queue_id": p["player_id"], "command": "pause",
    }})
    _PAUSED_BY_HUB.add(station)
    return True


async def resume_if_paused(station: str) -> bool:
    """Вернуть очередь MA, если её ставил на паузу хаб.

    MA после `pause` может отдавать `idle`; поэтому кроме состояния смотрим
    на флаг, который ставится только в `pause_if_playing`.
    """
    p = await _player(station)
    if not p:
        return False
    if station not in _PAUSED_BY_HUB and p.get("state") != "paused":
        return False
    await MA.call("ma_playback", {"params": {
        "queue_id": p["player_id"], "command": "play",
    }})
    _PAUSED_BY_HUB.discard(station)
    return True


async def queue_len(station: str) -> int:
    """Есть ли что продолжать в очереди станции: 0 — пусто.

    `now_playing` покрывает случай «включили один трек»: очередь как список
    может быть пуста, а текущий трек ещё стоит на паузе.
    """
    p = await _player(station)
    if not p:
        return 0
    try:
        res = await MA.call("ma_queue", {"params": {
            "queue_id": p["player_id"], "limit": 1,
        }})
    except Exception as e:  # noqa: BLE001
        log.warning("MA: очередь не получена: %s", e)
        return -1
    q = res.get("queue") if isinstance(res, dict) else None
    if not isinstance(q, dict):
        # Не «пусто», а «не знаем»: иначе кнопка решит, что продолжать
        # нечего, и отдаст нажатие maind — заиграет Алиса.
        return -1
    items = q.get("items")
    if isinstance(items, list) and items:
        return 1
    return 1 if q.get("now_playing") else 0


def forget(station: str) -> None:
    """Забыть нашу паузу при переподключении станции.

    Флаг «пауза наша» живёт в памяти модуля и после реконнекта уже не
    отражает реальность; возврат музыки должен опираться на состояние MA.
    """
    _PAUSED_BY_HUB.discard(station)


async def player_state(station: str) -> str | None:
    """Состояние плеера станции в MA: playing / paused / idle."""
    p = await _player(station)
    return (p or {}).get("state")


async def play_forced(station: str) -> bool:
    """Продолжить очередь MA, даже если её ставили на паузу не мы.

    С ПК/приложения MA после pause нередко отдаёт `idle`, поэтому
    `resume_if_paused` возвращает False. Кнопка «продолжить» должна работать
    с любой паузой, а не только с нашей.
    """
    p = await _player(station)
    if not p or p.get("state") == "playing":
        return False
    try:
        await MA.call("ma_playback", {"params": {
            "queue_id": p["player_id"], "command": "play",
        }})
    except Exception as e:  # noqa: BLE001
        log.warning("MA: play по кнопке не прошёл: %s", e)
        return False
    _PAUSED_BY_HUB.discard(station)
    return True


async def control(station: str, action: str, seconds: int | None = None) -> str | None:
    """Выполнить команду для станции. Возвращает текст ответа или None."""
    p = await _player(station)
    if not p:
        return None
    pid = p["player_id"]

    if action == "status":
        if p.get("state") not in ("playing", "paused"):
            return "ничего не играет"
        res = await MA.call("ma_queue", {"params": {"queue_id": pid, "limit": 1}})
        q = res.get("queue") if isinstance(res, dict) else None
        now = (q or {}).get("now_playing") or {}
        title = now.get("name") or "ничего не играет"
        return title

    if action == "seek":
        if seconds is None:
            return None
        await MA.call("ma_playback", {"params": {
            "queue_id": pid, "command": "play", "seek_seconds": int(seconds),
        }})
        return None

    if action in ("play", "pause", "stop", "next", "previous"):
        if action in ("play", "stop"):
            _PAUSED_BY_HUB.discard(station)
        await MA.call("ma_playback", {"params": {
            "queue_id": pid, "command": action,
        }})
        return None
    return None
