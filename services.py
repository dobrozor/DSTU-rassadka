"""Часть 2а. Бизнес-логика: места, калькулятор, статистика, журнал, авто-подбор, CSV.

Все функции работают в рамках текущей транзакции db.session и сами НЕ коммитят —
commit делает вызывающий (роут), чтобы действие и запись в журнале были атомарны.
"""
import colorsys
import csv
import io
import re
from collections import defaultdict

from sqlalchemy.orm import selectinload

import autopick as ap
import halls
from models import PAID, RESERVED, STATUSES, AuditLog, Order, SeatBooking, ShowSession, db

CURRENCY = "₽"
# Фиксированная палитра для подсветки диапазонов рядов на схеме (10 заметных, различимых цветов).
ROW_COLOR_PALETTE = ["#e53935", "#fb8c00", "#fdd835", "#7cb342", "#00897b",
                     "#1e88e5", "#3949ab", "#8e24aa", "#d81b60", "#6d4c41"]
STATUS_RU = {RESERVED: "бронь", PAID: "оплачен"}


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def hall_of(sess):
    """Зал сеанса. Если его убрали из halls.py — понятная ошибка, а не 500 без объяснений."""
    try:
        return halls.get(sess.hall)
    except KeyError:
        raise ApiError(500, f"Зал «{sess.hall}» не зарегистрирован в halls.py — верните его в реестр")


# ─────────────────────────── мелочи ───────────────────────────
def plural(n, forms=("место", "места", "мест")):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def seats_word(n):
    return f"{n} {plural(n)}"


def log(session_id, text, actor, order_id=None):
    db.session.add(AuditLog(session_id=session_id, action_text=text[:500],
                            actor=actor or "", order_id=order_id))


def pick_color(existing_hex):
    """Новый цвет, максимально далёкий по тону от уже занятых (золотой угол + проверка)."""
    hues = [colorsys.rgb_to_hls(*(int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)))[0] * 360
            for c in existing_hex]

    def dist(h):
        return min((min(abs(h - x), 360 - abs(h - x)) for x in hues), default=360)

    cands = [((len(hues) + i) * 137.508) % 360 for i in range(60)]
    h = max(cands, key=dist)
    r, g, b = colorsys.hls_to_rgb(h / 360, 0.42, 0.72)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


# ─────────────────────────── места ───────────────────────────
def norm_seat(d, hall):
    """{'zone','row','seat'[,'is_free']} → ((zone,row,seat), is_free). Проверяет, что место есть в схеме зала."""
    if not isinstance(d, dict):
        raise ApiError(400, "Место должно быть объектом {zone, row, seat}")
    key = (str(d.get("zone", "")), str(d.get("row") or ""), str(d.get("seat", "")))
    if key not in hall.seats:
        raise ApiError(400, f"Такого места нет в схеме: {key[0]} ряд {key[1] or '—'} место {key[2]}")
    return key, bool(d.get("is_free", False))


def norm_keys(items, hall):
    seen, out = set(), []
    for it in items or []:
        key, free = norm_seat(it, hall)
        if key not in seen:
            seen.add(key)
            out.append((key, free))
    return out


def occupied_map(session_id):
    """{ключ места: SeatBooking} по сеансу."""
    rows = SeatBooking.query.filter_by(session_id=session_id).all()
    return {(b.zone, b.row, b.seat): b for b in rows}


def add_seats(order, items):
    """Добавляет места в заказ. Возвращает число реально добавленных.
    Место, занятое другим заказом, → 409 со списком конфликтов."""
    wanted = norm_keys(items, hall_of(order.session))
    taken = occupied_map(order.session_id)
    other = {k: taken[k].order.name for k, _ in wanted
             if k in taken and taken[k].order_id != order.id}
    if other:
        raise ApiError(409, "Некоторые места уже заняты другим заказом",
                       conflicts=[{"zone": k[0], "row": k[1], "seat": k[2], "order": name}
                                  for k, name in other.items()])
    added = 0
    for key, free in wanted:
        if key in taken:  # уже в этом заказе — пропускаем
            continue
        order.seats.append(SeatBooking(session_id=order.session_id, zone=key[0],
                                       row=key[1], seat=key[2], is_free=free))
        added += 1
    return added


def remove_seats(order, items):
    ks = {k for k, _ in norm_keys(items, hall_of(order.session))}
    gone = [b for b in order.seats if (b.zone, b.row, b.seat) in ks]
    for b in gone:
        order.seats.remove(b)  # delete-orphan удалит строку
    return len(gone)


def set_free(order, items, value):
    ks = {k for k, _ in norm_keys(items, hall_of(order.session))}
    n = 0
    for b in order.seats:
        if (b.zone, b.row, b.seat) in ks and b.is_free != value:
            b.is_free, n = value, n + 1
    return n


