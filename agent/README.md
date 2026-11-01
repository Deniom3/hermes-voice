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

Автозапуск — `deploy/S99zhermes`. Адрес хаба берётся из `/data/opt/hermes-station/agent.env`:

```sh
VOICE_HUB_ADDR=<hub-host>
```

Без файла агент стучится в `127.0.0.1` — это безопасный дефолт для отладки, на колонке
адрес нужно задать.

Параметры запуска: `-server ws://<hub-host>:8796/ws`, `-name <имя>`, `-gain`, `-volume`.
