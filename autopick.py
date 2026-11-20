"""Авто-подбор лучшего свободного блока мест.

Идея. Зал переводится в сетку «ряд × колонка» по геометрии SVG (а не по номерам
мест: нумерация в ряду может идти в любую сторону, а проходы — это дырки в сетке).
Дальше перебираются «окна»: полоса из k соседних рядов × интервал колонок.
Из свободных мест окна берутся ровно n (лишние срезаются с краёв, сначала с
дальних рядов), и каждый вариант получает штраф — чем он меньше, тем лучше:

    ROW_W    · (число рядов − 1)        держим группу в как можно меньшем числе рядов
    BREAK_W  · разрывов внутри ряда     проход/занятое место посреди группы
    HOLE_W   · пропущенных клеток       то же, но по числу клеток
    ALIGN_W  · «ступенчатость»          края соседних рядов должны совпадать
    ORPHAN_W · одиночных мест-сирот     не оставляем одно свободное место у края блока
    + мелкие поправки: ближе к центру, ближе к сцене, партер предпочтительнее балкона

Возвращается несколько заметно разных вариантов (для кнопки «Другой вариант»).
Если цельного блока нет — пробуем разбить группу на 2–4 части (find_split).
"""
from collections import defaultdict
from dataclasses import dataclass
from math import ceil

PITCH = 20.0          # шаг между местами по x в схеме
ROW_LINK = 30.0       # ряды ближе этого по y считаются соседними (между 9 и 10 рядом партера — проход 60)
MAX_ROWS = 10         # максимум рядов в одном блоке
EXTRA_W = 8           # насколько окно может быть шире минимально необходимого

ROW_W, BREAK_W, HOLE_W, ALIGN_W, ORPHAN_W = 3.0, 4.0, 0.7, 0.25, 1.5
CENTER_W, FRONT_W = 0.03, 0.08
ZONE_BIAS = {"Партер": 0.0, "Балкон": 1.5}   # прочие зоны — 3.0
DEFAULT_ZONES = ("Партер", "Балкон")


class _Zone:
    def __init__(self, name, items, pitch=PITCH, row_link=ROW_LINK):
        self.name = name
        xmin = min(x for _, x, _ in items)
        ys = sorted({round(y) for _, _, y in items}, reverse=True)  # y больше → ближе к сцене
        ridx = {y: i for i, y in enumerate(ys)}
        self.ys = ys
        self.cells = [dict() for _ in ys]                            # ряд → {колонка: ключ места}
        for key, x, y in items:
            self.cells[ridx[round(y)]][round((x - xmin) / pitch)] = key
        self.ncols = max(max(c) for c in self.cells if c) + 1
        allcols = [c for row in self.cells for c in row]
        self.center = (min(allcols) + max(allcols)) / 2
        self.linked = [ys[i] - ys[i + 1] <= row_link for i in range(len(ys) - 1)]


@dataclass
class Block:
    keys: list      # [(zone, row, seat), ...] в порядке ряд → колонка
    score: float


class Grid:
    """Сетка зала. pitch/row_link/zone_bias — свои у каждого зала (см. halls.py);
    значения по умолчанию соответствуют Конгресс-холлу."""

    def __init__(self, seats, zone_bias=None, default_bias=3.0, pitch=PITCH, row_link=ROW_LINK):
        by_zone = defaultdict(list)
        for key, (x, y) in seats.items():
            by_zone[key[0]].append((key, x, y))
        self.zones = {name: _Zone(name, items, pitch, row_link) for name, items in by_zone.items()}
        self.zone_bias = ZONE_BIAS if zone_bias is None else zone_bias
        self.default_bias = default_bias


