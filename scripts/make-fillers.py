#!/usr/bin/env python3
"""Заготовки фраз в репозиторий: синтезируем один раз и кладём файлами в voice-fillers/<голос>/.

Зачем: фраза-заготовка должна звучать мгновенно и не зависеть от того, поднят ли
синтез на 139. Файл в проекте — это готовая запись: её видно в git, её не надо
синтезировать ни при первом запуске, ни после чистки кэша.

Запуск на сервере панели:
    /usr/bin/python3 scripts/panel/make-fillers.py            # доложить отсутствующие
    /usr/bin/python3 scripts/panel/make-fillers.py --force    # перезаписать все
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sys
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("voice_server", os.path.join(ROOT, "server.py"))
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


async def main(force: bool) -> int:
    out_dir = os.path.join(S.FILLER_REPO_DIR, S.TTS_VOICE)
    os.makedirs(out_dir, exist_ok=True)
    index: dict[str, dict] = {}
    made, kept, failed = 0, 0, []
    for kind, pool in (("think", S.FILLER_THINK), ("tool", S.FILLER_TOOL)):
        for phrase in pool:
            key = S.filler_key(phrase)
            name = key + ".wav"
            path = os.path.join(out_dir, name)
            if force or not os.path.exists(path):
                audio = await S._tts_once(phrase)
                if not audio:
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
            index[key] = {"phrase": phrase, "kind": kind, "file": name,
                          "voice": S.TTS_VOICE, "seconds": seconds, "bytes": len(audio)}
            print(f"  {kind:5} {seconds:4.2f} с  {name}  {phrase}")
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"voice": S.TTS_VOICE, "model": S.TTS_MODEL,
                   "phrases": [index[k] for k in sorted(index, key=lambda k: (index[k]['kind'], index[k]['phrase']))]},
                  f, ensure_ascii=False, indent=2, sort_keys=False)
    print(f"\nзаготовок: {len(index)} | синтезировано сейчас: {made} | уже были: {kept} | не вышло: {len(failed)}")
    print("каталог:", out_dir)
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="перезаписать все заготовки")
    args = ap.parse_args()
    sys.exit(__import__("asyncio").run(main(args.force)))
