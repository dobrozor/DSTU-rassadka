import os
import sys
from urllib.parse import quote

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import autopick as ap  # noqa: E402
import halls  # noqa: E402
from main import create_app  # noqa: E402

H = {"X-Operator": quote("Анна")}


@pytest.fixture()
def client():
    app = create_app("sqlite://")
    return app.test_client()


@pytest.fixture()
def sid(client):
    eid = client.post("/api/events", json={"name": "Новогодняя ёлка"}).json["id"]
    return client.post(f"/api/events/{eid}/sessions", headers=H, json={
        "starts_at": "2026-12-20T10:00", "base_price": 500, "zone_prices": {"Балкон": 300}}).json["id"]


def seat(z, r, s, free=False):
    return {"zone": z, "row": r, "seat": s, "is_free": free}


def test_calculator_ignores_free_and_uses_zone_price(client, sid):
    r = client.post(f"/api/sessions/{sid}/orders", headers=H, json={
        "name": "Школа №5",
        "seats": [seat("Партер", "5", "10"), seat("Партер", "5", "11"),
                  seat("Балкон", "1", "3"), seat("Партер", "5", "12", True)]})
    assert r.status_code == 201
    o = r.json
    assert (o["paid_seats"], o["free_seats"]) == (3, 1)
    assert o["total_amount"] == 500 + 500 + 300          # бесплатное не считается
    assert o["color"].startswith("#")

    # цена заказа перекрывает прайс сеанса
    o = client.patch(f"/api/orders/{o['id']}", headers=H, json={"price_override": 100}).json
    assert o["total_amount"] == 300


def test_double_booking_rejected(client, sid):
    client.post(f"/api/sessions/{sid}/orders", headers=H, json={"name": "А", "seats": [seat("Партер", "1", "5")]})
    r = client.post(f"/api/sessions/{sid}/orders", headers=H,
                    json={"name": "Б", "seats": [seat("Партер", "1", "5"), seat("Партер", "1", "6")]})
    assert r.status_code == 409 and r.json["conflicts"][0]["order"] == "А"
    st = client.get(f"/api/sessions/{sid}/state").json
    assert len(st["orders"]) == 1                        # «Б» не создан частично


def test_seat_must_exist(client, sid):
    r = client.post(f"/api/sessions/{sid}/orders", headers=H,
                    json={"name": "X", "seats": [seat("Партер", "99", "1")]})
    assert r.status_code == 400


def test_same_seat_in_other_session_is_free(client, sid):
    eid = client.get("/api/events").json[0]["id"]
    sid2 = client.post(f"/api/events/{eid}/sessions", headers=H, json={"starts_at": "2026-12-20T12:00"}).json["id"]
    for s in (sid, sid2):
        assert client.post(f"/api/sessions/{s}/orders", headers=H,
                           json={"name": "А", "seats": [seat("Партер", "1", "5")]}).status_code == 201


def test_lifecycle_stats_and_audit(client, sid):
    seats = [seat("Партер", "3", str(i)) for i in range(1, 6)]
    oid = client.post(f"/api/sessions/{sid}/orders", headers=H, json={"name": "Школа №5", "seats": seats}).json["id"]
    client.post(f"/api/sessions/{sid}/orders", headers=H, json={
        "name": "Лицей", "status": "paid", "seats": [seat("Партер", "4", "1"), seat("Партер", "4", "2")]})
    r = client.post(f"/api/orders/{oid}/seats", headers=H, json={
        "remove": seats[:2], "set_free": [seats[2]]}).json
    assert (r["removed"], r["freed"]) == (2, 1)
    client.patch(f"/api/orders/{oid}", headers=H, json={"status": "paid"})

    st = client.get(f"/api/sessions/{sid}/state").json
    assert st["stats"]["total"] == halls.get("congress").total == 1331
    assert (st["stats"]["reserved"], st["stats"]["paid"]) == (0, 5)
    assert st["stats"]["free"] == 1331 - 5
    assert st["stats"]["paid_amount"] == 2 * 500 + 2 * 500  # 2 платных у «Школы» + 2 у «Лицея»

    texts = [a["text"] for a in st["audit"]]
    assert "Бронь «Школа №5» на 5 мест добавлена" in texts
    assert "Убрано 2 места из заказа «Школа №5»" in texts
    assert any("бронь → оплачен" in t for t in texts)
    assert all(a["actor"] == "Анна" for a in st["audit"])

    client.delete(f"/api/orders/{oid}", headers=H)
    assert client.get(f"/api/sessions/{sid}/state").json["stats"]["paid"] == 2


