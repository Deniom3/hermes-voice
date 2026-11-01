"""Сервер голосового экрана Hermes: страница панели и её связь с хабом.

Браузер ──HTTPS (Traefik)──▶ :8792
   ├── /            — собранная страница (dist/)
   ├── /voice-assets, /assets — статика
   ├── /vnc/*       — прокси noVNC (живой экран браузера)
   ├── /health
   ├── WS /voice-ws — протокол браузера, переводится в протокол хаба
   └── WS /vnc-ws   — WebSocket для noVNC

Голосом занимается ядро: панель держит WS-клиент к хабу :8796, отдаёт туда
PCM с микрофона, а обратно получает состояния, текст и звук ответа.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import websockets
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice")

PORT = int(os.environ.get("VOICE_PORT", "8792"))
VNC_HTTP = os.environ.get("VOICE_VNC_HTTP", "http://127.0.0.1:6080")
VNC_WS = os.environ.get("VOICE_VNC_WS", "ws://127.0.0.1:6080/websockify")
DIST = Path(__file__).with_name("dist")


# Как «[Audio mode]» в видео: короткие ответы и проговаривание действий вслух.

_CTYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript",
    ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon",
    ".wasm": "application/wasm", ".onnx": "application/octet-stream",
}


CDP_URL = os.environ.get("VOICE_CDP_URL", "http://127.0.0.1:9223")


def _cdp_browser_ws_sync() -> str:
    """Адрес браузерного WebSocket Chrome (для команд Browser.*)."""
    with urllib.request.urlopen(CDP_URL + "/json/version", timeout=5) as r:
        return json.loads(r.read().decode()).get("webSocketDebuggerUrl", "")


def _cdp_first_page_sync() -> str:
    with urllib.request.urlopen(CDP_URL + "/json/list", timeout=5) as r:
        for t in json.loads(r.read().decode()):
            if t.get("type") == "page":
                return t.get("id", "")
    return ""


async def _cdp_cmd(ws_url: str, payload: dict) -> dict:
    import websockets

    async with websockets.connect(ws_url, max_size=None) as cdp:
        await cdp.send(json.dumps(payload))
        while True:
            raw = await asyncio.wait_for(cdp.recv(), timeout=10)
            d = json.loads(raw)
            if d.get("id") == payload.get("id"):
                return d


async def _window_state(ws_url: str, win: int, state: str, cid: int) -> str:
    """Ставит состояние окна; возвращает текст ошибки или пустую строку."""
    got = await _cdp_cmd(ws_url, {"id": cid, "method": "Browser.setWindowBounds",
                                  "params": {"windowId": win,
                                             "bounds": {"windowState": state}}})
    if got.get("error"):
        return str(got["error"].get("message"))[:140]
    return ""


async def set_screen_mode(mode: str) -> str:
    """Режим экрана панели: browser — Chrome во весь экран, desktop — рабочий стол."""
    try:
        ws_url = await asyncio.to_thread(_cdp_browser_ws_sync)
        target = await asyncio.to_thread(_cdp_first_page_sync)
        if not ws_url:
            return "нет связи с живым браузером"
        got = await _cdp_cmd(ws_url, {"id": 1, "method": "Browser.getWindowForTarget",
                                      "params": {"targetId": target}})
        win = (got.get("result") or {}).get("windowId")
        if win is None:
            return "окно браузера не найдено"
        # Chrome не переключает состояние напрямую (из полного экрана нельзя свернуть,
        # из свёрнутого — развернуть), поэтому всегда сначала обычное окно
        cur = await _cdp_cmd(ws_url, {"id": 4, "method": "Browser.getWindowBounds",
                                      "params": {"windowId": win}})
        state_now = ((cur.get("result") or {}).get("bounds") or {}).get("windowState")
        if state_now != "normal":
            await _window_state(ws_url, win, "normal", 5)
        if mode == "desktop":
            why = await _window_state(ws_url, win, "minimized", 2)
        else:
            why = await _window_state(ws_url, win, "maximized", 2)
        log.info("режим экрана: %s%s", mode, f" (ошибка: {why})" if why else "")
        return why
    except Exception as e:  # noqa: BLE001
        log.warning("режим экрана %s: %r", mode, e)
        return str(e)[:120]


def serve_http(path: str) -> tuple[int, str, bytes]:
    """Ответ на обычный GET: /health, /vnc/* (прокси), остальное — файлы из dist/."""
    raw = urllib.parse.urlparse(path)
    p = raw.path

    if p == "/health":
        return 200, "application/json", json.dumps({"ok": True}).encode()

    if p.startswith("/vnc"):
        target = VNC_HTTP + (p[len("/vnc"):] or "/")
        if raw.query:
            target += "?" + raw.query
        try:
            with urllib.request.urlopen(urllib.request.Request(target), timeout=15) as up:
                return up.status, up.headers.get("Content-Type", "application/octet-stream"), up.read()
        except urllib.error.HTTPError as e:
            return e.code, "text/plain", e.read()
        except Exception as e:  # noqa: BLE001
            log.warning("vnc-прокси: %r", e)
            return 502, "text/plain", b"vnc upstream unavailable"

    rel = p.lstrip("/") or "index.html"
    f = (DIST / rel).resolve()
    if not str(f).startswith(str(DIST.resolve())) or not f.is_file():
        f = DIST / "index.html"          # SPA-fallback
    if not f.is_file():
        return 404, "text/plain", b"not built yet"
    return 200, _CTYPES.get(f.suffix, "application/octet-stream"), f.read_bytes()


# ---------- шлюз Hermes ----------
_token_cache: dict[str, object] = {"v": "", "t": 0.0}
_MODELS_CACHE: dict[str, object] = {"v": [], "t": 0.0}


_CLIENTS = [0]         # сколько панелей сейчас открыто (для статистики)


_TTS_CACHE: dict[str, bytes] = {}


HUB_URL = os.environ.get("VOICE_HUB_URL", "ws://127.0.0.1:8796/ws")


async def client_via_hub(ws) -> None:
    """Панель как клиент голосового хаба: ядро одно, логика одна.

    Браузер остаётся на своём протоколе `/voice-ws`, а панель переводит его в
    протокол хаба: бинарный PCM вверх, состояния/ход/звук вниз. Вся речевая
    логика — ключевое слово, конец фразы, окно, распознавание, агент, синтез —
    живёт в хабе, поэтому колонка и панель ведут себя одинаково.
    """
    from websockets.asyncio.client import connect

    name = f"panel-{os.getpid() % 10000}"
    async with connect(HUB_URL, max_size=None) as hub:
        await hub.send(json.dumps({"t": "hello", "name": name, "role": "panel",
                                   "threshold": 0.95, "gain": 1.0, "agc": True,
                                   "write_audio": True}))
        turn = 0

        async def to_hub() -> None:
            async for msg in ws:
                if isinstance(msg, bytes):
                    await hub.send(msg)          # браузер уже отдаёт 16 кГц mono
                    continue
                try:
                    ctl = json.loads(msg)
                except Exception:  # noqa: BLE001
                    continue
                kind = ctl.get("type")
                if kind == "start":
                    await hub.send(json.dumps({"t": "start"}))
                elif kind == "cancel":
                    await hub.send(json.dumps({"t": "interrupt"}))
                elif kind == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif kind == "warm":
                    await ws.send(json.dumps({"type": "models", "list": [],
                                              "current": "hermes"}))
                elif kind in ("set_model", "new_session"):
                    # Голосом управляет хаб, а модель — Hermes; панель больше
                    # не держит собственный шлюз, иначе снова будет два пути.
                    await ws.send(json.dumps({
                        "type": "error",
                        "text": "модель и сессия задаются в Hermes, панель голосом не управляет"}))

        async def from_hub() -> None:
            nonlocal turn
            async for msg in hub:
                if isinstance(msg, bytes):
                    await ws.send(json.dumps({"type": "speaking", "turn": turn}))
                    await ws.send(msg)
                    await ws.send(json.dumps({"type": "audio_end", "turn": turn}))
                    continue
                d = json.loads(msg)
                t = d.get("t")
                if t == "state":
                    st = d.get("state")
                    if st == "collecting":
                        await ws.send(json.dumps({"type": "listening", "gate": False}))
                    elif st == "busy":
                        await ws.send(json.dumps({"type": "thinking"}))
                    elif st == "idle":
                        await ws.send(json.dumps({"type": "idle"}))
                elif t == "turn_start":
                    await ws.send(json.dumps({"type": "final", "text": d.get("text") or ""}))
                elif t == "tool":
                    # то же событие, что колонка показывает кольцом: панель
                    # рисует имя инструмента и его стадию
                    await ws.send(json.dumps({"type": "tool",
                                              "name": d.get("name"),
                                              "id": d.get("id"),
                                              "state": d.get("state")}))
                elif t == "turn_end":
                    turn += 1
                    await ws.send(json.dumps({"type": "agent_delta",
                                              "text": d.get("text") or ""}))
                    await ws.send(json.dumps({"type": "turn_done", "turn": turn}))
                elif t == "ping":
                    await hub.send(json.dumps({"t": "pong"}))
                elif t == "error":
                    await ws.send(json.dumps({"type": "error",
                                              "text": f"ошибка: {d.get('what')}"}))

        await asyncio.gather(to_hub(), from_hub())


async def client(ws) -> None:
    """Панель — клиент хаба: здесь только транспорт, ядро живёт в voice-hub."""
    peer = getattr(ws, "remote_address", None)
    log.info("клиент подключён (peer=%s)", peer)
    _CLIENTS[0] += 1
    await client_via_hub(ws)


async def vnc_ws(client_conn) -> None:
    """Прокси WebSocket для noVNC: браузер → мы → websockify."""
    from websockets.asyncio.client import connect

    sub = [s for s in (client_conn.subprotocol, "binary") if s]
    try:
        up = await connect(VNC_WS, max_size=None, subprotocols=sub,
                           ping_interval=None, open_timeout=20)
    except Exception as e:  # noqa: BLE001
        log.warning("VNC-аплинк: %r", e)
        await client_conn.close(code=4502)
        return

    async def to_up() -> None:
        async for m in client_conn:
            await up.send(m)

    async def to_client() -> None:
        async for m in up:
            await client_conn.send(m)

    t1, t2 = asyncio.create_task(to_up()), asyncio.create_task(to_client())
    try:
        await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (t1, t2):
            t.cancel()
        await up.close()


async def route(ws) -> None:
    path = getattr(getattr(ws, "request", None), "path", "") or ""
    if path.startswith("/vnc-ws"):
        await vnc_ws(ws)
    else:
        await client(ws)


async def process_request(connection, request):
    """Обычные GET отдаём сами; апгрейд на /voice-ws и /vnc-ws пропускаем в route()."""
    path = request.path or "/"
    if path.startswith("/voice-ws") or path.startswith("/vnc-ws"):
        return None
    if path.startswith("/browser-mode"):
        mode = urllib.parse.parse_qs(urllib.parse.urlparse(path).query).get("mode", ["browser"])[0]
        why = await set_screen_mode("desktop" if mode == "desktop" else "browser")
        body = json.dumps({"mode": mode, "ok": not why, "why": why}).encode()
        headers = Headers()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
        return Response(200, "OK", headers, body)
    if path.startswith("/warm"):
        # Прогрев не нужен: ASR и TTS на речевом шлюзе держатся постоянно.
        body = b'{"ok": true, "warming": false}'
        headers = Headers()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
        return Response(200, "OK", headers, body)
    try:
        status, ctype, body = serve_http(path)
    except Exception as e:  # noqa: BLE001
        log.exception("http: %r", e)
        status, ctype, body = 500, "text/plain", b"internal error"
    headers = Headers()
    headers["Content-Type"] = ctype
    headers["Content-Length"] = str(len(body))
    headers["Cache-Control"] = "no-store" if ctype.startswith("text/html") else "max-age=300"
    return Response(status, "OK" if status == 200 else "ERR", headers, body)


async def main() -> None:
    async with serve(route, "0.0.0.0", PORT, max_size=None, process_request=process_request):
        log.info("слушаю 0.0.0.0:%s — страница + /vnc + WS /voice-ws и /vnc-ws", PORT)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
