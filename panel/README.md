# panel — веб-панель голосового контура

Браузерная панель: микрофон из браузера, живой экран (браузер на VNC), окна и трансляция
рабочего стола. Панель — тонкий транспорт: голосовой тракт целиком живёт в хабе, панель
только гоняет звук и события по WebSocket.

```
Браузер ──WS /voice-ws──▶ server.py :8792 ──WS клиент──▶ хаб :8796
                            ├─ /            собранный фронтенд из dist/
                            ├─ /vnc/*       прокси noVNC
                            └─ /vnc-ws      WebSocket noVNC
```

## Состав

```
server.py        HTTP/WS-сервер панели, раздаёт dist/ и проксирует VNC
frontend/        Vite + React: орб, сцена, окна, микрофон, VAD
deploy/          systemd-юниты панели и живого браузера, запуск Chrome
```

## Сборка и запуск

```bash
cd frontend
npm ci
npm run build        # кладёт сборку в panel/dist — оттуда её отдаёт server.py
```

```bash
python3 server.py    # слушает VOICE_PORT (8792)
curl -s localhost:8792/health
```

`server.py` берёт переменные из окружения (см. `../.env.example`):
`VOICE_PORT`, `VOICE_HUB_URL`, `VOICE_CDP_URL`, `VOICE_VNC_HTTP`, `VOICE_VNC_WS`.

Юниты: `deploy/voice-panel.service` и `deploy/hermes-voice-browser.service`.
