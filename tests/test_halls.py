"""Несколько залов: реестр, выбор зала в сеансе, миграция старой базы."""
import os
import pathlib
import sqlite3
import sys
import tempfile
from urllib.parse import quote

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import halls  # noqa: E402
from main import create_app  # noqa: E402

H = {"X-Operator": quote("Анна")}


def _make_svg(path, rows=3, per_row=6):
    """Крошечный зал: зона «Партер» (3 ряда × 6 мест) и зона «Ложа» без рядов (4 места)."""
    c = []
    for r in range(1, rows + 1):
        for s in range(1, per_row + 1):
            c.append(f'<circle data-z="Партер" data-r="{r}" data-s="{s}" data-x="{50 + s * 20}" data-y="{250 - r * 22}" r="8"/>')
    for s in range(1, 5):
        c.append(f'<circle data-z="Ложа" data-s="{s}" data-x="{20}" data-y="{50 + s * 20}" r="8"/>')
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300"><g>' + "".join(c) + "</g></svg>",
                    encoding="utf-8")


_tmp = tempfile.mkdtemp()
_svg = os.path.join(_tmp, "mini.svg")
_make_svg(pathlib.Path(_svg))
if "mini" not in [h.key for h in halls.all_halls()]:
    halls.register(key="mini", name="Мини-зал", svg=_svg, default_zones=("Партер",),
                   extra_zones_label="с ложами", zone_bias={"Партер": 0.0}, pitch=20.0, row_link=30.0)


@pytest.fixture()
def client():
    return create_app("sqlite://").test_client()


@pytest.fixture()
def eid(client):
    return client.post("/api/events", json={"name": "Концерт"}).json["id"]


def seat(z, r, s):
    return {"zone": z, "row": r, "seat": s}


def new_session(client, eid, **kw):
    r = client.post(f"/api/events/{eid}/sessions", headers=H, json={"starts_at": "2026-12-20T10:00", **kw})
    assert r.status_code == 201, r.json
    return r.json["id"]


def test_registry_lists_halls_and_default_is_first(client):
    keys = [h["key"] for h in client.get("/api/halls").json]
    assert keys[0] == "congress" and "mini" in keys
    mini = next(h for h in client.get("/api/halls").json if h["key"] == "mini")
    assert mini["total"] == 22 and (mini["width"], mini["height"]) == (400, 300)
    assert [z["has_rows"] for z in mini["zones"]] == [True, False]
    assert mini["extra_zones_label"] == "с ложами"


def test_svg_endpoint_and_etag(client):
    r = client.get("/api/halls/mini/svg")
    assert r.status_code == 200 and r.mimetype == "image/svg+xml" and b"<circle" in r.data
    assert client.get("/api/halls/mini/svg", headers={"If-None-Match": r.headers["ETag"]}).status_code == 304
    assert client.get("/api/halls/nope/svg").status_code == 400


def test_session_defaults_to_congress_and_keeps_old_behaviour(client, eid):
    sid = new_session(client, eid)
    st = client.get(f"/api/sessions/{sid}/state").json
    assert st["session"]["hall"] == "congress" and st["stats"]["total"] == 1331


def test_session_in_other_hall_uses_its_own_seats_and_zones(client, eid):
    sid = new_session(client, eid, hall="mini", base_price=100, zone_prices={"Ложа": 300, "Партер": 150, "Балкон": 5})
    st = client.get(f"/api/sessions/{sid}/state").json
    assert st["stats"]["total"] == 22
    assert st["session"]["zone_prices"] == {"Ложа": 300, "Партер": 150}      # чужая зона «Балкон» отброшена

    ok = client.post(f"/api/sessions/{sid}/orders", headers=H,
                     json={"name": "А", "seats": [seat("Партер", "1", "1"), seat("Ложа", "", "2")]})
    assert ok.status_code == 201 and ok.json["total_amount"] == 150 + 300
    # места Конгресс-холла в мини-зале не существуют
    bad = client.post(f"/api/sessions/{sid}/orders", headers=H, json={"name": "Б", "seats": [seat("Партер", "20", "5")]})
    assert bad.status_code == 400
    # и наоборот: зона мини-зала не существует в Конгресс-холле
    cong = new_session(client, eid)
    assert client.post(f"/api/sessions/{cong}/orders", headers=H,
                       json={"name": "В", "seats": [seat("Ложа", "", "2")]}).status_code == 400


def test_row_rule_for_foreign_zone_rejected(client, eid):
    sid = new_session(client, eid, hall="mini")
    r = client.patch(f"/api/sessions/{sid}", headers=H,
                     json={"row_prices": [{"zone": "Балкон", "row_from": 1, "row_to": 2, "price": 1}]})
    assert r.status_code == 400 and "Мини-зал" in r.json["error"]


def test_unknown_hall_rejected(client, eid):
    r = client.post(f"/api/events/{eid}/sessions", headers=H, json={"starts_at": "2026-12-20T10:00", "hall": "nope"})
    assert r.status_code == 400


def test_hall_can_change_only_while_empty(client, eid):
    sid = new_session(client, eid, zone_prices={"Балкон": 300, "Партер": 500})
    st = client.patch(f"/api/sessions/{sid}", headers=H, json={"hall": "mini"}).json
    assert st["session"]["hall"] == "mini" and st["stats"]["total"] == 22
    assert st["session"]["zone_prices"] == {"Партер": 500}                    # «Балкон» в мини-зале нет

    client.post(f"/api/sessions/{sid}/orders", headers=H, json={"name": "А", "seats": [seat("Партер", "1", "1")]})
    r = client.patch(f"/api/sessions/{sid}", headers=H, json={"hall": "congress"})
    assert r.status_code == 409
    # тот же зал с занятыми местами — не смена, ошибки нет
    assert client.patch(f"/api/sessions/{sid}", headers=H, json={"hall": "mini", "base_price": 10}).status_code == 200


