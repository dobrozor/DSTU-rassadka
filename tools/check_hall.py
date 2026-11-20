"""Проверка схемы зала перед подключением / после правки.

    python tools/check_hall.py              # все залы из halls.py
    python tools/check_hall.py small_hall   # один зал

Показывает зоны, число мест и рядов, подсказывает pitch и row_link для автоподбора
и делает пробный подбор на пустом зале.
"""
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autopick as ap  # noqa: E402
import halls  # noqa: E402


def check(hall):
    print(f"\n══ {hall.name}  (ключ «{hall.key}», {hall.svg_path.name}) ══")
    print(f"viewBox 0 0 {hall.width:g} {hall.height:g} · мест всего: {hall.total}")
    warns = []
    for z in hall.zones:
        items = [(k, *hall.seats[k]) for k in hall.seats if k[0] == z]
        rows = defaultdict(list)
        for k, x, y in items:
            rows[round(y)].append(x)
        extra = "" if hall.zone_rows[z] else " · без номеров рядов"
        flag = " (по умолчанию)" if z in hall.default_zones else ""
        print(f"  {z}{flag}: {len(items)} мест, {len(rows)} линий по y{extra}")
        gaps = [b - a for r in rows.values() for a, b in zip(sorted(r), sorted(r)[1:]) if b - a > 0.5]
        if gaps:
            print(f"     шаг между местами по x (медиана): {statistics.median(gaps):g}  ← сравните с pitch в halls.py")
        ys = sorted(rows, reverse=True)
        dy = [a - b for a, b in zip(ys, ys[1:])]
        if dy:
            small = min(dy)
            aisles = sorted({d for d in dy if d > small * 1.5})
            print(f"     расстояние между рядами: {small:g}" + (f", проходы: {', '.join(f'{a:g}' for a in aisles)}" if aisles else ""))
            if aisles:
                print(f"     → row_link между {small:g} и {aisles[0]:g}, например {round((small + aisles[0]) / 2)}")
        out = [k for k, x, y in items if not (0 <= x <= hall.width and 0 <= y <= hall.height)]
        if out:
            warns.append(f"{z}: {len(out)} мест за пределами viewBox")
    for z in hall.zones:
        if not hall.zone_rows[z]:
            continue
        no_num = [k for k in hall.seats if k[0] == z and not k[1].isdigit()]
        if no_num:
            warns.append(f"{z}: у {len(no_num)} мест нечисловой ряд — цены «по рядам» к ним не применятся")
    # пробный подбор
    for n in (2, 10, 30):
        if n <= hall.total:
            b = ap.find_blocks(hall.grid, set(), n, hall.default_zones, top=1)
            print(f"  пробный подбор {n} мест на пустом зале: " + (f"ок ({len({k[:2] for k in b[0].keys})} ряд.)" if b else "ничего не нашлось — проверьте pitch/row_link"))
    print("  Проверка пройдена" if not warns else "  ВНИМАНИЕ:\n   - " + "\n   - ".join(warns))


if __name__ == "__main__":
    keys = sys.argv[1:] or [h.key for h in halls.all_halls()]
    for key in keys:
        try:
            check(halls.get(key))
        except KeyError:
            sys.exit(f"Зал «{key}» не зарегистрирован в halls.py. Есть: {[h.key for h in halls.all_halls()]}")