def find_blocks(grid, occupied, n, zones=DEFAULT_ZONES, top=5):
    """Лучшие блоки из n свободных мест. occupied — множество занятых ключей."""
    found = {}
    for zname in zones:
        z = grid.zones.get(zname)
        if z is None or n < 1:
            continue
        nrows = len(z.cells)
        free_sets, pref = [], []
        for row in z.cells:
            fs = {c for c, key in row.items() if key not in occupied}
            free_sets.append(fs)
            p, acc = [0], 0
            for c in range(z.ncols):
                acc += c in fs
                p.append(acc)
            pref.append(p)
        bias = grid.zone_bias.get(zname, grid.default_bias)

        for r0 in range(nrows):
            for k in range(1, min(n, MAX_ROWS) + 1):
                r1 = r0 + k - 1
                if r1 >= nrows or (k > 1 and not z.linked[r1 - 1]):
                    break
                w0 = ceil(n / k)
                for w in range(w0, min(w0 + EXTRA_W, z.ncols) + 1):
                    for c0 in range(z.ncols - w + 1):
                        c1 = c0 + w
                        counts = [pref[r][c1] - pref[r][c0] for r in range(r0, r1 + 1)]
                        extra = sum(counts) - n
                        # extra >= k: окно можно сузить на колонку и мест всё равно хватит —
                        # такой вариант перекрыт более узким окном, не считаем его.
                        if extra < 0 or extra >= k or min(counts) == 0:
                            continue
                        sel = _take(z, free_sets, r0, r1, c0, c1 - 1, n, extra)
                        keys = tuple(z.cells[r][c] for r in sorted(sel) for c in sel[r])
                        score = _score(z, sel, free_sets, r0) + bias
                        if keys not in found or score < found[keys]:
                            found[keys] = score

    blocks = []
    for keys, score in sorted(found.items(), key=lambda kv: kv[1]):
        ks = set(keys)
        if all(len(ks & set(b.keys)) / n <= 0.5 for b in blocks):   # варианты должны заметно отличаться
            blocks.append(Block(list(keys), round(score, 2)))
            if len(blocks) >= top:
                break
    return blocks


def _take(z, free_sets, r0, r1, c0, c1, n, extra):
    """Свободные места окна; лишние (extra штук) срезаются с краёв рядов."""
    sel = {r: sorted(c for c in free_sets[r] if c0 <= c <= c1) for r in range(r0, r1 + 1)}
    center = (c0 + c1) / 2
    for _ in range(extra):
        best = None
        for r, cols in sel.items():
            for idx in ({0, len(cols) - 1} if cols else ()):
                w = abs(cols[idx] - center) + 0.5 * (r - r0)   # дальние ряды срезаем охотнее
                if best is None or w > best[0]:
                    best = (w, r, idx)
        _, r, idx = best
        sel[r].pop(idx)
    return {r: cols for r, cols in sel.items() if cols}


def _score(z, sel, free_sets, r0):
    rows = sorted(sel)
    s = ROW_W * (len(rows) - 1)
    prev, allc = None, []
    for r in rows:
        cols = sel[r]
        lo, hi = cols[0], cols[-1]
        s += HOLE_W * ((hi - lo + 1) - len(cols))
        s += BREAK_W * sum(1 for a, b in zip(cols, cols[1:]) if b - a > 1)
        if prev:
            s += ALIGN_W * (abs(lo - prev[0]) + abs(hi - prev[1]))
        prev = (lo, hi)
        for side, edge in ((-1, lo), (1, hi)):                # сироты у краёв блока
            if (edge + side in free_sets[r]) and (edge + 2 * side not in free_sets[r]):
                s += ORPHAN_W
        allc += cols
    s += CENTER_W * abs(sum(allc) / len(allc) - z.center)
    s += FRONT_W * (sum(rows) / len(rows))
    return s


def find_split(grid, occupied, n, zones=DEFAULT_ZONES, max_parts=4):
    """Если цельного блока нет: n → 2..max_parts блока примерно равного размера.
    Возвращает список Block или None."""
    for parts in range(2, min(max_parts, n) + 1):
        sizes = [n // parts + (1 if i < n % parts else 0) for i in range(parts)]
        taken, result = set(occupied), []
        for sz in sorted(sizes, reverse=True):
            b = find_blocks(grid, taken, sz, zones, top=1)
            if not b:
                break
            result.append(b[0])
            taken |= set(b[0].keys)
        else:
            return result
    return None