def test_same_seat_key_in_two_halls_does_not_clash(client, eid):
    a, b = new_session(client, eid, hall="congress"), new_session(client, eid, hall="mini")
    for sid in (a, b):
        assert client.post(f"/api/sessions/{sid}/orders", headers=H,
                           json={"name": "А", "seats": [seat("Партер", "1", "1")]}).status_code == 201


def test_autopick_respects_hall(client, eid):
    sid = new_session(client, eid, hall="mini")
    a = client.post(f"/api/sessions/{sid}/autopick", json={"count": 5}).json
    assert len(a["seats"]) == 5 and {s["zone"] for s in a["seats"]} == {"Партер"}   # default_zones = только Партер
    too_many = client.post(f"/api/sessions/{sid}/autopick", json={"count": 19})      # в Партере всего 18
    assert too_many.status_code == 409 and too_many.json["free"] == 18
    # Партер занят целиком: в зонах по умолчанию мест нет, а с «дополнительной» зоной находится блок в Ложе
    parter = [seat("Партер", str(r), str(s)) for r in range(1, 4) for s in range(1, 7)]
    client.post(f"/api/sessions/{sid}/orders", headers=H, json={"name": "Все", "seats": parter})
    assert client.post(f"/api/sessions/{sid}/autopick", json={"count": 3}).status_code == 409
    both = client.post(f"/api/sessions/{sid}/autopick", json={"count": 3, "zones": ["Партер", "Ложа"]})
    assert both.status_code == 200 and {s["zone"] for s in both.json["seats"]} == {"Ложа"}


def test_events_list_has_hall(client, eid):
    new_session(client, eid, hall="mini")
    assert client.get("/api/events").json[0]["sessions"][0]["hall"] == "mini"


def test_csv_uses_hall_zone_order(client, eid):
    sid = new_session(client, eid, hall="mini")
    oid = client.post(f"/api/sessions/{sid}/orders", headers=H, json={
        "name": "А", "seats": [seat("Ложа", "", "1"), seat("Партер", "2", "3")]}).json["id"]
    lines = client.get(f"/api/orders/{oid}/export.csv").data.decode("utf-8-sig").strip().split("\r\n")
    assert lines[1].split(";")[2] == "Партер" and lines[2].split(";")[2] == "Ложа"


def test_registration_validation(tmp_path):
    p = tmp_path / "x.svg"
    _make_svg(p)
    with pytest.raises(ValueError, match="default_zones"):
        halls.Hall("x", "X", p, default_zones=("Нет такой",))
    with pytest.raises(ValueError, match="zone_bias"):
        halls.Hall("x", "X", p, zone_bias={"Нет такой": 1})
    with pytest.raises(ValueError, match="уже зарегистрирован"):
        halls.register(key="mini", name="Дубль", svg=p)
    bad = tmp_path / "bad.svg"
    bad.write_text('<svg viewBox="10 10 5 5"><circle data-z="A" data-s="1" data-x="1" data-y="1"/></svg>', encoding="utf-8")
    with pytest.raises(ValueError, match="viewBox"):
        halls.Hall("y", "Y", bad)


def test_old_database_is_migrated_to_congress_hall(tmp_path):
    """База, созданная до появления выбора зала: колонки hall нет — после запуска все сеансы в Конгресс-холле."""
    db_path = tmp_path / "old.db"
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE events (id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL, description TEXT, created_at DATETIME NOT NULL);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, starts_at DATETIME NOT NULL,
            base_price INTEGER NOT NULL, zone_prices JSON NOT NULL, row_prices JSON NOT NULL, created_at DATETIME NOT NULL);
        INSERT INTO events VALUES (1, 'Ёлка', '', '2026-01-01 00:00:00');
        INSERT INTO sessions VALUES (1, 1, '2026-12-20 10:00:00', 500, '{}', '[]', '2026-01-01 00:00:00');
    """)
    con.commit(); con.close()
    c = create_app(f"sqlite:///{db_path}").test_client()
    st = c.get("/api/sessions/1/state").json
    assert st["session"]["hall"] == "congress" and st["stats"]["total"] == 1331
    # и после миграции работает как обычно
    assert c.post("/api/sessions/1/orders", headers=H, json={"name": "А", "seats": [seat("Партер", "1", "5")]}).status_code == 201


def test_conference_hall_numbering():
    """Конференц-зал: 20 рядов, в каждом места с 1 подряд, нумерация справа налево, ряд 1 у сцены (внизу)."""
    h = halls.get("conference")
    assert h.total == 469
    for r in range(1, 21):
        nums = sorted(int(k[2]) for k in h.seats if k[1] == str(r))
        assert nums == list(range(1, len(nums) + 1))
    xs = [h.seats[("Партер", "1", str(s))][0] for s in range(1, 25)]
    assert xs == sorted(xs, reverse=True)                                   # 1 — самое правое
    assert h.seats[("Партер", "1", "1")][1] > h.seats[("Партер", "20", "1")][1]   # ряд 1 ниже ряда 20
