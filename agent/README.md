# agent — программа на колонке

Агент читает микрофон колонки, держит кольцевой буфер, отправляет PCM в хаб по WebSocket,
проигрывает ответ, управляет кольцом и музыкой. Написан на Go, собирается под 32-битный ARM.

```
микрофон ──PCM 16 кГц──▶ wsloop.go ──WebSocket──▶ хаб :8796
кольцо/музыка ◀── control.go (локальные команды) ──┘
```

## Сборка

```bash
GOOS=linux GOARCH=arm GOARM=7 CGO_ENABLED=0 \
  go build -trimpath -ldflags="-s -w" -o voice-agent .
```

## Установка на колонку

```bash
ssh -p 2222 root@<station-host> 'cat > /data/opt/hermes-station/voice-agent.new' < voice-agent
ssh -p 2222 root@<station-host> \
  'chmod +x /data/opt/hermes-station/voice-agent.new
   mv /data/opt/hermes-station/voice-agent.new /data/opt/hermes-station/voice-agent
   /etc/init.d/S99zhermes restart
   tail -5 /tmp/hermes-agent.log'
```

Автозапуск — `deploy/S99zhermes` (Лайт 1) и `deploy/S99zhermes.lite2`
(Лайт 2). Адрес хаба агент не хранит: он шлёт UDP-broadcast `HERMES-HUB?` на
порт 8797 и подключается к ответу; при переподключении поиск повторяется.
Имя станции хаб определяет по MAC из `wake.stations.<имя>.mac`.

`agent.env` на колонке не нужен: ни адрес хаба, ни параметры Sendspin там не
хранятся. Параметры запуска: `-v`, `-platform auto|lite1|lite2`,
`-mic-socket /data/opt/hermes-agent/mic.sock` (для plum), `-log`.
Флаг `-server` остался только для ручной отладки; `-name` удалён.

## Платформы: Лайт 1 (micro) и Лайт 2 (plum)

Платформа определяется автоматически по кольцу (`/sys/class/leds/plum:*` или `micro:*`)
или задаётся флагом `-platform auto|lite1|lite2`.

| | Лайт 1 (micro) | Лайт 2 (plum) |
|---|---|---|
| LED | `micro:*:ringN`, триггер `none` | `plum:*:ringN`, активен `led_ring_fb` |
| Индикация | анимации `.led` из `/data/quasar/ledpatterns` | статический цвет (анимаций `.led` нет) |
| Звук | `maind --gst-launch` | `aplay` (у maind на plum нет `--gst-launch`) |
| Штатные звуки | `/system/vendor/quasar/sounds/common/*.opus` | `/system/vendor/quasar/sounds/*` (файлы на самом деле mp3/ogg) |
| Микрофон | `yio_audio` IPC `127.0.0.1:9850` | IPC кадров не отдаёт — нужен VQE-хук |

На plum перед записью яркости агент снимает `led_ring_fb` и возвращает его после,
иначе кольцо не реагирует, а штатные анимации Алисы пропадают.
Озвучка на plum пока принимает только WAV; сырой PCM (S16_LE, 48 кГц, моно)
играется через `aplay -f S16_LE -r 48000 -c 1 -t raw` — проверено на устройстве.

## Громкость

Источник истины — ALSA-микшер `Master`, тот же, которым управляют голос Алисы и кнопки.
Снаружи агент работает уровнями 0–10 (0 — мьют), внутри переводит уровень в raw микшера
по калибровке платформы. Пусто — линейный режим.

Громкость Music Assistant 0–100 пишется прямо в ALSA `Master`; обратно в MA
уходит фактический процент через `client/state`. Поэтому ползунок MA и
кнопки/Алиса не расходятся.

Почему таблица, а не проценты: у Лайт 1 при уровне 5 микшер показывает 50%, у Лайт 2 —
58%, но дБ у обеих −25.4. Проценты у моделей разные, громкость одна. Таблица снимается
калибровкой (музыка на паузе, `media_player.volume_set` по 0.1…1.0, затем `amixer sget`);
подробности и замеры — в `docs/deploy.md`, раздел «Таблица громкости и калибровка».

