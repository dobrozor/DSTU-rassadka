"""Резервная копия базы в Telegram (pyTelegramBotAPI / telebot).

• автоматически — каждый день в 12:00 и 00:00 (по времени компьютера, где запущен сервер);
• по запросу — кнопка «📦 Получить базу» и команда /db в меню бота.

Настройка — переменные окружения:
    TG_BOT_TOKEN   токен бота от @BotFather
    TG_CHAT_ID     ваш chat id. Бот отвечает ТОЛЬКО этому чату — иначе любой, кто найдёт бота,
                   смог бы скачать базу
    BACKUP_TIMES   (необязательно) время автоотправки через запятую, по умолчанию "12:00,00:00"
"""
import datetime
import os
import sqlite3
import tempfile
import threading
import time

import telebot
from telebot import types

from models import db

TOKEN = "ТОКЕН_БОТА"
CHAT_ID = 123123123
BTN_TEXT = "📦 Получить базу"


def _parse_times(raw):
    out = []
    for part in raw.split(","):
        hh, mm = part.strip().split(":")
        out.append((int(hh), int(mm)))
    return out


TIMES = _parse_times(os.environ.get("BACKUP_TIMES", "12:00,00:00"))
_lock = threading.Lock()          # кнопка и расписание не должны отправлять одновременно


class BackupError(Exception):
    pass


def _snapshot(db_path):
    """Согласованная копия базы. sqlite3.backup безопасен, даже если в базу в этот момент пишут
    (простое копирование файла могло бы поймать её на середине записи)."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return tmp


def send_backup(app, reason, chat_id=None):
    """Делает копию базы и отправляет файлом в Telegram."""
    if not (TOKEN and CHAT_ID):
        raise BackupError("Не заданы TG_BOT_TOKEN и TG_CHAT_ID")
    with _lock, app.app_context():
        db_path = db.engine.url.database
        if not db_path or not os.path.exists(db_path):
            raise BackupError("Файл базы не найден")
        tmp = _snapshot(db_path)
        try:
            now = datetime.datetime.now()
            with open(tmp, "rb") as f:
                telebot.TeleBot(TOKEN).send_document(
                    chat_id or CHAT_ID, f, visible_file_name=f"hall_{now:%Y-%m-%d_%H-%M}.db",
                    caption=f"Копия базы · {now:%d.%m.%Y %H:%M} · {reason}")
        except BackupError:
            raise
        except Exception as e:                      # сеть, неверный токен/чат и т.п.
            raise BackupError(f"Telegram не принял файл: {e}")
        finally:
            os.remove(tmp)


def _next_run(now, times=None):
    """Ближайший момент после now из списка времён суток."""
    cands = []
    for hh, mm in times or TIMES:
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t <= now:
            t += datetime.timedelta(days=1)
        cands.append(t)
    return min(cands)


def _schedule_loop(app):
    while True:
        t = _next_run(datetime.datetime.now())
        while (left := (t - datetime.datetime.now()).total_seconds()) > 0:
            time.sleep(min(left, 30))               # короткими шагами: переживает смену времени/сон компьютера
        try:
            send_backup(app, "по расписанию")
        except Exception as e:                      # поток не должен умирать из-за разового сбоя сети
            print("[backup] ошибка отправки по расписанию:", e)
        time.sleep(1)


def _build_bot(app):
    bot = telebot.TeleBot(TOKEN)
    mine = lambda m: m.chat.id == int(CHAT_ID)      # чужим не отвечаем вообще

    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton(BTN_TEXT))

    @bot.message_handler(commands=["start", "menu"], func=mine)
    def on_start(m):
        times = ", ".join(f"{h:02d}:{mm:02d}" for h, mm in TIMES)
        bot.send_message(m.chat.id, f"База отправляется автоматически в {times}.\n"
                                    f"Чтобы получить её сейчас — кнопка «{BTN_TEXT}» или /db.", reply_markup=kb)

    @bot.message_handler(commands=["db"], func=mine)
    @bot.message_handler(func=lambda m: mine(m) and m.text == BTN_TEXT)
    def on_db(m):
        try:
            send_backup(app, "по запросу", chat_id=m.chat.id)
        except BackupError as e:
            bot.send_message(m.chat.id, f"Не получилось: {e}")

    return bot


def start(app):
    """Запускает бота (приём команд) и расписание в фоновых потоках."""
    if not (TOKEN and CHAT_ID):
        print("[backup] выключено: не заданы TG_BOT_TOKEN и TG_CHAT_ID")
        return
    bot = _build_bot(app)
    try:   # пункт в меню бота (кнопка «Меню» слева от поля ввода)
        bot.set_my_commands([types.BotCommand("db", "📦 Получить базу данных"),
                             types.BotCommand("start", "Показать кнопку")])
    except Exception as e:
        print("[backup] не удалось задать меню бота:", e)
    threading.Thread(target=lambda: bot.infinity_polling(skip_pending=True), name="tg-bot", daemon=True).start()
    threading.Thread(target=_schedule_loop, args=(app,), name="db-backup", daemon=True).start()
    print("[backup] бот запущен; автоотправка в", ", ".join(f"{h:02d}:{m:02d}" for h, m in TIMES))
