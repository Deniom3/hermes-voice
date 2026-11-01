# Развёртывание

## Где что лежит

| Что | Путь |
|---|---|
| исходники (репозиторий) | `/opt/hermes-voice` |
| рабочая копия серверной части | `/srv/hermes-station/server` |
| окружение Python | `/opt/hermes-voice/.venv-wake` |
| юниты systemd | `/etc/systemd/system` |
| модель ключевого слова | `/srv/kws/models/wakeword_e90e6e54.json` |
| записи для разбора | `/tmp/station-hub` |
| агент на колонке | `/data/opt/hermes-station/voice-agent` |

Сейчас прод запускается из `/srv/hermes-station/server`, а в репозитории лежат те же файлы
в `hub/`. Раскладка историческая; при обновлении копировать файлы из репозитория в рабочую
копию и сверять `diff` — расхождение означает, что правили не там.

## Сервер: хаб и панель

```bash
cd /opt/hermes-voice
git pull

# хаб
cp hub/voice_hub.py hub/voice_dialog.py \
   hub/voice_core.py hub/voice_loop.py /srv/hermes-station/server/
/opt/hermes-voice/.venv-wake/bin/python -m py_compile /srv/hermes-station/server/voice_hub.py
systemctl restart voice-hub
curl -s http://127.0.0.1:8796/health

# панель (если менялся фронтенд)
cd panel/frontend && npm ci && npm run build
systemctl restart voice-panel
```

Юниты лежат в `panel/deploy/` и `hub/deploy/`. Установка:

```bash
cp hub/deploy/voice-hub.service panel/deploy/voice-panel.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now voice-hub voice-panel
```

## Переменные хаба

Задаются в `voice-hub.service` строкой `Environment=` (или через `EnvironmentFile`). По умолчанию всё выключено, чтобы поведение было предсказуемым:

- `ASR_URL`, `TTS_URL` — адреса речевого шлюза для распознавания и синтеза. **По умолчанию
  локальные (`127.0.0.1:8000`).** Если шлюз на другой машине, оба адреса обязательны в
  окружении хаба — иначе распознавание и синтез будут стучаться в localhost и молча падать.
- `HERMES_WS_URL`, `HERMES_DASH_TOKEN_FILE` — агент Hermes и файл с токеном.
- `HASS_URL`, `HASS_TOKEN` — Home Assistant для быстрого дома; без них модуль выключен.
- `STATION_ENTITY` — колонка, чья комната считается комнатой по умолчанию.

- `VOICE_FILLERS=1` — фразы-заполнители в тишине («секунду, думаю»), пока агент не начал
  отвечать. Пока не отлажены на живом разговоре, оставляем выключенными.
- `VOICE_FILLER_FIRST`, `VOICE_FILLER_MIN_GAP`, `VOICE_FILLER_MAX_GAP`, `VOICE_FILLER_MAX` —
  тайминги и предел числа фраз.
- `VOICE_FILLER_DIR` — каталог готовых записей, если он не `/opt/hermes-voice/hub/fillers/<голос>`.
- `VOICE_RESUME_DELAY` — через сколько секунд после IDLE возвращать музыку на колонку
  (по умолчанию 3). Меньше — музыка влезает в микрофон, пока человек говорит фразу после
  «окей, Гермес»; больше — долгая пауза после разговора.
- `KWS_TRAIN=0` — выключить запись окон событий (по умолчанию запись включена).
- `KWS_TRAIN_DIR` — каталог живого слоя данных обучения (по умолчанию `/srv/kws/train`),
  внутри `positives`, `negatives`, `unclear` и `versions` (замороженные снимки).
- `KWS_MODEL_JSON` — путь к модели ключевого слова (в репозиторий не копируется).
- `VOICE_DUMP_STREAM=1` — писать полный поток соединения на диск для разбора (много места).

## Переменные панели

У панели свои, только транспортные — голосовой тракт целиком живёт в хабе:

- `VOICE_PORT` — порт панели (8792);
- `VOICE_HUB_URL` — хаб, к которому она подключается (`ws://127.0.0.1:8796/ws`);
- `VOICE_CDP_URL`, `VOICE_VNC_HTTP`, `VOICE_VNC_WS` — окно живого браузера и трансляция стола.

## Колонка: агент

Сборка под armhf (колонка — 32-битный ARM):

```bash
cd /opt/hermes-voice/agent
GOOS=linux GOARCH=arm GOARM=7 CGO_ENABLED=0 \
  go build -trimpath -ldflags="-s -w" -o voice-agent .
```

Заливка и запуск:

```bash
ssh -p 2222 root@<station-host> 'cat > /data/opt/hermes-station/voice-agent.new' < voice-agent
ssh -p 2222 root@<station-host> \
  'chmod +x /data/opt/hermes-station/voice-agent.new
   mv /data/opt/hermes-station/voice-agent.new /data/opt/hermes-station/voice-agent
   /etc/init.d/S99zhermes restart
   tail -5 /tmp/hermes-agent.log'
```

В логе должны появиться строки `ws: подключено к ws://…` и `сервер принял hello`. В `health`
хаба — клиент с нужным именем и растущий `audio_mb`.

Параметры запуска задаются в `/etc/init.d/S99zhermes` (копия — `agent/deploy/S99zhermes`):

```
-server "ws://<hub-host>:8796/ws"  -name lite-1  -threshold 0.95  -v
```

Порог берётся из `agent.env` (`VOICE_THRESHOLD`, по умолчанию `0.95`) и уходит в `hello`;
усиление — флаг `-gain`, по умолчанию `10`. Если микрофон тихий и слово не ловится,
добавляют `-gain 250` (замеры 31.10: ×10 давало 0.814, ×250 — 0.960).

Вторая колонка подключается с другим `-name`; порог и усиление подбираются под её микрофон.

## Модели

Распознавание и синтез живут на <speech-host> и доступны **через один шлюз на :8000**
(он же `/v1/models`). Порты `:8010` (audio.cpp) и `:8011` (silero-tts) — внутренние,
клиенты на них не ходят. Если шлюз не ответил, хаб пишет в журнал
`ASR: шлюз … не ответил (…), попытка N из 2` — по этой строке и разбирать.

```bash
curl -s http://<speech-host>:8000/health
curl -s http://<speech-host>:8000/v1/models        # какие модели видит шлюз
```

Обновление модели ключевого слова: заменить файлы в `/srv/kws/models/` и перезапустить
`voice-hub`. Путь берётся из переменной `KWS_MODEL_JSON`, копию в репозиторий не кладём.

## Проверка после обновления

1. `curl -s http://127.0.0.1:8796/health` — клиент подключён, состояние `idle`.
2. Сказать «Окей, Гермес, сколько времени» — в журнале должна пройти цепочка
   `активация → ключ подтверждён → фраза → ответ → первый звук`.
3. Проверить тайминги: сбор фразы 2–4 с, распознавание до 0.5 с, ответ агента 3–12 с,
   синтез до 0.5 с.

## Откат

- **Хаб**: `git revert <коммит>` или вернуть прошлую версию файлов в рабочую копию и
  `systemctl restart voice-hub`.
- **Агент**: собрать из предыдущего коммита и залить тем же способом. Старый бинарь хранить
  не обязательно — сборка воспроизводима из репозитория.
