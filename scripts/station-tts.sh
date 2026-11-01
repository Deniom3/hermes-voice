#!/bin/bash
# Озвучка через Яндекс.Станцию: локальный TTS -> сервис tts-station -> колонка говорит по Glagol.
# Провайдер Hermes: {text_path} {output_path} {voice} {model}
# Адрес сервиса — STATION_TTS_ENDPOINT (по умолчанию локальный).
set -euo pipefail

TEXT_FILE="${1:?нужен путь к файлу с текстом}"
OUT="${2:?нужен путь для аудио}"
VOICE="${3:-M1}"
API="${STATION_TTS_ENDPOINT:-http://127.0.0.1:8771/say}"

/usr/bin/python3 - "$TEXT_FILE" "$OUT" "$VOICE" "$API" <<'PY'
import json
import sys
import urllib.error
import urllib.request

text_path, out, voice, api = sys.argv[1:5]
text = open(text_path, encoding="utf-8").read().strip()
if not text:
    sys.stderr.write("пустой текст")
    sys.exit(1)

payload = {"text": text, "voice": voice, "audio": True}
request = urllib.request.Request(
    api,
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(request, timeout=300) as response:
        body = response.read()
        ctype = response.headers.get("Content-Type", "")
except urllib.error.HTTPError as exc:
    sys.stderr.write("HTTP %s: %s" % (exc.code, exc.read()[:300].decode("utf-8", "replace")))
    sys.exit(1)

if not ctype.startswith("audio"):
    sys.stderr.write("не аудио в ответе: " + body[:300].decode("utf-8", "replace"))
    sys.exit(1)

with open(out, "wb") as handle:
    handle.write(body)
PY
