"""Часть 2б. Flask: JSON API + страница со схемой.

Запуск:  flask --app app run --debug      (или python main.py)
"""
import os
import re
from datetime import datetime
from urllib.parse import quote, unquote

from flask import Flask, Response, jsonify, render_template, request
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException

import backup
import halls
import services as sv
from models import RESERVED, STATUSES, Event, Order, SeatBooking, ShowSession, db

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ROW_PALETTE = set(sv.ROW_COLOR_PALETTE)


def _migrate():
    """db.create_all() не трогает уже существующие таблицы: у базы, созданной до появления
    новых колонок, их просто не будет. Добавляем недостающие сами."""
    cols = {row[1] for row in db.session.execute(db.text("PRAGMA table_info(sessions)")).fetchall()}
    if "row_prices" not in cols:   # цены по рядам
        db.session.execute(db.text("ALTER TABLE sessions ADD COLUMN row_prices TEXT"))
        db.session.execute(db.text("UPDATE sessions SET row_prices = '[]' WHERE row_prices IS NULL"))
    if "hall" not in cols:         # выбор зала: все прежние сеансы были в Конгресс-холле
        db.session.execute(db.text(
            f"ALTER TABLE sessions ADD COLUMN hall VARCHAR(40) NOT NULL DEFAULT '{halls.LEGACY_HALL}'"))
    ocols = {row[1] for row in db.session.execute(db.text("PRAGMA table_info(orders)")).fetchall()}
    if "printed" not in ocols:     # отметка «билеты напечатаны»
        db.session.execute(db.text("ALTER TABLE orders ADD COLUMN printed BOOLEAN NOT NULL DEFAULT 0"))
    db.session.commit()