def test_row_price_overrides_zone_price(client, sid):
    client.patch(f"/api/sessions/{sid}", headers=H, json={"row_prices": [
        {"zone": "Партер", "row_from": 1, "row_to": 6, "price": 250},
        {"zone": "Партер", "row_from": 7, "row_to": 10, "price": 700}]})
    o = client.post(f"/api/sessions/{sid}/orders", headers=H, json={"name": "А", "seats": [
        seat("Партер", "3", "5"),    # попадает в 1–6 → 250
        seat("Партер", "9", "5"),    # попадает в 7–10 → 700
        seat("Партер", "20", "5"),   # вне правил → базовая цена сеанса (500)
        seat("Балкон", "1", "3")]}).json  # другая зона, правила не действуют → 300
    assert o["total_amount"] == 250 + 700 + 500 + 300

    # цена заказа (price_override) по-прежнему главнее правил по рядам
    o = client.patch(f"/api/orders/{o['id']}", headers=H, json={"price_override": 100}).json
    assert o["total_amount"] == 4 * 100


def test_row_price_first_matching_rule_wins(client, sid):
    client.patch(f"/api/sessions/{sid}", headers=H, json={"row_prices": [
        {"zone": "Партер", "row_from": 1, "row_to": 10, "price": 1000},   # «VIP-ряд» внутри общего диапазона
        {"zone": "Партер", "row_from": 1, "row_to": 20, "price": 250}]})
    o = client.post(f"/api/sessions/{sid}/orders", headers=H, json={"name": "А", "seats": [seat("Партер", "5", "1")]}).json
    assert o["total_amount"] == 1000


def test_row_price_color_roundtrip_and_validation(client, sid):
    import services as sv
    good_color = sv.ROW_COLOR_PALETTE[0]
    r = client.patch(f"/api/sessions/{sid}", headers=H, json={"row_prices": [
        {"zone": "Партер", "row_from": 1, "row_to": 2, "price": 200, "color": good_color},
        {"zone": "Партер", "row_from": 3, "row_to": 4, "price": 300}]})   # без цвета — тоже можно
    assert r.status_code == 200
    rules = r.json["session"]["row_prices"]
    assert rules[0]["color"] == good_color and rules[1]["color"] is None

    bad = client.patch(f"/api/sessions/{sid}", headers=H,
                       json={"row_prices": [{"zone": "Партер", "row_from": 1, "row_to": 2, "price": 1, "color": "#123456"}]})
    assert bad.status_code == 400


def test_config_exposes_palette(client):
    import services as sv
    r = client.get("/api/config")
    assert r.status_code == 200 and r.json["row_colors"] == sv.ROW_COLOR_PALETTE and len(sv.ROW_COLOR_PALETTE) == 10


def test_row_price_validation_and_recalc(client, sid):
    bad = client.patch(f"/api/sessions/{sid}", headers=H, json={"row_prices": [{"zone": "Партер", "row_from": 9, "row_to": 1, "price": 1}]})
    assert bad.status_code == 400
    bad = client.patch(f"/api/sessions/{sid}", headers=H, json={"row_prices": [{"zone": "Космос", "row_from": 1, "row_to": 2, "price": 1}]})
    assert bad.status_code == 400

    oid = client.post(f"/api/sessions/{sid}/orders", headers=H, json={"name": "А", "seats": [seat("Партер", "2", "1")]}).json["id"]
    st = client.patch(f"/api/sessions/{sid}", headers=H, json={"row_prices": [{"zone": "Партер", "row_from": 1, "row_to": 6, "price": 250}]}).json
    assert st["orders"][0]["total_amount"] == 250 and st["session"]["row_prices"][0]["price"] == 250


