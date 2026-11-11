#!/usr/bin/env python3
"""Разбор кольца: что слышала колонка в указанное время.

Хаб пишет последний час сырого потока в `/srv/kws/ring/<колонка>-<epoch>.raw.pcm`.
Когда человек говорит «в 14:27 не сработало», этот скрипт вырезает окно,
прогоняет детектор и печатает пики и текст вокруг них — то есть показывает,
был ли пик ниже порога, было ли это вообще обращение и что услышал ASR.

Примеры:

    # всё за минуту до и минуту после 15:20 МСК
    python3 ring-probe.py --at "2026-11-03 15:20"

    # только пики детектора, без распознавания
    python3 ring-probe.py --at "2026-11-03 15:20" --no-asr

    # что вообще есть на диске
    python3 ring-probe.py --list
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SR = 16000
BPS = SR * 2
HUB = Path(__file__).resolve().parents[2] / "hub"
sys.path.insert(0, str(HUB))

from voice_capture import RetroScanner, crop_around  # noqa: E402
from voice_loop import transcribe  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")


def parse_at(text: str) -> float:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        now = datetime.now(MSK)
        if fmt in ("%H:%M:%S", "%H:%M"):
            dt = now.replace(hour=dt.hour, minute=dt.minute,
                             second=dt.second, microsecond=0)
        else:
            dt = dt.replace(tzinfo=MSK)
        return dt.timestamp()
    raise SystemExit(f"не понял время: {text!r} (нужно 'ГГГГ-ММ-ДД ЧЧ:ММ[:СС]' или 'ЧЧ:ММ')")


def segments(root: Path, name: str, t0: float, t1: float):
    """Файлы кольца, пересекающие [t0, t1], по возрастанию времени."""
    out = []
    for f in sorted(root.glob(f"{name}-*.raw.pcm")):
        try:
            started = int(f.name[len(name) + 1:-len(".raw.pcm")]) / 1000.0
        except ValueError:
            continue
        size = f.stat().st_size
        ended = started + size / BPS
        if ended >= t0 and started <= t1:
            out.append((started, ended, size, f))
    return out


def load_window(root: Path, name: str, t0: float, t1: float) -> bytes:
    parts = []
    for started, ended, size, f in segments(root, name, t0, t1):
        a = max(0, int((t0 - started) * BPS))
        b = min(size, int((t1 - started) * BPS))
        a -= a % 2
        b -= b % 2
        if b <= a:
            continue
        with open(f, "rb") as fh:
            fh.seek(a)
            parts.append(fh.read(b - a))
    return b"".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", help="время МСК: 'ГГГГ-ММ-ДД ЧЧ:ММ[:СС]' или 'ЧЧ:ММ'")
    ap.add_argument("--dir", default=os.environ.get("KWS_RING_DIR", "/srv/kws/ring"))
    ap.add_argument("--name", default="lite-1", help="имя колонки (по умолчанию lite-1)")
    ap.add_argument("--span", type=float, default=60.0, help="окно вокруг времени, с")
    ap.add_argument("--threshold", type=float, default=0.30,
                    help="минимальный пик для показа (по умолчанию 0.30)")
    ap.add_argument("--no-asr", action="store_true", help="не звать распознавание")
    ap.add_argument("--list", action="store_true", help="показать файлы кольца")
    args = ap.parse_args()

    root = Path(args.dir)
    if args.list or not args.at:
        files = sorted(root.glob(f"{args.name}-*.raw.pcm"))
        if not files:
            print(f"в {root} нет файлов {args.name}-*.raw.pcm")
            return 1
        total = sum(f.stat().st_size for f in files)
        for f in files:
            started = int(f.name[len(args.name) + 1:-len(".raw.pcm")]) / 1000.0
            size = f.stat().st_size
            print(f"{datetime.fromtimestamp(started, MSK):%Y-%m-%d %H:%M:%S}  "
                  f"{size / 1048576:6.1f} МБ  {size / BPS / 60:5.1f} мин  {f.name}")
        print(f"итого {len(files)} файлов, {total / 1048576:.1f} МБ")
        return 0

    at = parse_at(args.at)
    t0, t1 = at - args.span / 2, at + args.span / 2
    pcm = load_window(root, args.name, t0, t1)
    if not pcm:
        print(f"за {datetime.fromtimestamp(t0, MSK):%H:%M:%S}–"
              f"{datetime.fromtimestamp(t1, MSK):%H:%M:%S} записи нет")
        return 1

    dur = len(pcm) / BPS
    print(f"окно {datetime.fromtimestamp(t0, MSK):%Y-%m-%d %H:%M:%S} + {args.span:.0f} с, "
          f"звука {dur:.1f} с")

    scanner = RetroScanner()
    if not scanner.available:
        print("детектор не загрузился (pymicro_wakeword)")
        return 2
    peaks = scanner.scan(pcm, threshold=args.threshold)
    if not peaks:
        print(f"пиков выше {args.threshold:.2f} нет")
        return 0

    print(f"\n{'время':<10} {'сек':>6} {'пик':>6}  текст")
    for t, p in peaks[:15]:
        stamp = datetime.fromtimestamp(t0 + t, MSK).strftime("%H:%M:%S")
        text = ""
        if not args.no_asr:
            try:
                text = transcribe(crop_around(pcm, t))
            except Exception as e:  # noqa: BLE001
                text = f"<ASR: {e}>"
        mark = "  ← порог 0.95" if p >= 0.95 else ""
        print(f"{stamp:<10} {t:6.1f} {p:6.3f}  {text}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
