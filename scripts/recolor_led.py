#!/usr/bin/env python3
"""Перекраска .led-паттерна колонки с сохранением движения и яркости.

Формат файла: первая строка `loop`, дальше строки
`RRGGBB RRGGBB RRGGBB длительность` — три сегмента кольца и время кадра.

Меняем только оттенок (hue), насыщенность и яркость остаются от исходника,
поэтому дыхание и бегущий огонёк выглядят как раньше, а цвет — свой.
"""
import colorsys
import sys

HUE_DEGREES = {"teal": 166, "amber": 38, "red": 0, "violet": 270, "blue": 212, "lime": 120, "magenta": 330}


def recolor_line(line: str, hue: float) -> str:
    parts = line.split()
    if len(parts) != 4 or line.startswith("loop"):
        return line
    out = []
    for hex_colour in parts[:3]:
        r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (0, 2, 4))
        _, s, v = colorsys.rgb_to_hsv(r, g, b)
        if v == 0:
            out.append("000000")
            continue
        nr, ng, nb = colorsys.hsv_to_rgb(hue / 360.0, s, v)
        out.append("%02x%02x%02x" % (int(nr * 255), int(ng * 255), int(nb * 255)))
    return " ".join(out + [parts[3]])


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: recolor_led.py <source.led> <colour> <out.led>", file=sys.stderr)
        print("colours:", ", ".join(HUE_DEGREES), file=sys.stderr)
        return 2
    src, colour, dst = sys.argv[1], sys.argv[2], sys.argv[3]
    hue = HUE_DEGREES.get(colour)
    if hue is None:
        hue = float(colour)
    with open(src) as f:
        lines = f.read().splitlines()
    out = [recolor_line(line, hue) for line in lines]
    with open(dst, "w") as f:
        f.write("\n".join(out) + "\n")

    colours = [c for line in out if not line.startswith("loop")
               for c in line.split()[:3]]
    peak = max(colours, key=lambda h: sum(int(h[i:i + 2], 16) for i in (0, 2, 4)))
    print(f"{dst}: {len(out)} строк, пик {peak}, оттенок {hue}°")
    return 0


if __name__ == "__main__":
    sys.exit(main())
