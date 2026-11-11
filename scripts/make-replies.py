#!/usr/bin/env python3
"""Сборка библиотеки реплик: синтезируем фразы из манифеста в wav рядом с ним.

Зачем: готовая фраза должна звучать мгновенно и не зависеть от того, поднят ли
синтез. Файл в репозитории — это запись, которую видно в git и не надо
синтезировать ни при первом запуске, ни после чистки кэша.

Манифест `hub/replies/<голос>/manifest.json`:

    {"voice": "eugene", "pools": {"think": ["Секунду, думаю.", ...]}}

Ключ файла тот же, что у рантайма (`replies.key`): sha1(текст|голос)[:16].

Запуск:
    .venv-wake/bin/python scripts/make-replies.py            # только отсутствующие
    .venv-wake/bin/python scripts/make-replies.py --force    # перезаписать все
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub"))

import replies  # noqa: E402
import voice_dialog  # noqa: E402


def main(force: bool) -> int:
    out_dir = replies._repo_dir()
    manifest_path = os.path.join(out_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        print(f"нет манифеста: {manifest_path}")
        return 1
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    pools: dict[str, list[str]] = manifest.get("pools") or {}
    if not pools:
        print("в манифесте нет pools")
        return 1

    made = kept = 0
    failed: list[str] = []
    index: dict[str, dict] = {}
    for name, phrases in pools.items():
        for phrase in phrases:
            key = replies.key(phrase)
            path = os.path.join(out_dir, key + ".wav")
            if force or not os.path.exists(path):
                try:
                    audio = voice_dialog.synthesize(phrase)
                except Exception as e:  # noqa: BLE001
                    print(f"  ОШИБКА {name:5} {phrase!r}: {e}")
                    failed.append(phrase)
                    continue
                with open(path, "wb") as f:
                    f.write(audio)
                made += 1
            else:
                kept += 1
            with open(path, "rb") as f:
                audio = f.read()
            with wave.open(io.BytesIO(audio), "rb") as w:
                seconds = round(w.getnframes() / w.getframerate(), 2)
            index[key] = {"phrase": phrase, "pool": name, "file": key + ".wav",
                          "voice": replies.voice(), "seconds": seconds,
                          "bytes": len(audio)}
            print(f"  {name:5} {seconds:4.2f} с  {key}.wav  {phrase}")

    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"voice": replies.voice(),
                   "phrases": [index[k] for k in sorted(index)]},
                  f, ensure_ascii=False, indent=2)
    print(f"\nреплик: {len(index)} | синтезировано сейчас: {made} | уже были: {kept} | не вышло: {len(failed)}")
    print("каталог:", out_dir)
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="перезаписать все записи")
    args = ap.parse_args()
    sys.exit(main(args.force))
