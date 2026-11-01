#!/usr/bin/env python3
"""Клиент Hermes /api/ws с ПОСТОЯННЫМ соединением на диалог.

Раньше на каждый вопрос открывалось новое websocket-соединение, а сессия живёт
вместе с ним: следующая реплика приходила с мёртвым id и получала
`session not found` (4001). Теперь соединение держится, пока идёт диалог,
и пересоздаётся только при реальном обрыве.
"""
import asyncio
import json
import os
import sys

import websockets

URL = 'ws://127.0.0.1:9119/api/ws'

LAST_SESSION = {'id': None}

# одна сессия на колонку: id хранится на диске и переиспользуется после
# перезапуска сервиса, чтобы каждое обращение не начинало новый диалог
# путь можно переопределить: в репозитории не место абсолютным рабочим путям
SESSION_FILE = os.environ.get(
    'STATION_SESSION_FILE',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '.session_id'))


def load_session() -> str | None:
    try:
        with open(SESSION_FILE) as f:
            sid = f.read().strip()
            return sid or None
    except OSError:
        return None


def save_session(sid: str) -> None:
    try:
        with open(SESSION_FILE, 'w') as f:
            f.write(sid)
    except OSError:
        pass


def token() -> str:
    try:
        with open(os.environ.get('HERMES_DASH_TOKEN_FILE',
                                  os.path.expanduser('~/.hermes/.dash_token'))) as f:
            return f.read().strip()
    except OSError:
        return ''