# ─────────────────────────── калькулятор ───────────────────────────
def row_rule(sess, zone, row):
    """Правило диапазона рядов, под которое подходит место (первое подходящее в списке
    побеждает). Если ряд не числовой или правило без рядов — правило применяется на всю зону."""
    r = int(row) if row and str(row).isdigit() else None
    for rule in sess.row_prices or []:
        if rule.get("zone") == zone:
            rf = rule.get("row_from")
            rt = rule.get("row_to")
            if rf is None or rt is None:
                return rule  # Правило на всю зону
            if r is not None and rf <= r <= rt:
                return rule
    return None


def seat_price(order, zone, row=None):
    if order.price_override is not None:
        return order.price_override
    rule = row_rule(order.session, zone, row)
    if rule is not None:
        return int(rule["price"])
    zp = order.session.zone_prices or {}
    return int(zp.get(zone, order.session.base_price))


def calc_order(order):
    """{'paid_seats','free_seats','total'} — бесплатные места в сумму не входят."""
    paid = [b for b in order.seats if not b.is_free]
    return {"paid_seats": len(paid),
            "free_seats": len(order.seats) - len(paid),
            "total": sum(seat_price(order, b.zone, b.row) for b in paid)}


def recalc_order(order):
    order.total_amount = calc_order(order)["total"]
    return order.total_amount


# ─────────────────────────── состояние сеанса ───────────────────────────
def load_orders(session_id):
    return (Order.query.filter_by(session_id=session_id)
            .options(selectinload(Order.seats)).order_by(Order.id).all())


def serialize_order(o):
    c = calc_order(o)
    return {"id": o.id, "name": o.name, "comment": o.comment or "", "color": o.color,
            "status": o.status, "price_override": o.price_override, "printed": o.printed,
            "seats": [[b.zone, b.row, b.seat, b.is_free] for b in o.seats],
            "paid_seats": c["paid_seats"], "free_seats": c["free_seats"],
            "total_amount": c["total"]}


def session_stats(orders, hall):
    booked = {s: sum(len(o.seats) for o in orders if o.status == s) for s in STATUSES}
    amount = {s: sum(o.total_amount for o in orders if o.status == s) for s in STATUSES}
    return {"total": hall.total, "reserved": booked[RESERVED], "paid": booked[PAID],
            "free": hall.total - booked[RESERVED] - booked[PAID],
            "reserved_amount": amount[RESERVED], "paid_amount": amount[PAID]}


def serialize_audit(a):
    return {"id": a.id, "ts": a.timestamp.isoformat() + "Z", "actor": a.actor,
            "text": a.action_text, "order_id": a.order_id}


def audit_rows(session_id, limit=100, before=None):
    q = AuditLog.query.filter_by(session_id=session_id)
    if before:
        q = q.filter(AuditLog.id < before)
    return [serialize_audit(a) for a in q.order_by(AuditLog.id.desc()).limit(limit)]


def audit_all(limit=100, before=None, event_id=None):
    """Общий журнал по всем сеансам (новые сверху); у каждой записи — к какому сеансу относится."""
    q = (db.session.query(AuditLog, ShowSession)
         .join(ShowSession, ShowSession.id == AuditLog.session_id))
    if event_id:
        q = q.filter(ShowSession.event_id == event_id)
    if before:
        q = q.filter(AuditLog.id < before)
    rows = []
    for a, sess in q.order_by(AuditLog.id.desc()).limit(limit):
        d = serialize_audit(a)
        d.update(session_id=sess.id, session=f"{sess.starts_at:%d.%m %H:%M}",
                 event_id=sess.event_id, event=sess.event.name)
        rows.append(d)
    return rows


def serialize_session(s):
    return {"id": s.id, "event_id": s.event_id, "event_name": s.event.name, "hall": s.hall,
            "starts_at": s.starts_at.isoformat(timespec="minutes"),
            "base_price": s.base_price, "zone_prices": s.zone_prices or {},
            "row_prices": s.row_prices or []}


def session_state(s):
    orders = load_orders(s.id)
    last = audit_rows(s.id, 1)
    return {"session": serialize_session(s), "currency": CURRENCY,
            "stats": session_stats(orders, hall_of(s)),
            "orders": [serialize_order(o) for o in orders],
            "audit": audit_rows(s.id, 100),
            "rev": last[0]["id"] if last else 0}


# ─────────────────────────── авто-подбор ───────────────────────────
def _label(keys):
    """Читаемое описание блока: «Партер, ряд 7: места 5–19 (15)»."""
    g = defaultdict(list)
    for z, r, s in keys:
        g[(z, r)].append(s)
    out = []
    for (z, r), ss in g.items():
        ss.sort(key=float)
        span = ss[0] if len(ss) == 1 else f"{ss[0]}–{ss[-1]}"
        out.append(f"{z}{', ряд ' + r if r else ''}: {'место' if len(ss) == 1 else 'места'} {span} ({len(ss)})")
    return out