- `volume_set` ставит raw из таблицы; уровень вне 0–10 зажимается.
- `volume` **всегда** читает микшер заново: Алиса и кнопки меняют его без нас, и кэш
  на вопрос «какая громкость» сразу после них врал.
- Фоновый опрос — раз в 2 с; изменение уходит хабу событием `volume`.
- `hello` при подключении читает микшер сразу, иначе первый `hello` уходил с уровнем 0.

## Sendspin (Music Assistant)

Агент умеет быть плеером MA по протоколу Sendspin: MA подключается к колонке
(точнее, клиент сам идёт на `ws://MA:8927/sendspin`), отдаёт PCM 48 кГц/16 бит/стерео,
агент пишет его в `aplay -D default`. Ни mDNS, ни FLAC/Opus, ни отдельного бинаря нет.

Настройки Sendspin живут на хабе, в `/etc/hermes-voice/hub.json`; агент
получает их сообщением `sendspin` после `hello_ok`. Отдельного `agent.env`
на колонке нет.

```json
"sendspin": {
  "url": "ws://192.168.1.10:8927/sendspin",
  "output": "default"
},
"wake": {
  "stations": {
    "lite-1": {
      "sendspin": {
        "name": "Комната (Hermes)",
        "state_dir": "/data/opt/hermes-station/sendspin"
      }
    }
  }
}
```

`state_dir` хранит `identity.key`; после перезагрузки это тот же плеер.
Без `sendspin.url` или при `"enabled": false` блок не запускается.

Арбитраж: `pause`/`play` от хаба при активном Sendspin управляют только им и
не трогают нативный `mediad`; на время `say`/`sound` aplay останавливается и
запускается после ответа. Очередь MA на время голосового хода ставит на паузу
сам хаб через `ma_control.pause_if_playing()`, а после ответа снимает её через
`resume_if_paused()` — иначе поток продолжает идти и позиция уезжает. Если
нативный mediad всё же играет, `stream/start` гасит его — иначе Yandex и MA
смешиваются в dmix.

Диагностика: в логе должны быть `ws: sendspin config получен`, `Noise OK`,
`server/activate`, `aplay запущен`; при паузе — `aplay остановлен`.

## Микрофон на plum (VQE-хук)

`yio_audio` на plum кадров не отдаёт, поэтому микрофон берётся из VQE-движка
`platformd`: `tap/vqe_tap_hermes.c` перехватывает `YandexVqeC_GetOutputChannelData`
(kind 6, канал 0), конвертирует float32 → S16_LE 16 кГц и шлёт датаграммы в Unix-сокет
(`HERMES_VQE_SOCKET`). Хук подключается только через профиль `launch.bridge.json`
и `LD_PRELOAD` в `platformd`; системный `launch.json` не трогаем.

## Грабли на plum

- **`LD_PRELOAD` с хуком на `snd_pcm_readi` в `maind --platformd` роняет колонку.**
  Симптомы: `ALSA open /dev/snd/pcmC0D1c failed -16`, `sh: /system/bin/startap.sh: not found`,
  уход в первичную настройку. Такой хук (`tap/vqe_alsa_tap.c`) удалён из репозитория;
  рабочий путь — только VQE-хук выше.
- **Quasar запускать строго из `/data/quasar`.** `quasar_launcher2 <профиль>` из другого
  каталога уводит колонку в setup. Проверка: `readlink /proc/<pid>/cwd`.
- **`killall maind` не делать** — колонка падает.
- Автозапуск — `/etc/init.d/S99zhermes` плюс подмена `/system/vendor/quasar/run.sh`
  с откатом на штатный `launch.json`, если профиля нет. После обновления прошивки
  `/etc/init.d` затирается, скрипт нужно залить заново.
