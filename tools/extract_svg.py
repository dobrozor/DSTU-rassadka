"""Достаёт <svg>…</svg> из «Схема зала.html» в data/<имя>.svg.

Запуск:  python tools/extract_svg.py "путь/к/Схема зала.html" [имя_файла.svg]
Без имени — перезаписывает data/hall.svg (Конгресс-холл). Для нового зала укажите своё имя
и затем зарегистрируйте файл в halls.py. Приложение читает готовые файлы из data/.
"""
import pathlib
import re
import sys

if len(sys.argv) < 2:
    sys.exit(__doc__)
src = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
m = re.search(r"<svg\b.*?</svg>", src, re.S)
if not m:
    sys.exit("<svg> не найден")
name = sys.argv[2] if len(sys.argv) > 2 else "hall.svg"
out = pathlib.Path(__file__).resolve().parent.parent / "data" / name
out.write_text(m.group(0), encoding="utf-8")
print(f"OK: {out} ({len(m.group(0)):,} байт)")