def test_session_price_change_recalculates(client, sid):
    oid = client.post(f"/api/sessions/{sid}/orders", headers=H,
                      json={"name": "А", "seats": [seat("Партер", "1", "5")]}).json["id"]
    st = client.patch(f"/api/sessions/{sid}", headers=H, json={"base_price": 700}).json
    assert st["orders"][0]["total_amount"] == 700 and st["orders"][0]["id"] == oid


def test_csv_export(client, sid):
    oid = client.post(f"/api/sessions/{sid}/orders", headers=H, json={
        "name": "Школа №5", "seats": [seat("Партер", "2", "3"), seat("Партер", "2", "4", True)]}).json["id"]
    r = client.get(f"/api/orders/{oid}/export.csv")
    text = r.data.decode("utf-8-sig")
    lines = text.strip().split("\r\n")
    assert lines[0] == "Заказ;Статус;Зона;Ряд;Место;Тип билета;Цена"
    assert "Школа №5;Бронь;Партер;2;3;Платный;500" in lines
    assert "Школа №5;Бронь;Партер;2;4;Бесплатный;0" in lines
    assert client.get(f"/api/sessions/{sid}/export.csv").status_code == 200


def test_csv_keeps_special_seat_numbers_as_text(client, sid):
    import csv, io
    oid = client.post(f"/api/sessions/{sid}/orders", headers=H, json={"name": "Школа", "seats": [
        seat("Партер", "1", "0"), seat("Партер", "1", "0.1"), seat("Партер", "1", "2")]}).json["id"]
    text = client.get(f"/api/orders/{oid}/export.csv").data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    assert [r[4] for r in rows[1:]] == ['="0"', '="0.1"', "2"]   # сортировка по числу сохранена


def test_event_xlsx_export(client, sid):
    import io
    from openpyxl import load_workbook
    client.post(f"/api/sessions/{sid}/orders", headers=H, json={
        "name": "Школа №5", "comment": "2 класса", "seats": [
            seat("Партер", "2", "3"), seat("Партер", "2", "4"), seat("Партер", "2", "5", True)]})
    eid = client.get("/api/events").json[0]["id"]
    r = client.get(f"/api/events/{eid}/export.xlsx")
    assert r.status_code == 200 and "spreadsheetml" in r.mimetype
    ws = load_workbook(io.BytesIO(r.data)).active
    assert ws["A1"].value == "20.12 10:00"
    assert [c.value for c in ws[2]] == ["Школа/заказчик", "Комментарий", "Статус", "Кол-во билетов платных",
                                        "Цена билетов", "Кол-во билетов бесплатных", "Общее кол-во билетов",
                                        "Сумма к оплате"]
    assert [c.value for c in ws[3]] == ["Школа №5", "2 класса", "Бронь", 2, 500, 1, 3, 1000]


def test_global_audit_log(client, sid):
    client.post(f"/api/sessions/{sid}/orders", headers=H, json={
        "name": "Школа №5", "seats": [seat("Партер", "2", "3")]})
    assert client.get("/log").status_code == 200
    rows = client.get("/api/audit").json
    assert rows and rows[0]["session"] == "20.12 10:00" and rows[0]["actor"] == "Анна"
    assert "Школа №5" in rows[0]["text"] and rows[0]["event"]
    assert client.get(f"/api/audit?event_id={rows[0]['event_id']}").json == rows
    assert client.get("/api/audit?event_id=99999").json == []
    assert client.get(f"/api/audit?before={rows[-1]['id']}").json == []


def test_session_delete_removes_orders(client, sid):
    client.post(f"/api/sessions/{sid}/orders", headers=H, json={"name": "Школа", "seats": [seat("Партер", "2", "3")]})
    assert client.delete(f"/api/sessions/{sid}", headers=H).status_code == 204
    assert client.get(f"/api/sessions/{sid}/state").status_code == 404
    assert client.get("/api/audit").json == []


