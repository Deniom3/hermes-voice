#!/usr/bin/env python3
"""Обслуживание собранных событий ключевого слова.

Правила:
  * positives — ценные, держим до лимита 500 МБ, дальше удаляем самые старые;
  * negatives — 30 дней;
  * unclear   — 7 дней (это в основном обрывки, они быстро теряют ценность);
  * пустые/битые wav и осиротевшие json удаляются всегда.

Поток станции этот скрипт не трогает: поток больше не пишется на диск,
здесь только каталог живых событий. Замороженные версии в `versions/`
не трогаются вообще: это артефакты, по ним уже обучали.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

DEFAULT_ROOT = "/srv/kws/train"
POSITIVES_LIMIT = 500 * 1024 * 1024
RETENTION_DAYS = {"negatives": 30, "unclear": 7}


def file_age_days(path: str) -> float:
    return (time.time() - os.path.getmtime(path)) / 86400.0


def total_size(paths: list[str]) -> int:
    return sum(os.path.getsize(p) for p in paths if os.path.exists(p))


def clean(root: str, dry: bool = False) -> dict:
    stats = {"удалено_файлов": 0, "освобождено_мб": 0.0, "по_классам": {}}
    for cls in ("positives", "negatives", "unclear"):
        d = os.path.join(root, cls)
        if not os.path.isdir(d):
            continue
        removed = 0
        freed = 0
        removed_stems = set()

        # пустые и битые wav
        for name in os.listdir(d):
            if not name.endswith(".wav"):
                continue
            p = os.path.join(d, name)
            try:
                if os.path.getsize(p) < 1000:
                    removed += 1
                    freed += os.path.getsize(p)
                    removed_stems.add(name[:-4])
                    if not dry:
                        os.remove(p)
            except OSError:
                continue

        # ретенция по возрасту
        days = RETENTION_DAYS.get(cls)
        if days:
            for name in os.listdir(d):
                if not name.endswith(".wav"):
                    continue
                p = os.path.join(d, name)
                try:
                    if file_age_days(p) > days:
                        removed += 1
                        freed += os.path.getsize(p)
                        removed_stems.add(name[:-4])
                        if not dry:
                            os.remove(p)
                except OSError:
                    continue

        # positives: лимит по объёму, удаляем самые старые
        if cls == "positives":
            wavs = sorted(
                (os.path.join(d, n) for n in os.listdir(d) if n.endswith(".wav")),
                key=lambda p: os.path.getmtime(p),
            )
            size = total_size(wavs)
            for p in wavs:
                if size <= POSITIVES_LIMIT:
                    break
                try:
                    sz = os.path.getsize(p)
                    size -= sz
                    removed += 1
                    freed += sz
                    removed_stems.add(os.path.basename(p)[:-4])
                    if not dry:
                        os.remove(p)
                except OSError:
                    continue

        # осиротевшие json
        for name in os.listdir(d):
            if name.endswith(".json") and name[:-5] in removed_stems:
                p = os.path.join(d, name)
                try:
                    freed += os.path.getsize(p)
                    if not dry:
                        os.remove(p)
                except OSError:
                    pass

        stats["по_классам"][cls] = {
            "файлов": sum(1 for n in os.listdir(d) if n.endswith(".wav")),
            "мб": round(total_size([os.path.join(d, n) for n in os.listdir(d)
                                    if n.endswith(".wav")]) / 1e6, 1),
            "удалено": removed,
        }
        stats["удалено_файлов"] += removed
        stats["освобождено_мб"] += freed / 1e6
    stats["освобождено_мб"] = round(stats["освобождено_мб"], 1)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not os.path.isdir(args.root):
        print(json.dumps({"error": f"нет каталога {args.root}"}, ensure_ascii=False))
        return 1
    stats = clean(args.root, dry=args.dry_run)
    stats["dry_run"] = args.dry_run
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