class HermesClient:
    def __init__(self, url: str = URL, timeout: float = 180.0):
        self.url = url
        self.timeout = timeout
        self.ws = None
        self.sid = None
        self._rid = 0

    async def close(self):
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass
        self.ws = None
        self.sid = None

    async def _connect(self):
        await self.close()
        self.ws = await websockets.connect(
            f'{self.url}?token={token()}', max_size=8 * 1024 * 1024,
            # шлюз молчит, пока думает над ответом (бывает 10-170 с), и
            # стандартный ping timeout рвал соединение: sent 1011 keepalive
            ping_interval=30, ping_timeout=180)
        self._rid = 0

    async def _send(self, method: str, params=None):
        self._rid += 1
        await self.ws.send(json.dumps(
            {'jsonrpc': '2.0', 'id': self._rid, 'method': method, 'params': params or {}}))

    async def _recv(self):
        raw = await asyncio.wait_for(self.ws.recv(), timeout=self.timeout)
        return json.loads(raw)

    async def _create_session(self) -> str:
        await self._send('session.create', {})
        for _ in range(60):
            msg = await self._recv()
            if msg.get('error'):
                raise RuntimeError(msg['error'])
            result = msg.get('result') or {}
            if result.get('session_id'):
                self.sid = result['session_id']
                LAST_SESSION['id'] = self.sid
                save_session(self.sid)
                return self.sid
        raise RuntimeError('не удалось создать сессию')

    async def _turn(self, text: str) -> str:
        await self._send('prompt.submit', {'session_id': self.sid, 'text': text})
        parts = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        while True:
            left = deadline - loop.time()
            if left <= 0:
                break
            try:
                msg = await asyncio.wait_for(self._recv(), timeout=min(left, 30))
            except asyncio.TimeoutError:
                if parts:
                    break
                continue
            if msg.get('error'):
                raise RuntimeError(msg['error'])
            params = msg.get('params') or {}
            ptype = params.get('type')
            if os.environ.get('HERMES_WS_DEBUG'):
                print('<<', json.dumps(msg, ensure_ascii=False)[:400], file=sys.stderr)
            payload = params.get('payload') or {}
            if ptype == 'message.delta':
                parts.append(payload.get('text', ''))
            elif ptype == 'message.complete':
                if not parts and payload.get('text'):
                    parts.append(payload['text'])
                break
            elif ptype in ('turn.complete', 'session.idle'):
                break
        return ''.join(parts).strip()

    @staticmethod
    async def _notify(on_event, kind: str, payload: dict) -> None:
        """Отдать наружу событие инструмента в общем виде.

        Формат один для панели и колонки: имя инструмента и стадия. Колбэк может
        быть и обычной функцией, и корутиной — вызываем по факту.
        """
        if on_event is None:
            return
        ev = {
            "type": "tool",
            "name": payload.get("name") or payload.get("preview") or "инструмент",
            "id": payload.get("tool_id"),
            "state": "start" if kind == "tool.start" else "complete",
        }
        try:
            res = on_event(ev)
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:  # noqa: BLE001
            print(f"событие инструмента не передано: {e}", file=sys.stderr)

    async def _turn_stream(self, text: str, on_event=None):
        """Тот же запрос, но куски ответа отдаются сразу (message.delta).

        События инструментов (tool.start / tool.complete) отдаются наружу через
        on_event: по ним колонка и панель показывают, что агент реально делает.
        """
        await self._send('prompt.submit', {'session_id': self.sid, 'text': text})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        sent = False
        while True:
            left = deadline - loop.time()
            if left <= 0:
                return
            try:
                msg = await asyncio.wait_for(self._recv(), timeout=min(left, 30))
            except asyncio.TimeoutError:
                continue
            if msg.get('error'):
                raise RuntimeError(msg['error'])
            params = msg.get('params') or {}
            ptype = params.get('type')
            payload = params.get('payload') or {}
            if os.environ.get('HERMES_WS_DEBUG'):
                print('<<', json.dumps(msg, ensure_ascii=False)[:400], file=sys.stderr)
            if ptype == 'message.delta':
                chunk = payload.get('text', '')
                if chunk:
                    sent = True
                    yield chunk
            elif ptype == 'message.complete':
                # полный текст приходит в конце: отдаём его только если дельт не было
                if not sent and payload.get('text'):
                    yield payload['text']
                return
            elif ptype in ('tool.start', 'tool.complete'):
                await self._notify(on_event, ptype, payload)
            elif ptype in ('turn.complete', 'session.idle'):
                return

    async def stream(self, text: str, on_event=None):
        """Асинхронный генератор кусков ответа — для потокового синтеза речи."""
        if self.ws is None:
            await self._connect()
        if not self.sid:
            self.sid = load_session()
        if not self.sid:
            await self._create_session()
        else:
            LAST_SESSION['id'] = self.sid
        try:
            async for piece in self._turn_stream(text, on_event):
                yield piece
            return
        except (RuntimeError, ConnectionError, websockets.exceptions.WebSocketException) as e:
            msg = str(e)
            need_reconnect = ('session not found' in msg or '4001' in msg
                              or isinstance(e, (ConnectionError,
                                                websockets.exceptions.WebSocketException)))
            if not need_reconnect:
                raise
            await self._connect()
            if 'session not found' in msg or '4001' in msg:
                self.sid = None
                await self._create_session()
            elif not self.sid:
                await self._create_session()
        async for piece in self._turn_stream(text, on_event):
            yield piece

    async def ask(self, text: str) -> str:
        """Вопрос агенту в рамках текущего диалога."""
        if self.ws is None:
            await self._connect()
        if not self.sid:
            self.sid = load_session()
        if not self.sid:
            await self._create_session()
        else:
            LAST_SESSION['id'] = self.sid
        try:
            return await self._turn(text)
        except (RuntimeError, ConnectionError, websockets.exceptions.WebSocketException) as e:
            msg = str(e)
            need_reconnect = ('session not found' in msg or '4001' in msg
                              or isinstance(e, (ConnectionError,
                                                websockets.exceptions.WebSocketException)))
            if not need_reconnect:
                raise
            # соединение умерло (в том числе keepalive ping timeout) — повторяем один раз
            log_note = 'переподключаюсь' if not isinstance(e, RuntimeError) else 'сессия умерла'
            print(f'{log_note}: {msg}', file=sys.stderr)
            await self._connect()
            if isinstance(e, RuntimeError) and ('session not found' in msg or '4001' in msg):
                self.sid = None
                await self._create_session()
            elif not self.sid:
                await self._create_session()
            return await self._turn(text)

CLIENT = HermesClient()


async def ask(text: str, timeout: float = 180.0, session_id: str | None = None) -> str:
    """Совместимый вход: постоянный клиент, аргументы оставлены для старых вызовов."""
    CLIENT.timeout = timeout
    if session_id and CLIENT.sid != session_id:
        CLIENT.sid = None
    return await CLIENT.ask(text)


async def stream(text: str, timeout: float = 180.0, on_event=None):
    """Совместимый вход: поток кусков ответа через постоянный клиент."""
    CLIENT.timeout = timeout
    async for piece in CLIENT.stream(text, on_event):
        yield piece


if __name__ == '__main__':
    q = ' '.join(sys.argv[1:]) or 'Скажи одним предложением, что ты жив.'
    print(asyncio.run(ask(q)))
