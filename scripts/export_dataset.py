#!/usr/bin/env python3
"""Выгрузка данных обучения: заморозка живого слоя в версию.

Живые события хаб пишет сразу в готовом виде — `/srv/kws/train/{positives,negatives,unclear}`.
Отдельной ежедневной сборки нет: фильтры, нормализация и дедуп выполняются в
момент записи. Скрипт нужен ровно для одного — сделать неизменяемый снимок
(версию) тогда, когда данные понадобились. По версии видно, на чём именно
обучали, даже если живой слой с тех пор изменился.

Раскладка:
  /srv/kws/train/{positives,negatives,unclear}   живой слой, пополняется сам
  /srv/kws/train/versions/<имя>/                 версии (снимки), их не чистят

Что попадает в набор (правила, согласованные при разборе датасета):

  * positives — только если в тексте действительно есть обращение (name_like).
    Окно без текста в positives — брак классификации, ему там не место.
  * negatives — только hard-негативы: детектор сработал, обращения нет
    (`reason=no_name`), и score не ниже порога. Это самое ценное: акустически
    похоже на имя, имени не было.
  * unclear — по умолчанию НЕ берём: обрывки не размечены и учат модель мусору.
    Нужны для разбора — есть флаг --with-unclear.

Что отбрасывается всегда (и попадает в отчёт с причиной):

  * тишина и слишком тихое окно (active_rms < 8, peak < 40);
  * клиппинг (peak >= 32000) — такой клип нельзя нормализовать без искажений;
  * короткое окно (< 1.2 с) — не хватает контекста;
  * дубли по md5 внутри партии и (по умолчанию) уже собранные ранее наборы;
  * negatives с score ниже порога.

Громкость выравнивается по активной речи, а не по общему RMS: на общем RMS
нормализация клипует согласные. Если окно уже в норме (±20 % от цели), файл
переносится как есть, чтобы не накапливать повторные пересчёты.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "station"))

from voice_capture import (  # noqa: E402
    SR,
    TARGET_RMS,
    active_rms,
    normalize,
)
from voice_core import name_like  # noqa: E402

CLASSES = ("positives", "negatives", "misses")
CLASSES_WITH_UNCLEAR = ("positives", "negatives", "misses", "unclear")
# Куда кладём класс при экспорте. `misses` — это фразы, где обращение
# прозвучало, но детектор не добрал до порога: для обучения это positives,
# иначе именно те случаи, из-за которых модель не срабатывает, в набор
# не попадают. Исходный класс сохраняется в meta как source_class.
TARGET_CLASS = {"misses": "positives"}
MIN_SECONDS = 1.2
MIN_ACTIVE_RMS = 8.0
MIN_PEAK = 40
CLIP_PEAK = 32000
RMS_TOLERANCE = 0.2


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def read_wav(path: str) -> np.ndarray | None:
    """Прочитать wav как int16 mono. Чужой формат — None (в набор не берём)."""
    try:
        with wave.open(path, "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                return None
            sr = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    except (OSError, wave.Error):
        return None
    if sr != SR or pcm.size == 0:
        return None
    return pcm


def event_files(root: str, cls: str) -> list[str]:
    """wav-файлы класса, у которых есть sidecar с метаданными."""
    out = []
    for wav in sorted(glob.glob(os.path.join(root, cls, "*.wav"))):
        meta_path = wav[:-4] + ".json"
        if os.path.exists(meta_path):
            out.append(wav)
    return out


def load_meta(wav: str) -> dict:
    try:
        with open(wav[:-4] + ".json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def reject_reason(cls: str, pcm: np.ndarray, meta: dict, min_score: float) -> str | None:
    """Почему окно не годится в набор. None — годится."""
    sec = len(pcm) / SR
    if sec < MIN_SECONDS:
        return f"короткое окно ({sec:.2f} с < {MIN_SECONDS})"
    peak = int(np.abs(pcm.astype(np.int32)).max())
    if peak < MIN_PEAK:
        return "тишина"
    if peak >= CLIP_PEAK:
        return "клиппинг"
    if active_rms(pcm.astype(np.float32)) < MIN_ACTIVE_RMS:
        return "нет активной речи"

    text = (meta.get("text") or "").strip()
    if cls in ("positives", "misses"):
        if not text:
            return f"{cls} без текста"
        if not name_like(text):
            return f"{cls} без обращения в тексте"
    elif cls == "negatives":
        if meta.get("reason") != "no_name":
            return f"negatives с чужой причиной ({meta.get('reason')})"
        try:
            score = float(meta.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score < min_score:
            return f"negatives со слабым score ({score:.3f} < {min_score})"
    return None


def level_ok(pcm: np.ndarray) -> bool:
    """Громкость уже в норме — пересчитывать не надо."""
    ra = active_rms(pcm.astype(np.float32))
    return abs(ra - TARGET_RMS) <= TARGET_RMS * RMS_TOLERANCE


def fingerprints(pcm: np.ndarray) -> set[str]:
    """Отпечатки окна: как есть и в канонической (выровненной) форме.

    Нужны оба: в наборе файл уже выровнен, а на складе исходник ещё тихий,
    и без канонической формы дубль прошёл бы как новое событие.
    """
    marks = {md5_bytes(pcm.tobytes())}
    canon = normalize(pcm)
    if canon is not None:
        marks.add(md5_bytes(canon.tobytes()))
    return marks


def existing_md5(out_root: str) -> set[str]:
    """Отпечатки уже разложенных окон: не тащим дубли в новые наборы.

    Считаем по PCM, а не по файлу: у WAV-контейнера свой заголовок, и md5
    файла не совпал бы с md5 сырых данных, то есть дубль прошёл бы.
    """
    seen = set()
    for path in glob.glob(os.path.join(out_root, "*", "*", "*.wav")):
        pcm = read_wav(path)
        if pcm is not None:
            seen |= fingerprints(pcm)
    return seen


def export(root: str, out_root: str, name: str, min_score: float,
          with_unclear: bool, exclude_existing: bool, dry: bool) -> dict:
    classes = CLASSES_WITH_UNCLEAR if with_unclear else CLASSES
    skip_existing = existing_md5(out_root) if exclude_existing else set()
    seen = set()
    skipped: dict[str, int] = {}
    items: list[dict] = []
    counts: dict[str, int] = {TARGET_CLASS.get(c, c): 0 for c in classes}
    target_dir = os.path.join(out_root, name)

    for cls in classes:
        dst_cls = TARGET_CLASS.get(cls, cls)
        for wav in event_files(root, cls):
            meta = load_meta(wav)
            pcm = read_wav(wav)
            if pcm is None:
                skipped["нечитаемый wav (не 16 кГц mono 16 бит)"] = \
                    skipped.get("нечитаемый wav (не 16 кГц mono 16 бит)", 0) + 1
                continue
            reason = reject_reason(cls, pcm, meta, min_score)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue

            marks = fingerprints(pcm)
            digest = md5_bytes(pcm.tobytes())
            if marks & seen:
                skipped["дубль в партии"] = skipped.get("дубль в партии", 0) + 1
                continue
            if marks & skip_existing:
                skipped["уже есть в собранных наборах"] = \
                    skipped.get("уже есть в собранных наборах", 0) + 1
                continue
            seen |= marks

            out_pcm = pcm
            relit = False
            if not level_ok(pcm):
                out_pcm = normalize(pcm)
                if out_pcm is None:
                    skipped["не нормализуется (усиление вне пределов)"] = \
                        skipped.get("не нормализуется (усиление вне пределов)", 0) + 1
                    continue
                relit = True

            dst_dir = os.path.join(target_dir, dst_cls)
            dst = os.path.join(dst_dir, os.path.basename(wav))
            if not dry:
                os.makedirs(dst_dir, exist_ok=True)
                if relit:
                    with wave.open(dst, "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(SR)
                        w.writeframes(out_pcm.tobytes())
                else:
                    shutil.copy2(wav, dst)
                out_meta = dict(meta)
                if cls != dst_cls:
                    # Откуда взят клип: misses → positives. Нужно, чтобы
                    # можно было отличить добавленные промахи от срабатываний.
                    out_meta["source_class"] = cls
                with open(dst[:-4] + ".json", "w", encoding="utf-8") as f:
                    json.dump(out_meta, f, ensure_ascii=False, indent=1)

            counts[dst_cls] += 1
            items.append({
                "file": f"{dst_cls}/{os.path.basename(wav)}",
                "sec": round(len(out_pcm) / SR, 2),
                "rms": round(active_rms(out_pcm.astype(np.float32))),
                "peak": int(np.abs(out_pcm.astype(np.int32)).max()),
                "score": meta.get("score"),
                "reason": meta.get("reason"),
                "text": meta.get("text") or "",
                "level_fixed": relit,
                "md5": digest,
            })

    manifest = {
        "name": name,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": root,
        "with_unclear": with_unclear,
        "min_score": min_score,
        "counts": counts,
        "total": sum(counts.values()),
        "skipped": skipped,
        "items": items,
    }
    if not dry:
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="Собрать обучающий набор из событий ключевого слова")
    ap.add_argument("--root", default="/srv/kws/train", help="живой слой событий")
    ap.add_argument("--out", default="", help="куда класть версии (по умолчанию <root>/versions)")
    ap.add_argument("--name", default=time.strftime("%Y%m%d-%H%M-export"), help="имя версии")
    ap.add_argument("--min-score", type=float, default=0.9,
                    help="минимальный score для негативов (по умолчанию 0.9)")
    ap.add_argument("--with-unclear", action="store_true",
                    help="включить unclear (по умолчанию не берём)")
    ap.add_argument("--no-exclude-existing", action="store_true",
                    help="не сверяться с уже собранными наборами")
    ap.add_argument("--tar", action="store_true", help="сложить версию в tar.gz рядом")
    ap.add_argument("--dry-run", action="store_true", help="только отчёт, ничего не писать")
    args = ap.parse_args()
    if not args.out:
        args.out = os.path.join(args.root, "versions")

    manifest = export(args.root, args.out, args.name, args.min_score,
                     args.with_unclear, not args.no_exclude_existing, args.dry_run)

    print(f"версия: {args.name}{' (пробный прогон)' if args.dry_run else ''}")
    for cls, n in manifest["counts"].items():
        print(f"  {cls:10} {n}")
    print(f"  всего      {manifest['total']}")
    if manifest["skipped"]:
        print("отброшено:")
        for reason, n in sorted(manifest["skipped"].items(), key=lambda kv: -kv[1]):
            print(f"  {n:4}  {reason}")
    if not args.dry_run:
        print(f"манифест: {os.path.join(args.out, args.name, 'manifest.json')}")
        if args.tar:
            import tarfile
            archive = os.path.join(args.out, args.name + ".tar.gz")
            with tarfile.open(archive, "w:gz") as t:
                t.add(os.path.join(args.out, args.name), arcname=args.name)
            print(f"архив: {archive} ({os.path.getsize(archive) / 1048576:.1f} МБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