def autopick(sess, count, zones=None, alt=0):
    hall = hall_of(sess)
    if not isinstance(count, int) or not 1 <= count <= hall.total:
        raise ApiError(400, "Количество мест — целое число от 1")
    zones = [z for z in (zones or hall.default_zones) if z in hall.zones]
    if not zones:
        raise ApiError(400, "Не выбрано ни одной зоны")
    occupied = set(occupied_map(sess.id))
    free_total = sum(1 for k in hall.seats if k[0] in zones and k not in occupied)
    if count > free_total:
        raise ApiError(409, f"Свободно только {free_total} {plural(free_total)} в выбранных зонах",
                       free=free_total)

    blocks = ap.find_blocks(hall.grid, occupied, count, tuple(zones), top=5)
    split = False
    if blocks:
        alt = alt % len(blocks)
        chosen, n_alt = [blocks[alt]], len(blocks)
    else:
        chosen, n_alt, split, alt = ap.find_split(hall.grid, occupied, count, tuple(zones)), 1, True, 0
        if not chosen:
            hint = f" или включите «{hall.extra_zones_label}»" if hall.extra_zones else ""
            raise ApiError(409, f"Не нашлось подходящих блоков — попробуйте меньшее число мест{hint}",
                           free=free_total)
    seats = [k for b in chosen for k in b.keys]
    return {"found": True, "split": split, "alt": alt, "alternatives": n_alt,
            "score": round(sum(b.score for b in chosen), 2),
            "seats": [{"zone": z, "row": r, "seat": s} for z, r, s in seats],
            "summary": [line for b in chosen for line in _label(b.keys)]}


# ─────────────────────────── CSV для контроля на входе ───────────────────────────
def _seat_cell(seat):
    """Excel превращает «0.1» в число/дату. Формула ="0.1" заставляет его оставить текст.
    Обычные номера (1, 2, … 42) не трогаем: пусть остаются числами — по ним удобно сортировать."""
    return seat if re.fullmatch(r"[1-9]\d*", seat) else f'="{seat}"'


def orders_csv(orders, sess):
    zone_order = {z: i for i, z in enumerate(hall_of(sess).zones)}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["Заказ", "Статус", "Зона", "Ряд", "Место", "Тип билета", "Цена"])
    for o in orders:
        seats = sorted(o.seats, key=lambda b: (zone_order.get(b.zone, 99),
                                               int(b.row) if b.row else 0, float(b.seat)))
        for b in seats:
            w.writerow([o.name, STATUS_RU[o.status].capitalize(), b.zone, b.row, _seat_cell(b.seat),
                        "Бесплатный" if b.is_free else "Платный",
                        0 if b.is_free else seat_price(o, b.zone, b.row)])
    return "\ufeff" + buf.getvalue()  # BOM — чтобы Excel открыл кириллицу правильно


# ─────────────────────────── Excel: полная выгрузка мероприятия ───────────────────────────
XLSX_HEADERS = ["Школа/заказчик", "Комментарий", "Статус", "Кол-во билетов платных", "Цена билетов",
                "Кол-во билетов бесплатных", "Общее кол-во билетов", "Сумма к оплате"]


def _price_cell(o):
    """Цена платного билета; если в заказе билеты разной цены — «250 / 500»; если платных нет — пусто."""
    prices = sorted({seat_price(o, b.zone, b.row) for b in o.seats if not b.is_free})
    if not prices:
        return None
    return prices[0] if len(prices) == 1 else " / ".join(str(p) for p in prices)


def event_xlsx(event):
    """Один лист: блок на каждый сеанс — строка с датой и временем («14.12 11:00»), заголовки колонок, заказы.
    Возвращает байты .xlsx."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Все сеансы"
    sessions = sorted(event.sessions, key=lambda s: s.starts_at)
    thin = Side(style="thin", color="BBBBBB")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="E8E8E8")

    row = 1
    for sess in sessions:
        title = f"{sess.starts_at:%d.%m} {sess.starts_at:%H:%M}"
        ws.cell(row, 1, title).font = Font(bold=True, size=14)
        row += 1
        for col, h in enumerate(XLSX_HEADERS, 1):
            c = ws.cell(row, col, h)
            c.font, c.fill, c.border = Font(bold=True), head_fill, box
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        row += 1
        for o in load_orders(sess.id):
            calc = calc_order(o)
            values = [o.name, o.comment or "", STATUS_RU[o.status].capitalize(), calc["paid_seats"],
                      _price_cell(o), calc["free_seats"], len(o.seats), calc["total"]]
            for col, v in enumerate(values, 1):
                c = ws.cell(row, col, v)
                c.border = box
                c.alignment = Alignment(vertical="top", wrap_text=col in (1, 2),
                                        horizontal="left" if col <= 3 else "center")
                if col in (5, 8) and isinstance(v, int):
                    c.number_format = "#,##0"
            row += 1
        row += 1  # пустая строка между сеансами

    for col, w in zip("ABCDEFGH", (34, 36, 12, 14, 14, 16, 14, 16)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
