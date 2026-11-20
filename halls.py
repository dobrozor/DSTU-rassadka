"""Реестр залов. Единственное место, где перечислены все залы.

Как добавить зал
----------------
1. Положите SVG-схему в data/ (например data/small_hall.svg). Требования к схеме те же, что
   у data/hall.svg: каждое место — <circle data-z="зона" data-r="ряд" data-s="место"
   data-x="…" data-y="…">, viewBox начинается с «0 0».
   ('data-r' можно не указывать у зон без рядов, например боковые балконы.)
2. Добавьте блок register(...) внизу этого файла.
3. Проверьте схему:  python tools/check_hall.py small_hall
4. Перезапустите приложение. Зал сразу появится в окне «Новый сеанс».

Зал, под которым уже созданы сеансы, нельзя удалять из реестра и нельзя менять у него
названия зон/ряды/места — иначе уже сделанные брони перестанут находить своё место на схеме.
Ключ (key) зала хранится в базе, поэтому после создания сеансов его тоже менять нельзя.
"""
import pathlib
from dataclasses import dataclass, field

import autopick as ap
import seatmap

DATA_DIR = pathlib.Path(__file__).parent / "data"
LEGACY_HALL = "congress"   # зал, в котором лежат все сеансы, созданные до появления выбора зала


class Hall:
    """Зал: схема + настройки алгоритма подбора. Всё тяжёлое считается один раз при старте."""

    def __init__(self, key, name, svg, *, default_zones=None, extra_zones_label="со всеми зонами",
                 zone_bias=None, default_bias=0.0, pitch=ap.PITCH, row_link=ap.ROW_LINK):
        """
        key               короткий латинский идентификатор (хранится в базе, потом не меняйте)
        name              название для интерфейса
        svg               имя файла в data/ (или готовый путь)
        default_zones     в каких зонах подбирать места по умолчанию (None — во всех)
        extra_zones_label подпись галочки «подбирать и в остальных зонах» (показывается,
                          только если есть зоны вне default_zones)
        zone_bias         штраф автоподбора за зону {"Партер": 0, "Балкон": 1.5}: чем меньше,
                          тем охотнее берём; зоны, которых нет в словаре, получают default_bias
        pitch             шаг между соседними местами в ряду по x в единицах SVG
        row_link          ряды, расстояние между которыми по y не больше этого, считаются соседними
                          (проход между секциями должен быть больше)
        """
        path = pathlib.Path(svg)
        if not path.is_absolute():
            path = DATA_DIR / path
        self.key, self.name, self.svg_path = key, name, path
        self.svg, self.seats, (self.width, self.height) = seatmap.load_svg(path)
        self.zones = list(dict.fromkeys(k[0] for k in self.seats))   # порядок появления в файле
        self.total = len(self.seats)

        self.default_zones = tuple(default_zones) if default_zones else tuple(self.zones)
        unknown = [z for z in self.default_zones if z not in self.zones]
        if unknown:
            raise ValueError(f"Зал «{key}»: в default_zones есть зоны, которых нет в схеме: {unknown}. "
                             f"Есть: {self.zones}")
        bad_bias = [z for z in (zone_bias or {}) if z not in self.zones]
        if bad_bias:
            raise ValueError(f"Зал «{key}»: в zone_bias есть зоны, которых нет в схеме: {bad_bias}. "
                             f"Есть: {self.zones}")
        self.extra_zones = [z for z in self.zones if z not in self.default_zones]
        self.extra_zones_label = extra_zones_label

        self.zone_rows = {z: any(k[1] for k in self.seats if k[0] == z) for z in self.zones}
        self.zone_counts = {z: sum(1 for k in self.seats if k[0] == z) for z in self.zones}
        self.grid = ap.Grid(self.seats, zone_bias=zone_bias or {}, default_bias=default_bias,
                            pitch=pitch, row_link=row_link)

    def meta(self):
        """Описание зала для фронтенда (без самой схемы — она отдаётся отдельным запросом)."""
        return {"key": self.key, "name": self.name, "width": self.width, "height": self.height,
                "total": self.total,
                "zones": [{"name": z, "has_rows": self.zone_rows[z], "count": self.zone_counts[z]}
                          for z in self.zones],
                "default_zones": list(self.default_zones),
                "extra_zones_label": self.extra_zones_label if self.extra_zones else ""}


_HALLS: dict = {}


def register(key, name, svg, **options):
    if key in _HALLS:
        raise ValueError(f"Зал с ключом «{key}» уже зарегистрирован")
    _HALLS[key] = Hall(key, name, svg, **options)
    return _HALLS[key]


def get(key):
    """Зал по ключу; KeyError, если такого нет (например, его убрали из реестра)."""
    return _HALLS[key]


def all_halls():
    return list(_HALLS.values())


def default():
    """Зал, который выбран по умолчанию в новом сеансе (первый в реестре)."""
    return next(iter(_HALLS.values()))


# ═══════════════════════════════ ЗАЛЫ ═══════════════════════════════
# Порядок важен: первый зал выбирается по умолчанию.

register(
    key=LEGACY_HALL,
    name="Конгресс-холл ДГТУ",
    svg="hall.svg",
    default_zones=("Партер", "Балкон"),
    extra_zones_label="с боковыми балконами",
    zone_bias={"Партер": 0.0, "Балкон": 1.5},
    default_bias=3.0,           # боковые балконы — в последнюю очередь
)

register(
    key="conference",
    name="Конференц-зал",
    svg="conference.svg",
    default_zones=("Партер",),
    zone_bias={"Партер": 0.0},
    pitch=20.0,                 # шаг между местами по x
    row_link=30.0,              # ряды идут через 22, проходов между рядами нет
)

# Пример второго зала — раскомментируйте и поправьте:
#
# register(
#     key="small_hall",
#     name="Малый зал",
#     svg="small_hall.svg",
#     default_zones=("Партер",),
#     zone_bias={"Партер": 0.0},
#     pitch=18.0,               # шаг между местами по x; узнать: python tools/check_hall.py small_hall
#     row_link=26.0,
# )

if LEGACY_HALL not in _HALLS:
    raise RuntimeError(f"В реестре должен остаться зал «{LEGACY_HALL}»: к нему привязаны старые сеансы")