def test_printed_flag(client, sid):
    o = client.post(f"/api/sessions/{sid}/orders", headers=H, json={
        "name": "Школа", "seats": [seat("Партер", "2", "3")]}).json
    assert o["printed"] is False and isinstance(o["id"], int)
    # отметили «напечатано»
    assert client.patch(f"/api/orders/{o['id']}", headers=H, json={"printed": True}).json["printed"] is True
    # убрали место — билеты не меняются в большую сторону, отметка остаётся
    client.post(f"/api/orders/{o['id']}/seats", headers=H, json={"add": [seat("Партер", "2", "4")], "remove": []})
    got = client.get(f"/api/sessions/{sid}/state").json["orders"][0]
    assert got["printed"] is False                      # добавили место → сброс
    client.patch(f"/api/orders/{o['id']}", headers=H, json={"printed": True})
    client.post(f"/api/orders/{o['id']}/seats", headers=H, json={"remove": [seat("Партер", "2", "4")]})
    assert client.get(f"/api/sessions/{sid}/state").json["orders"][0]["printed"] is True   # убрали → остаётся
    assert client.patch(f"/api/orders/{o['id']}", headers=H, json={"printed": "да"}).status_code == 400
    log = " ".join(a["text"] for a in client.get(f"/api/sessions/{sid}/audit").json)
    assert "билеты напечатаны" in log and "допечатать" in log


# ───────────────────────── авто-подбор ─────────────────────────
def test_autopick_endpoint_and_no_overlap(client, sid):
    a = client.post(f"/api/sessions/{sid}/autopick", json={"count": 30}).json
    assert a["found"] and len(a["seats"]) == 30 and not a["split"]
    rows = {(s["zone"], s["row"]) for s in a["seats"]}
    assert len(rows) <= 2                                # «кучно»: не больше двух рядов
    client.post(f"/api/sessions/{sid}/orders", headers=H, json={"name": "А", "seats": a["seats"]})
    b = client.post(f"/api/sessions/{sid}/autopick", json={"count": 30}).json
    assert not ({(s["zone"], s["row"], s["seat"]) for s in a["seats"]}
                & {(s["zone"], s["row"], s["seat"]) for s in b["seats"]})
    alt = client.post(f"/api/sessions/{sid}/autopick", json={"count": 30, "alt": 1}).json
    assert alt["seats"] != b["seats"]


def test_autopick_not_enough(client, sid):
    r = client.post(f"/api/sessions/{sid}/autopick", json={"count": 1300})
    assert r.status_code == 409 and r.json["free"] == 1017 + 248


def test_autopick_avoids_orphan_single_seat():
    hall = halls.get("congress")
    g = hall.grid
    row = sorted((k for k in hall.seats if k[:2] == ("Партер", "5")), key=lambda k: hall.seats[k][0])
    pocket = row[:12]                                    # свободный «карман» из 12 мест, остальное занято
    occ = set(hall.seats) - set(pocket)
    best = ap.find_blocks(g, occ, 10, ("Партер",))[0]
    assert set(best.keys) in (set(pocket[:10]), set(pocket[2:]))   # 2–11 оставило бы два одиночных места


def test_split_fallback_when_hall_is_fragmented():
    hall = halls.get("congress")
    g = hall.grid
    free = set()
    for z, r in (("Партер", "1"), ("Партер", "15"), ("Балкон", "4")):   # три далёких кармана
        pocket = sorted((k for k in hall.seats if k[:2] == (z, r)), key=lambda k: float(k[2]))[3:15]
        free |= set(pocket)                                              # по 12 мест
    occ = set(hall.seats) - free
    assert ap.find_blocks(g, occ, 30) == []
    parts = ap.find_split(g, occ, 30)
    assert parts and sum(len(b.keys) for b in parts) == 30
    assert not (set(k for b in parts for k in b.keys) - free)
