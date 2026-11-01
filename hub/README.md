# hub — серверная часть голосового контура

Здесь живёт всё, что обслуживает колонки и панель: речевое ядро, сборщик фразы,
WebSocket-хаб, слой диалога с агентом Hermes и быстрый дом через Home Assistant.

```
колонка (Go-агент) ⇄ WebSocket 8796 ⇄ voice_hub.py
                                        ├─ voice_core.py    автомат состояний
                                        ├─ voice_loop.py    детектор, AGC, сбор фразы, ASR
                                        ├─ voice_dialog.py  ход «текст → агент → звук»
                                        └─ fast_route.py    быстрый дом без модели
```

## Файлы

| Файл | Что делает |
|---|---|
| `voice_hub.py` | WebSocket-сервер: несколько клиентов, команды устройству, HTTP `/health`, `/interrupt`, `/cmd` |
| `voice_core.py` | состояния `IDLE / COLLECTING / BUSY / DIALOG_WAIT`, параметры тракта |
| `voice_loop.py` | детектор ключевого слова, AGC, `PhraseCollector`, клиент распознавания |
| `voice_dialog.py` | диалог с агентом: очистка текста, синтез, потоковая отдача звука |
| `wake_gate.py` | локальный детектор ключевого слова (TFLite), грузится и как модуль, и как скрипт |
| `wake_probe.py` | проверка ключевого слова на записи |
| `ha_morph.py` | стеммер и матчер устройств Home Assistant |
| `ha_route.py` | команды и показания HA без модели |
| `fast_route.py` | развилка «быстрый дом или агент» |
| `hermes_ws.py` | клиент агента Hermes |
| `smart_turn.py` | семантический конец реплики (ONNX) |
| `voice_fillers.py` | фразы-заполнители тишины |
| `voice_capture.py` | сбор окон событий в датасет ключевого слова |
| `kws_train_cleanup.py` | обслуживание датасета обучения |
| `models/` | модель ключевого слова |
| `fillers/` | готовые фразы-заполнители |
| `tests/` | тесты поведения |
| `deploy/` | systemd-юнит хаба |

## Запуск

```bash
/opt/hermes-voice/.venv-wake/bin/python voice_hub.py --port 8796 --out /tmp/station-hub
```

systemd-юнит — `deploy/voice-hub.service`.

## Что читать дальше

- [../docs/station-processes.md](../docs/station-processes.md) — как колонка слышит и собирает фразу
- [../docs/hub-protocol.md](../docs/hub-protocol.md) — протокол WebSocket
- [../docs/architecture.md](../docs/architecture.md) — общая картина