def create_app(db_uri=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_uri or "sqlite:///" + os.path.join(app.instance_path, "hall.db")
    app.json.ensure_ascii = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        _migrate()

    # ───────────── helpers ─────────────
    def actor():
        """Кто делает действие: имя оператора приходит в заголовке X-Operator (URL-encoded)."""
        return unquote(request.headers.get("X-Operator", "")).strip()[:64] or "Оператор"

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise sv.ApiError(400, "Ожидается JSON-объект")
        return data

    def get_or_404(model, ident):
        obj = db.session.get(model, ident)
        if obj is None:
            raise sv.ApiError(404, "Не найдено")
        return obj

    def commit():
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise sv.ApiError(409, "Эти места только что заняли — обновите схему и повторите")

    def to_int(v, name, minimum=0, nullable=False):
        if v in (None, "") and nullable:
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise sv.ApiError(400, f"«{name}» должно быть целым числом")
        if n < minimum:
            raise sv.ApiError(400, f"«{name}» не может быть меньше {minimum}")
        return n

    def clean_name(v):
        v = (v or "").strip()
        if not v:
            raise sv.ApiError(400, "Укажите название")
        return v[:200]

    def get_hall(key):
        try:
            return halls.get(key)
        except (KeyError, TypeError):
            raise sv.ApiError(400, f"Неизвестный зал «{key}»")

    def zone_prices(v, hall):
        if not v:
            return {}
        if not isinstance(v, dict):
            raise sv.ApiError(400, "Цены по зонам — объект {зона: цена}")
        return {z: to_int(p, f"цена «{z}»") for z, p in v.items()
                if z in hall.zones and p not in (None, "")}

    def row_prices(v, hall):
        """[{zone, row_from, row_to, price}] — проверяем зону и целостность диапазона;
        порядок в списке сохраняется как есть (первое подходящее правило побеждает)."""
        if not v:
            return []
        if not isinstance(v, list):
            raise sv.ApiError(400, "Цены по рядам — список правил")
        out = []
        for i, r in enumerate(v):
            if not isinstance(r, dict):
                raise sv.ApiError(400, f"Правило №{i + 1}: ожидается объект")
            zone = r.get("zone")
            if zone not in hall.zones:
                raise sv.ApiError(400, f"Правило №{i + 1}: в зале «{hall.name}» нет зоны «{zone}»")
            rf = to_int(r.get("row_from"), f"правило №{i + 1}: ряд с", 1, nullable=True)
            rt = to_int(r.get("row_to"), f"правило №{i + 1}: ряд по", 1, nullable=True)
            if rf is not None and rt is not None and rt < rf:
                raise sv.ApiError(400, f"Правило №{i + 1}: «ряд по» меньше «ряд с»")
            price = to_int(r.get("price"), f"правило №{i + 1}: цена")
            color = r.get("color")
            if color is not None:
                if not isinstance(color, str) or color.lower() not in ROW_PALETTE:
                    raise sv.ApiError(400, f"Правило №{i + 1}: цвет — один из палитры подсветки")
                color = color.lower()
            out.append({"zone": zone, "row_from": rf, "row_to": rt, "price": price, "color": color})
        return out

    def parse_dt(v):
        try:
            return datetime.fromisoformat(str(v))
        except ValueError:
            raise sv.ApiError(400, "Дата и время сеанса — в формате ГГГГ-ММ-ДДTЧЧ:ММ")

    # ───────────── страница ─────────────
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/log")
    def log_page():
        """Общий журнал действий по всем сеансам."""
        return render_template("log.html")

    @app.get("/api/audit")
    def audit_all():
        limit = min(to_int(request.args.get("limit", 100), "limit", 1), 500)
        before = to_int(request.args.get("before"), "before", 1, nullable=True)
        event_id = to_int(request.args.get("event_id"), "event_id", 1, nullable=True)
        return jsonify(sv.audit_all(limit, before, event_id))

    @app.get("/api/config")
    def config():
        """Статические настройки для фронтенда: валюта и палитра подсветки рядов —
        чтобы список цветов задавался в одном месте (здесь) и не расходился с проверкой в row_prices()."""
        return jsonify({"currency": sv.CURRENCY, "row_colors": sv.ROW_COLOR_PALETTE})

    # ───────────── резервная копия ─────────────
    @app.post("/api/backup/send")
    def backup_send():
        """Кнопка «Отправить базу в Telegram»."""
        try:
            backup.send_backup(app, f"по запросу · {actor()}")
        except backup.BackupError as e:
            raise sv.ApiError(503, str(e))
        return jsonify({"ok": True})

    # ───────────── залы ─────────────
    @app.get("/api/halls")
    def halls_list():
        """Все залы из реестра halls.py (первый — по умолчанию)."""
        return jsonify([h.meta() for h in halls.all_halls()])

    @app.get("/api/halls/<key>/svg")
    def hall_svg(key):
        """Схема зала. ETag + no-cache: браузер каждый раз спрашивает, не изменилась ли схема,
        но заново скачивает её только если она правда изменилась."""
        hall = get_hall(key)
        resp = Response(hall.svg, mimetype="image/svg+xml")
        resp.headers["Cache-Control"] = "no-cache"
        resp.add_etag()
        return resp.make_conditional(request)

    # ───────────── мероприятия и сеансы ─────────────
    @app.get("/api/events")
    def events_list():
        return jsonify([{"id": e.id, "name": e.name, "description": e.description,
                 "orders": Order.query.join(ShowSession).filter(ShowSession.event_id == e.id).count(),
                 "sessions": [{"id": s.id, "hall": s.hall,
                               "starts_at": s.starts_at.isoformat(timespec="minutes")}
                              for s in e.sessions]}
                for e in Event.query.order_by(Event.id).all()])

    @app.post("/api/events")
    def event_create():
        d = body()
        e = Event(name=clean_name(d.get("name")), description=(d.get("description") or "").strip())
        db.session.add(e)
        commit()
        return jsonify({"id": e.id}), 201

    @app.delete("/api/events/<int:eid>")
    def event_delete(eid):
        db.session.delete(get_or_404(Event, eid))  # каскадом: сеансы, заказы, места, журнал
        commit()
        return "", 204

    @app.post("/api/events/<int:eid>/sessions")
    def session_create(eid):
        get_or_404(Event, eid)
        d = body()
        hall = get_hall(d.get("hall") or halls.default().key)
        s = ShowSession(event_id=eid, hall=hall.key, starts_at=parse_dt(d.get("starts_at")),
                        base_price=to_int(d.get("base_price", 0), "цена"),
                        zone_prices=zone_prices(d.get("zone_prices"), hall),
                        row_prices=row_prices(d.get("row_prices"), hall))
        db.session.add(s)
        db.session.flush()
        sv.log(s.id, f"Сеанс создан · {hall.name}", actor())
        commit()
        return jsonify({"id": s.id}), 201

    @app.patch("/api/sessions/<int:sid>")
    def session_update(sid):
        s, d = get_or_404(ShowSession, sid), body()
        hall, log_text = sv.hall_of(s), "Параметры сеанса (дата/цены) изменены"
        if "hall" in d and d["hall"] != s.hall:
            new = get_hall(d["hall"])
            if SeatBooking.query.filter_by(session_id=sid).first() is not None:
                raise sv.ApiError(409, "Зал нельзя сменить, пока в сеансе есть занятые места: "
                                       "сначала удалите заказы или создайте новый сеанс")
            log_text = f"Зал сеанса изменён: «{hall.name}» → «{new.name}»"
            hall, s.hall = new, new.key
            # цены прежнего зала к новому не относятся: оставляем только то, что есть и там
            s.zone_prices = {z: p for z, p in (s.zone_prices or {}).items() if z in hall.zones}
            s.row_prices = [r for r in (s.row_prices or []) if r.get("zone") in hall.zones]
        if "starts_at" in d:
            s.starts_at = parse_dt(d["starts_at"])
        if "base_price" in d:
            s.base_price = to_int(d["base_price"], "цена")
        if "zone_prices" in d:
            s.zone_prices = zone_prices(d["zone_prices"], hall)
        if "row_prices" in d:
            s.row_prices = row_prices(d["row_prices"], hall)
        db.session.flush()
        for o in sv.load_orders(sid):   # цены изменились → пересчитать суммы всех заказов
            sv.recalc_order(o)
        sv.log(sid, log_text, actor())
        commit()
        return jsonify(sv.session_state(s))

    @app.delete("/api/sessions/<int:sid>")
    def session_delete(sid):
        db.session.delete(get_or_404(ShowSession, sid))
        commit()
        return "", 204

    @app.get("/api/sessions/<int:sid>/state")
    def session_state(sid):
        """Всё, что нужно для отрисовки схемы: заказы с местами, статистика, журнал."""
        return jsonify(sv.session_state(get_or_404(ShowSession, sid)))

    @app.get("/api/sessions/<int:sid>/audit")
    def session_audit(sid):
        get_or_404(ShowSession, sid)
        limit = min(to_int(request.args.get("limit", 100), "limit", 1), 500)
        before = to_int(request.args.get("before"), "before", 1, nullable=True)
        return jsonify(sv.audit_rows(sid, limit, before))

    # ───────────── заказы ─────────────
    @app.post("/api/sessions/<int:sid>/orders")
    def order_create(sid):
        s, d = get_or_404(ShowSession, sid), body()
        status = d.get("status", RESERVED)
        if status not in STATUSES:
            raise sv.ApiError(400, "Статус: reserved или paid")
        if not d.get("seats"):
            raise sv.ApiError(400, "Выберите хотя бы одно место")
        color = d.get("color")
        if not (isinstance(color, str) and COLOR_RE.match(color)):
            color = sv.pick_color([o.color for o in s.orders])
        o = Order(session_id=sid, name=clean_name(d.get("name")), comment=(d.get("comment") or "").strip(),
                  color=color.lower(), status=status,
                  price_override=to_int(d.get("price_override"), "цена", nullable=True))
        db.session.add(o)
        db.session.flush()
        n = sv.add_seats(o, d["seats"])
        db.session.flush()
        sv.recalc_order(o)
        verb = "Бронь" if status == RESERVED else "Оплаченный заказ"
        sv.log(sid, f"{verb} «{o.name}» на {sv.seats_word(n)} добавлен{'а' if status == RESERVED else ''}",
               actor(), o.id)
        commit()
        return jsonify(sv.serialize_order(o)), 201

    @app.patch("/api/orders/<int:oid>")
    def order_update(oid):
        o, d = get_or_404(Order, oid), body()
        old_name, changed = o.name, []
        if "name" in d and clean_name(d["name"]) != o.name:
            o.name = clean_name(d["name"])
            changed.append(f"название → «{o.name}»")
        if "comment" in d and (d["comment"] or "").strip() != (o.comment or ""):
            o.comment = (d["comment"] or "").strip()
            changed.append("комментарий")
        if "color" in d:
            if not (isinstance(d["color"], str) and COLOR_RE.match(d["color"])):
                raise sv.ApiError(400, "Цвет — в формате #rrggbb")
            if d["color"].lower() != o.color:
                o.color = d["color"].lower()
                changed.append("цвет")
        if "price_override" in d:
            price = to_int(d["price_override"], "цена", nullable=True)
            if price != o.price_override:
                o.price_override = price
                changed.append("цена билета " + (f"{price} {sv.CURRENCY}" if price is not None else "— по прайсу сеанса"))
        if "status" in d:
            if d["status"] not in STATUSES:
                raise sv.ApiError(400, "Статус: reserved или paid")
            if d["status"] != o.status:
                sv.log(o.session_id, f"Заказ «{old_name}»: {sv.STATUS_RU[o.status]} → {sv.STATUS_RU[d['status']]}",
                       actor(), o.id)
                o.status = d["status"]
        if "printed" in d:
            if not isinstance(d["printed"], bool):
                raise sv.ApiError(400, "printed: true или false")
            if d["printed"] != o.printed:
                o.printed = d["printed"]
                sv.log(o.session_id, f"Заказ №{o.id} «{old_name}»: " +
                       ("билеты напечатаны" if o.printed else "отметка «напечатано» снята"), actor(), o.id)
        db.session.flush()
        sv.recalc_order(o)
        if changed:
            sv.log(o.session_id, f"Заказ «{old_name}» изменён: " + "; ".join(changed), actor(), o.id)
        commit()
        return jsonify(sv.serialize_order(o))

    @app.delete("/api/orders/<int:oid>")
    def order_delete(oid):
        o = get_or_404(Order, oid)
        sv.log(o.session_id, f"Заказ «{o.name}» удалён, освобождено {sv.seats_word(len(o.seats))}",
               actor(), o.id)
        db.session.delete(o)
        commit()
        return "", 204

    @app.post("/api/orders/<int:oid>/seats")
    def order_seats(oid):
        """Пакетное изменение состава заказа. Любой из ключей необязателен:
        add / remove — [{zone,row,seat[,is_free]}], set_free / set_paid — [{zone,row,seat}]."""
        o, d = get_or_404(Order, oid), body()
        sid, name, a = o.session_id, o.name, actor()
        res = {"added": 0, "removed": 0, "freed": 0, "unfreed": 0}
        if d.get("remove"):
            res["removed"] = sv.remove_seats(o, d["remove"])
            if res["removed"]:
                sv.log(sid, f"Убрано {sv.seats_word(res['removed'])} из заказа «{name}»", a, oid)
        if d.get("add"):
            db.session.flush()
            res["added"] = sv.add_seats(o, d["add"])
            if res["added"]:
                reprint = o.printed
                o.printed = False       # состав изменился — билеты надо допечатать
                sv.log(sid, f"В заказ «{name}» добавлено {sv.seats_word(res['added'])}" +
                       (" — билеты нужно допечатать" if reprint else ""), a, oid)
        if d.get("set_free"):
            res["freed"] = sv.set_free(o, d["set_free"], True)
            if res["freed"]:
                sv.log(sid, f"Заказ «{name}»: бесплатными отмечено {sv.seats_word(res['freed'])}", a, oid)
        if d.get("set_paid"):
            res["unfreed"] = sv.set_free(o, d["set_paid"], False)
            if res["unfreed"]:
                sv.log(sid, f"Заказ «{name}»: платными отмечено {sv.seats_word(res['unfreed'])}", a, oid)
        db.session.flush()
        sv.recalc_order(o)
        commit()
        return jsonify({**res, "order": sv.serialize_order(o)})

    # ───────────── авто-подбор ─────────────
    @app.post("/api/sessions/<int:sid>/autopick")
    def autopick(sid):
        """{count, zones?: ["Партер", ...], alt?: 0..} → лучший блок свободных мест.
        alt=1,2,… — следующие по качеству, заметно отличающиеся варианты."""
        s, d = get_or_404(ShowSession, sid), body()
        return jsonify(sv.autopick(s, to_int(d.get("count"), "количество", 1),
                                   d.get("zones"), to_int(d.get("alt", 0), "alt")))

    # ───────────── экспорт CSV ─────────────
    def csv_response(text, name):
        safe = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "export"
        return Response(text.encode("utf-8"), mimetype="text/csv; charset=utf-8", headers={
            "Content-Disposition": f"attachment; filename=\"export.csv\"; filename*=UTF-8''{quote(safe)}.csv"})

    @app.get("/api/orders/<int:oid>/export.csv")
    def order_export(oid):
        o = get_or_404(Order, oid)
        return csv_response(sv.orders_csv([o], o.session),
                            f"{o.name} — {o.session.event.name} {o.session.starts_at:%d.%m %H-%M}")

    @app.get("/api/sessions/<int:sid>/export.csv")
    def session_export(sid):
        s = get_or_404(ShowSession, sid)
        return csv_response(sv.orders_csv(sv.load_orders(sid), s),
                            f"Все заказы — {s.event.name} {s.starts_at:%d.%m %H-%M}")

    @app.get("/api/events/<int:eid>/export.xlsx")
    def event_export(eid):
        """Полная выгрузка мероприятия: все сеансы одним листом Excel."""
        e = get_or_404(Event, eid)
        safe = re.sub(r'[\\/:*?"<>|]+', "_", f"Все сеансы — {e.name}").strip() or "export"
        return Response(sv.event_xlsx(e),
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition":
                                 f"attachment; filename=\"export.xlsx\"; filename*=UTF-8''{quote(safe)}.xlsx"})

    # ───────────── ошибки → JSON ─────────────
    @app.errorhandler(sv.ApiError)
    def on_api_error(e):
        return jsonify({"error": e.message, **e.extra}), e.status

    @app.errorhandler(HTTPException)
    def on_http_error(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": e.description}), e.code
        return e

    return app


app = create_app()

DEBUG = True

if __name__ == "__main__":
    # В режиме debug Flask запускает два процесса (перезагрузчик + рабочий); бот и расписание нужны только
    # в рабочем, иначе копии придут дважды, а два бота с одним токеном будут конфликтовать (ошибка 409).
    if not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        backup.start(app)
    app.run(host='0.0.0.0', port=6969, debug=DEBUG)