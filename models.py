"""Часть 1. Модели SQLAlchemy (Flask-SQLAlchemy 3.x).

Event ─┬─< ShowSession ─┬─< Order ─< SeatBooking
       │                └─< AuditLog

Схема зала выбирается на сеанс (ShowSession.hall — ключ из halls.py); состояние мест
живёт только в SeatBooking и всегда привязано к сеансу.

Класс сеанса назван ShowSession (а не Session), чтобы не путать его
с db.session и flask.session.
"""
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, UniqueConstraint, event
from sqlalchemy.engine import Engine

import halls

db = SQLAlchemy()

RESERVED = "reserved"  # забронирован
PAID = "paid"          # оплачен
STATUSES = (RESERVED, PAID)


def utcnow() -> datetime:
    """Наивный UTC (SQLite всё равно не хранит tz). В JSON уходит с суффиксом Z."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@event.listens_for(Engine, "connect")
def _sqlite_fk_on(dbapi_conn, _):
    # В SQLite внешние ключи по умолчанию выключены.
    if dbapi_conn.__class__.__module__.startswith("sqlite3"):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


class Event(db.Model):
    """Мероприятие, например «Новогодняя ёлка»."""
    __tablename__ = "events"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    sessions = db.relationship(
        "ShowSession", back_populates="event",
        cascade="all, delete-orphan", order_by="ShowSession.starts_at",
    )


class ShowSession(db.Model):
    """Сеанс: конкретная дата и время внутри мероприятия."""
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    starts_at = db.Column(db.DateTime, nullable=False)  # местное «настенное» время
    # Ключ зала из реестра halls.py. Меняется только пока в сеансе нет ни одного занятого места.
    hall = db.Column(db.String(40), nullable=False, default=lambda: halls.default().key)
    # Цена билета (целые единицы валюты), по возрастанию специфичности:
    # base_price  →  zone_prices (цена по зоне: {"Партер": 500, "Балкон": 300})
    #             →  row_prices (цена по диапазону рядов внутри зоны, см. ниже)
    #             →  Order.price_override (своя цена для конкретного заказа)
    base_price = db.Column(db.Integer, nullable=False, default=0)
    zone_prices = db.Column(db.JSON, nullable=False, default=dict)
    # Правила по рядам, в порядке приоритета (первое подходящее побеждает):
    # [{"zone": "Партер", "row_from": 1, "row_to": 6, "price": 250}, ...]
    # Применяются только к местам с числовым data-r (боковые балконы его не имеют).
    row_prices = db.Column(db.JSON, nullable=False, default=list)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    event = db.relationship("Event", back_populates="sessions")
    orders = db.relationship("Order", back_populates="session",
                             cascade="all, delete-orphan", order_by="Order.id")
    audit = db.relationship("AuditLog", cascade="all, delete-orphan",
                            order_by="AuditLog.id")


class Order(db.Model):
    """Заказ школы на сеанс. Цвет и статус определяют вид мест на схеме."""
    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    comment = db.Column(db.Text, default="")
    color = db.Column(db.String(7), nullable=False)          # базовый (насыщенный) #rrggbb
    status = db.Column(db.String(10), nullable=False, default=RESERVED)
    price_override = db.Column(db.Integer, nullable=True)    # особая цена билета в этом заказе
    total_amount = db.Column(db.Integer, nullable=False, default=0)  # кэш, см. services.recalc_order
    # Билеты заказа напечатаны. Сбрасывается, когда в заказ добавляют места (их надо допечатать).
    printed = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    session = db.relationship("ShowSession", back_populates="orders")
    seats = db.relationship("SeatBooking", back_populates="order",
                            cascade="all, delete-orphan", order_by="SeatBooking.id")


class SeatBooking(db.Model):
    """Место в заказе. Одно место на сеанс можно занять только один раз —
    это гарантирует UNIQUE, а не только проверки в коде (защита от гонок)."""
    __tablename__ = "seat_bookings"
    __table_args__ = (
        UniqueConstraint("session_id", "zone", "row", "seat", name="uq_seat_per_session"),
        Index("ix_seat_order", "order_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id", ondelete="CASCADE"),
                           nullable=False)  # денормализация ради UNIQUE
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id", ondelete="CASCADE"),
                         nullable=False)
    zone = db.Column(db.String(40), nullable=False)          # data-z
    row = db.Column(db.String(8), nullable=False, default="")  # data-r ('' у боковых балконов; NULL нельзя — ломает UNIQUE)
    seat = db.Column(db.String(8), nullable=False)           # data-s (строка: в схеме есть «0.1»)
    is_free = db.Column(db.Boolean, nullable=False, default=False)  # бесплатное (сопровождающий)

    order = db.relationship("Order", back_populates="seats")


class AuditLog(db.Model):
    """Журнал действий по сеансу. order_id — без FK: история переживает удаление заказа."""
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    timestamp = db.Column(db.DateTime, default=utcnow, nullable=False)
    actor = db.Column(db.String(64), nullable=False, default="")
    action_text = db.Column(db.String(500), nullable=False)
    order_id = db.Column(db.Integer, nullable=True)
