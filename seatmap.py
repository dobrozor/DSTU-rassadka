"""Чтение мест из SVG-схемы зала.

Из <circle data-z data-r data-s data-x data-y> строится словарь
{(зона, ряд, место): (x, y)}. Координаты x/y — абсолютные (в системе viewBox),
именно их использует и фронтенд для зума и выделения.

Какие залы существуют и какой файл у какого — см. halls.py.
"""
import pathlib
import re

_CIRCLE = re.compile(r"<circle\b([^>]*)>")
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
_VIEWBOX = re.compile(r'<svg\b[^>]*\bviewBox="([^"]+)"')


def load_svg(path):
    """→ (текст svg, {(зона, ряд, место): (x, y)}, (ширина, высота) из viewBox)."""
    svg = pathlib.Path(path).read_text(encoding="utf-8")
    seats = {}
    for m in _CIRCLE.finditer(svg):
        a = dict(_ATTR.findall(m.group(1)))
        if "data-z" not in a:
            continue
        for need in ("data-s", "data-x", "data-y"):
            if need not in a:
                raise ValueError(f"{path}: у места {a.get('data-z')} нет атрибута {need}")
        key = (a["data-z"], a.get("data-r", ""), a["data-s"])
        if key in seats:
            raise ValueError(f"{path}: дубликат места в схеме: {key}")
        seats[key] = (float(a["data-x"]), float(a["data-y"]))
    if not seats:
        raise ValueError(f"{path}: в SVG не найдено ни одного места (circle[data-z])")
    vb = _VIEWBOX.search(svg)
    if not vb:
        raise ValueError(f"{path}: у <svg> нет атрибута viewBox")
    parts = vb.group(1).replace(",", " ").split()
    if len(parts) != 4 or float(parts[0]) != 0 or float(parts[1]) != 0:
        raise ValueError(f"{path}: viewBox должен быть вида «0 0 ширина высота», а не «{vb.group(1)}»")
    return svg, seats, (float(parts[2]), float(parts[3]))
