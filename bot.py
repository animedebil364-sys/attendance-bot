import os
import sqlite3
import asyncio
from datetime import datetime, date
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в Variables Railway")


# Если укажешь свой Telegram ID здесь через Railway Variables,
# только ты сможешь назначать/снимать старосту.
#
# Например:
# OWNER_ID = 123456789
#
# Если переменная не задана — первый пользователь сможет
# нажать "Стать старостой".
OWNER_ID = os.getenv("OWNER_ID")

if OWNER_ID:
    OWNER_ID = int(OWNER_ID)


DB_FILE = "attendance.db"


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row


def init_db():
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            subgroup INTEGER,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            lesson_id INTEGER NOT NULL,
            lesson_date TEXT NOT NULL,
            subject TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            marked_at TEXT NOT NULL,
            UNIQUE(telegram_id, lesson_id, lesson_date)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            subject TEXT NOT NULL,
            teacher TEXT,
            room TEXT,
            lesson_type TEXT,
            subgroup INTEGER,
            created_by INTEGER,
            is_custom INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    db.commit()


init_db()


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return date.today().strftime("%Y-%m-%d")


def save_user(message: Message):
    user = message.from_user

    full_name = user.full_name or ""
    username = user.username or ""

    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO users (
            telegram_id,
            username,
            full_name,
            subgroup,
            created_at
        )
        VALUES (?, ?, ?, NULL, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name
    """, (
        user.id,
        username,
        full_name,
        now_str()
    ))

    db.commit()


def get_user_subgroup(user_id: int) -> Optional[int]:
    cursor = db.cursor()

    cursor.execute(
        "SELECT subgroup FROM users WHERE telegram_id = ?",
        (user_id,)
    )

    row = cursor.fetchone()

    if not row:
        return None

    return row["subgroup"]


def set_user_subgroup(user_id: int, subgroup: int):
    cursor = db.cursor()

    cursor.execute("""
        UPDATE users
        SET subgroup = ?
        WHERE telegram_id = ?
    """, (
        subgroup,
        user_id
    ))

    db.commit()


def get_starosta_id() -> Optional[int]:
    cursor = db.cursor()

    cursor.execute(
        "SELECT value FROM settings WHERE key = 'starosta_id'"
    )

    row = cursor.fetchone()

    if not row:
        return None

    try:
        return int(row["value"])
    except Exception:
        return None


def set_starosta_id(user_id: int):
    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO settings(key, value)
        VALUES('starosta_id', ?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
    """, (str(user_id),))

    db.commit()


def remove_starosta():
    cursor = db.cursor()

    cursor.execute(
        "DELETE FROM settings WHERE key = 'starosta_id'"
    )

    db.commit()


def is_starosta(user_id: int) -> bool:
    starosta = get_starosta_id()

    return starosta is not None and starosta == user_id


def is_owner(user_id: int) -> bool:
    return OWNER_ID is not None and OWNER_ID == user_id


# ============================================================
# РАСПИСАНИЕ
# ============================================================

# Формат:
#
# (
#   дата,
#   начало,
#   конец,
#   предмет,
#   преподаватель,
#   аудитория,
#   тип,
#   подгруппа
# )
#
# Подгруппа:
# 1 = первая
# 2 = вторая
# 0 = общая


SCHEDULE = [

    # ========================================================
    # 07.09.2026 ПОНЕДЕЛЬНИК
    # ========================================================

    (
        "2026-09-07",
        "15:20",
        "16:50",
        "Инженерная геология",
        "Чернышов Валерий Иванович",
        "к. 3, ауд. 119",
        "Лабораторная",
        1
    ),

    (
        "2026-09-07",
        "17:00",
        "18:30",
        "Инженерная геология",
        "Чернышов Валерий Иванович",
        "к. 3, ауд. 119",
        "Лабораторная",
        1
    ),


    # ========================================================
    # 08.09.2026 ВТОРНИК
    # ========================================================

    (
        "2026-09-08",
        "08:00",
        "09:30",
        "Инженерная геология",
        "ст.пр. Чернышов Валерий Иванович",
        "к. 4, ауд. 2а",
        "Лекция",
        0
    ),

    (
        "2026-09-08",
        "09:40",
        "11:10",
        "Физика",
        "ст.пр. Соловьёв Александр Сергеевич",
        "к. 2, ауд. 303",
        "Лекция",
        0
    ),

    (
        "2026-09-08",
        "11:40",
        "13:10",
        "Иностранный язык",
        "доц. Чубарова Наталья Андреевна",
        "к. 5, ауд. 322",
        "Практика",
        1
    ),

    (
        "2026-09-08",
        "13:20",
        "14:50",
        "Иностранный язык",
        "доц. Чубарова Наталья Андреевна",
        "к. 5, ауд. 322",
        "Практика",
        1
    ),


    # ========================================================
    # 09.09.2026 СРЕДА
    # ========================================================

    (
        "2026-09-09",
        "11:40",
        "13:10",
        "История России",
        "зав.каф. Саблин Василий Анатольевич",
        "к. 4, ауд. 1а",
        "Лекция",
        0
    ),

    (
        "2026-09-09",
        "13:20",
        "14:50",
        "Инженерная графика",
        "доц. Шашкова Лола Эдуардовна",
        "к. 4, ауд. 1а",
        "Лекция",
        0
    ),


    # ========================================================
    # 10.09.2026 ЧЕТВЕРГ
    # ========================================================

    (
        "2026-09-10",
        "09:40",
        "11:10",
        "Физическая культура и спорт",
        "ст.пр. Королева Ирина Валентиновна",
        "к. с/к 1, ауд. спорт. корпус",
        "Практика",
        1
    ),

    (
        "2026-09-10",
        "09:40",
        "11:10",
        "Физическая культура и спорт",
        "ст.пр. Соколова Ирина Юрьевна",
        "к. с/к 1, ауд. спорт. корпус",
        "Практика",
        2
    ),

    (
        "2026-09-10",
        "09:40",
        "11:10",
        "Физическая культура и спорт",
        "ст.пр. Королева Ирина Валентиновна",
        "к. с/к 1, ауд. спорт. корпус",
        "Практика",
        0
    ),

    (
        "2026-09-10",
        "13:20",
        "14:50",
        "Физика",
        "ст.пр. Соловьёв Александр Сергеевич",
        "к. 2, ауд. 321",
        "Практика",
        0
    ),


    # ========================================================
    # 11.09.2026 ПЯТНИЦА
    # ========================================================

    (
        "2026-09-11",
        "08:00",
        "09:30",
        "Русский язык и деловая коммуникация",
        "ст.пр. Голубева Анастасия Анатольевна",
        "к. 1, ауд. 413",
        "Практика",
        0
    ),

    (
        "2026-09-11",
        "09:40",
        "11:10",
        "История России",
        "доц. Ильина Ольга Викторовна",
        "к. 1, ауд. 415",
        "Практика",
        0
    ),

    (
        "2026-09-11",
        "11:40",
        "13:10",
        "Основы российской государственности",
        "доц. Желтов Андрей Александрович",
        "к. 1, ауд. 413",
        "Практика",
        0
    ),


    # ========================================================
    # 12.09.2026 СУББОТА
    # ========================================================

    (
        "2026-09-12",
        "08:00",
        "09:30",
        "Основы российской государственности",
        "доц. Желтов Андрей Александрович",
        "к. 2, ауд. 303",
        "Лекция",
        0
    ),

    (
        "2026-09-12",
        "09:40",
        "11:10",
        "Основы российской государственности",
        "доц. Желтов Андрей Александрович",
        "к. 2, ауд. 303",
        "Лекция",
        0
    ),


    # ========================================================
    # 14.09.2026 ПОНЕДЕЛЬНИК
    # ========================================================

    (
        "2026-09-14",
        "08:00",
        "09:30",
        "Физика",
        "ст.пр. Соловьёв Александр Сергеевич",
        "к. 2, ауд. 217",
        "Лабораторная",
        1
    ),

    (
        "2026-09-14",
        "09:40",
        "11:10",
        "Физика",
        "ст.пр. Соловьёв Александр Сергеевич",
        "к. 2, ауд. 217",
        "Лабораторная",
        1
    ),

    (
        "2026-09-14",
        "15:20",
        "16:50",
        "Инженерная геология",
        "ст.пр. Чернышов Валерий Иванович",
        "к. 3, ауд. 119",
        "Лабораторная",
        2
    ),

    (
        "2026-09-14",
        "17:00",
        "18:30",
        "Инженерная геология",
        "ст.пр. Чернышов Валерий Иванович",
        "к. 3, ауд. 119",
        "Лабораторная",
        2
    ),


    # ========================================================
    # 15.09.2026 ВТОРНИК
    # ========================================================

    (
        "2026-09-15",
        "11:40",
        "13:10",
        "Иностранный язык",
        "доц. Чубарова Наталья Андреевна",
        "к. 5, ауд. 322",
        "Практика",
        2
    ),

    (
        "2026-09-15",
        "13:20",
        "14:50",
        "Иностранный язык",
        "доц. Чубарова Наталья Андреевна",
        "к. 5, ауд. 322",
        "Практика",
        2
    ),


    # ========================================================
    # 16.09.2026 СРЕДА
    # ========================================================

    (
        "2026-09-16",
        "11:40",
        "13:10",
        "История России",
        "зав.каф. Саблин Василий Анатольевич",
        "к. 4, ауд. 1а",
        "Лекция",
        0
    ),

    (
        "2026-09-16",
        "13:20",
        "14:50",
        "Инженерная графика",
        "доц. Шашкова Лола Эдуардовна",
        "к. 4, ауд. 1а",
        "Лекция",
        0
    ),


    # ========================================================
    # 17.09.2026 ЧЕТВЕРГ
    # ========================================================

    (
        "2026-09-17",
        "08:00",
        "09:30",
        "Высшая математика",
        "доц. Кочкарева Татьяна Александровна",
        "к. 1, ауд. 407",
        "Практика",
        0
    ),

    (
        "2026-09-17",
        "09:40",
        "11:10",
        "Физическая культура и спорт",
        "ст.пр. Королева Ирина Валентиновна",
        "к. с/к 1, ауд. спорт. корпус",
        "Практика",
        1
    ),

    (
        "2026-09-17",
        "09:40",
        "11:10",
        "Физическая культура и спорт",
        "ст.пр. Соколова Ирина Юрьевна",
        "к. с/к 1, ауд. спорт. корпус",
        "Практика",
        2
    ),

    (
        "2026-09-17",
        "11:40",
        "13:10",
        "Высшая математика",
        "доц. Кочкарева Татьяна Александровна",
        "к. 1, ауд. 401",
        "Лекция",
        0
    ),


    # ========================================================
    # 18.09.2026 ПЯТНИЦА
    # ========================================================

    (
        "2026-09-18",
        "08:00",
        "09:30",
        "Русский язык и деловая коммуникация",
        "ст.пр. Голубева Анастасия Анатольевна",
        "к. 1, ауд. 413",
        "Практика",
        0
    ),

    (
        "2026-09-18",
        "09:40",
        "11:10",
        "История России",
        "доц. Ильина Ольга Викторовна",
        "к. 1, ауд. 415",
        "Практика",
        0
    ),

    (
        "2026-09-18",
        "11:40",
        "13:10",
        "Основы российской государственности",
        "доц. Желтов Андрей Александрович",
        "к. 1, ауд. 413",
        "Практика",
        0
    ),

    (
        "2026-09-18",
        "13:20",
        "14:50",
        "Информационные технологии и основы искусственного интеллекта",
        "доц. Ганиева Елена Михайловна",
        "к. 4, ауд. 1а",
        "Лекция",
        0
    ),


    # ========================================================
    # 19.09.2026 СУББОТА
    # ========================================================

    (
        "2026-09-19",
        "08:00",
        "09:30",
        "Основы российской государственности",
        "доц. Желтов Андрей Александрович",
        "к. 2, ауд. 303",
        "Лекция",
        0
    ),

    (
        "2026-09-19",
        "09:40",
        "11:10",
        "Основы российской государственности",
        "доц. Желтов Андрей Александрович",
        "к. 2, ауд. 303",
        "Лекция",
        0
    ),


    # ========================================================
    # 21.09.2026 ПОНЕДЕЛЬНИК
    # ========================================================

    (
        "2026-09-21",
        "08:00",
        "09:30",
        "Физика",
        "ст.пр. Соловьёв Александр Сергеевич",
        "к. 2, ауд. 217",
        "Лабораторная",
        2
    ),

    (
        "2026-09-21",
        "09:40",
        "11:10",
        "Физика",
        "ст.пр. Соловьёв Александр Сергеевич",
        "к. 2, ауд. 217",
        "Лабораторная",
        2
    ),

    (
        "2026-09-21",
        "15:20",
        "16:50",
        "Инженерная геология",
        "ст.пр. Чернышов Валерий Иванович",
        "к. 3, ауд. 119",
        "Лабораторная",
        1
    ),

    (
        "2026-09-21",
        "17:00",
        "18:30",
        "Инженерная геология",
        "ст.пр. Чернышов Валерий Иванович",
        "к. 3, ауд. 119",
        "Лабораторная",
        1
    ),


    # ========================================================
    # 22.09.2026 ВТОРНИК
    # ========================================================

    (
        "2026-09-22",
        "08:00",
        "09:30",
        "Инженерная геология",
        "ст.пр. Чернышов Валерий Иванович",
        "к. 4, ауд. 2а",
        "Лекция",
        0
    ),

    (
        "2026-09-22",
        "09:40",
        "11:10",
        "Физика",
        "ст.пр. Соловьёв Александр Сергеевич",
        "к. 2, ауд. 303",
        "Лекция",
        0
    ),

    (
        "2026-09-22",
        "11:40",
        "13:10",
        "Иностранный язык",
        "доц. Чубарова Наталья Андреевна",
        "к. 5, ауд. 322",
        "Практика",
        1
    ),

    (
        "2026-09-22",
        "13:20",
        "14:50",
        "Иностранный язык",
        "доц. Чубарова Наталья Андреевна",
        "к. 5, ауд. 322",
        "Практика",
        1
    ),


    # ========================================================
    # 23.09.2026 СРЕДА
    # ========================================================

    (
        "2026-09-23",
        "11:40",
        "13:10",
        "История России",
        "зав.каф. Саблин Василий Анатольевич",
        "к. 4, ауд. 1а",
        "Лекция",
        0
    ),

    (
        "2026-09-23",
        "13:20",
        "14:50",
        "Инженерная графика",
        "доц. Шашкова Лола Эдуардовна",
        "к. 4, ауд. 1а",
        "Лекция",
        0
    ),


    # ========================================================
    # 24.09.2026 ЧЕТВЕРГ
    # ========================================================

    (
        "2026-09-24",
        "08:00",
        "09:30",
        "Высшая математика",
        "доц. Кочкарева Татьяна Александровна",
        "к. 1, ауд. 407",
        "Практика",
        0
    ),

    (
        "2026-09-24",
        "09:40",
        "11:10",
        "Физическая культура и спорт",
        "ст.пр. Королева Ирина Валентиновна",
        "к. с/к 1, ауд. спорт. корпус",
        "Практика",
        1
    ),

    (
        "2026-09-24",
        "09:40",
        "11:10",
        "Физическая культура и спорт",
        "ст.пр. Соколова Ирина Юрьевна",
        "к. с/к 1, ауд. спорт. корпус",
        "Практика",
        2
    ),

    (
        "2026-09-24",
        "11:40",
        "13:10",
        "Высшая математика",
        "доц. Кочкарева Татьяна Александровна",
        "к. 1, ауд. 401",
        "Лекция",
        0
    ),

    (
        "2026-09-24",
        "13:20",
        "14:50",
        "Физика",
        "ст.пр. Соловьёв Александр Сергеевич",
        "к. 2, ауд. 321",
        "Практика",
        0
    ),


    # ========================================================
    # 25.09.2026 ПЯТНИЦА
    # ========================================================

    (
        "2026-09-25",
        "08:00",
        "09:30",
        "Русский язык и деловая коммуникация",
        "ст.пр. Голубева Анастасия Анатольевна",
        "к. 1, ауд. 413",
        "Практика",
        0
    ),

    (
        "2026-09-25",
        "09:40",
        "11:10",
        "История России",
        "доц. Ильина Ольга Викторовна",
        "к. 1, ауд. 415",
        "Практика",
        0
    ),

    (
        "2026-09-25",
        "11:40",
        "13:10",
        "Основы российской государственности",
        "доц. Желтов Андрей Александрович",
        "к. 1, ауд. 413",
        "Практика",
        0
    ),


    # ========================================================
    # 26.09.2026 СУББОТА
    # ========================================================

    (
        "2026-09-26",
        "08:00",
        "09:30",
        "Основы российской государственности",
        "доц. Желтов Андрей Александрович",
        "к. 2, ауд. 303",
        "Лекция",
        0
    ),

    (
        "2026-09-26",
        "09:40",
        "11:10",
        "Основы российской государственности",
        "доц. Желтов Андрей Александрович",
        "к. 2, ауд. 303",
        "Лекция",
        0
    ),

    (
        "2026-09-26",
        "11:40",
        "13:10",
        "Физическая культура и спорт",
        "ст.пр. Митрофанова Анастасия Геннадьевна",
        "к. 4, ауд. 1а",
        "Лекция",
        0
    ),

    (
        "2026-09-26",
        "13:20",
        "14:50",
        "Физическая культура и спорт",
        "ст.пр. Митрофанова Анастасия Геннадьевна",
        "к. 4, ауд. 1а",
        "Лекция",
        0
    ),
]


# ============================================================
# ЗАПИСЬ РАСПИСАНИЯ В БАЗУ
# ============================================================

def load_schedule_into_db():
    cursor = db.cursor()

    # Удаляем только автоматически загруженное расписание.
    cursor.execute("""
        DELETE FROM lessons
        WHERE is_custom = 0
    """)

    for lesson in SCHEDULE:
        (
            lesson_date,
            start_time,
            end_time,
            subject,
            teacher,
            room,
            lesson_type,
            subgroup
        ) = lesson

        cursor.execute("""
            INSERT INTO lessons (
                lesson_date,
                start_time,
                end_time,
                subject,
                teacher,
                room,
                lesson_type,
                subgroup,
                created_by,
                is_custom
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            lesson_date,
            start_time,
            end_time,
            subject,
            teacher,
            room,
            lesson_type,
            subgroup,
            0
        ))

    db.commit()


# Загружаем только если база расписания пустая.
cursor = db.cursor()
cursor.execute("SELECT COUNT(*) AS count FROM lessons WHERE is_custom = 0")
schedule_count = cursor.fetchone()["count"]

if schedule_count == 0:
    load_schedule_into_db()


# ============================================================
# МЕНЮ
# ============================================================

def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📅 Расписание"),
                KeyboardButton(text="📌 Сегодня")
            ],
            [
                KeyboardButton(text="👤 Моя группа"),
                KeyboardButton(text="📋 Перекличка")
            ],
            [
                KeyboardButton(text="🗄 База"),
                KeyboardButton(text="📊 Статистика")
            ],
            [
                KeyboardButton(text="👑 Староста"),
                KeyboardButton(text="➕ Добавить пару")
            ],
            [
                KeyboardButton(text="🗑 Удалить пару"),
                KeyboardButton(text="ℹ️ Помощь")
            ],
        ],
        resize_keyboard=True
    )


def subgroup_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="1️⃣ 1-я подгруппа",
                    callback_data="subgroup_1"
                )
            ],
            [
                InlineKeyboardButton(
                    text="2️⃣ 2-я подгруппа",
                    callback_data="subgroup_2"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Общие пары",
                    callback_data="subgroup_0"
                )
            ],
        ]
    )


# ============================================================
# ПОЛУЧЕНИЕ РАСПИСАНИЯ
# ============================================================

def get_lessons_for_date(
    lesson_date: str,
    subgroup: Optional[int] = None
):
    cursor = db.cursor()

    if subgroup is None:
        cursor.execute("""
            SELECT *
            FROM lessons
            WHERE lesson_date = ?
            ORDER BY start_time
        """, (lesson_date,))

    else:
        cursor.execute("""
            SELECT *
            FROM lessons
            WHERE lesson_date = ?
            AND (
                subgroup = 0
                OR subgroup = ?
            )
            ORDER BY start_time
        """, (
            lesson_date,
            subgroup
        ))

    return cursor.fetchall()


def format_lesson(lesson, number: int):
    text = (
        f"<b>{number}. {lesson['start_time']}–{lesson['end_time']}</b>\n"
        f"📚 <b>{lesson['subject']}</b>\n"
    )

    if lesson["teacher"]:
        text += f"👨‍🏫 {lesson['teacher']}\n"

    if lesson["room"]:
        text += f"📍 {lesson['room']}\n"

    if lesson["lesson_type"]:
        text += f"📝 {lesson['lesson_type']}\n"

    if lesson["subgroup"] == 1:
        text += "👤 1-я подгруппа\n"
    elif lesson["subgroup"] == 2:
        text += "👤 2-я подгруппа\n"
    else:
        text += "👥 Общая пара\n"

    return text


def attendance_button(lesson):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Я на паре",
                    callback_data=f"attend_{lesson['id']}"
                )
            ]
        ]
    )


# ============================================================
# START
# ============================================================

async def start_handler(message: Message):
    save_user(message)

    subgroup = get_user_subgroup(message.from_user.id)

    text = (
        "👋 <b>Привет!</b>\n\n"
        "Я бот для расписания и посещаемости.\n\n"
    )

    if subgroup is None:
        text += (
            "Сначала выбери свою подгруппу в разделе "
            "«👤 Моя группа».\n\n"
        )
    else:
        text += (
            f"Твоя подгруппа: <b>{subgroup}</b>\n\n"
        )

    text += (
        "Выбирай нужный раздел кнопками ниже 👇"
    )

    await message.answer(
        text,
        reply_markup=main_menu()
    )


# ============================================================
# МОЯ ГРУППА
# ============================================================

async def my_group_handler(message: Message):
    save_user(message)

    subgroup = get_user_subgroup(message.from_user.id)

    if subgroup:
        await message.answer(
            f"👤 Сейчас выбрана <b>{subgroup}-я подгруппа</b>.\n\n"
            "Если нужно изменить — выбери ниже:",
            reply_markup=subgroup_keyboard()
        )
    else:
        await message.answer(
            "👤 Выбери свою подгруппу:",
            reply_markup=subgroup_keyboard()
        )


async def subgroup_callback(callback: CallbackQuery):
    subgroup = int(callback.data.split("_")[1])

    set_user_subgroup(
        callback.from_user.id,
        subgroup
    )

    if subgroup == 0:
        text = "👥 Теперь выбраны общие пары."
    else:
        text = f"✅ Выбрана <b>{subgroup}-я подгруппа</b>."

    await callback.message.edit_text(text)

    await callback.answer("Сохранено")


# ============================================================
# РАСПИСАНИЕ
# ============================================================

async def schedule_handler(message: Message):
    save_user(message)

    subgroup = get_user_subgroup(message.from_user.id)

    if subgroup is None:
        await message.answer(
            "Сначала выбери свою подгруппу:",
            reply_markup=subgroup_keyboard()
        )
        return

    cursor = db.cursor()

    cursor.execute("""
        SELECT DISTINCT lesson_date
        FROM lessons
        ORDER BY lesson_date
    """)

    dates = [row["lesson_date"] for row in cursor.fetchall()]

    if not dates:
        await message.answer(
            "📅 Расписание пока пустое."
        )
        return

    text = "📅 <b>Расписание</b>\n\n"

    for d in dates:
        lessons = get_lessons_for_date(d, subgroup)

        if not lessons:
            continue

        formatted_date = datetime.strptime(
            d,
            "%Y-%m-%d"
        ).strftime("%d.%m.%Y")

        text += f"🗓 <b>{formatted_date}</b>\n"

        for i, lesson in enumerate(lessons, 1):
            text += format_lesson(lesson, i)
            text += "\n"

        text += "──────────────\n"

    await message.answer(text)


# ============================================================
# СЕГОДНЯ
# ============================================================

async def today_handler(message: Message):
    save_user(message)

    subgroup = get_user_subgroup(message.from_user.id)

    if subgroup is None:
        await message.answer(
            "Сначала выбери свою подгруппу:",
            reply_markup=subgroup_keyboard()
        )
        return

    current_date = today_str()

    lessons = get_lessons_for_date(
        current_date,
        subgroup
    )

    formatted_date = datetime.now().strftime(
        "%d.%m.%Y"
    )

    if not lessons:
        await message.answer(
            f"📌 <b>Сегодня {formatted_date}</b>\n\n"
            "Пар по расписанию нет."
        )
        return

    text = (
        f"📌 <b>Сегодня — {formatted_date}</b>\n\n"
    )

    for i, lesson in enumerate(lessons, 1):
        text += format_lesson(lesson, i)
        text += "\n"

    await message.answer(text)

    # Отдельно показываем кнопки отметки.
    for lesson in lessons:
        await message.answer(
            (
                f"📚 <b>{lesson['subject']}</b>\n"
                f"⏰ {lesson['start_time']}–{lesson['end_time']}"
            ),
            reply_markup=attendance_button(lesson)
        )


# ============================================================
# ОТМЕТКА
# ============================================================

async def attendance_callback(callback: CallbackQuery):
    user_id = callback.from_user.id

    lesson_id = int(
        callback.data.split("_")[1]
    )

    cursor = db.cursor()

    cursor.execute(
        "SELECT * FROM lessons WHERE id = ?",
        (lesson_id,)
    )

    lesson = cursor.fetchone()

    if not lesson:
        await callback.answer(
            "Пара не найдена",
            show_alert=True
        )
        return

    user_subgroup = get_user_subgroup(user_id)

    if user_subgroup is None:
        await callback.answer(
            "Сначала выбери свою подгруппу",
            show_alert=True
        )
        return

    # Проверяем принадлежность к подгруппе.
    if lesson["subgroup"] not in (0, user_subgroup):
        await callback.answer(
            "Эта пара относится к другой подгруппе",
            show_alert=True
        )
        return

    cursor.execute("""
        SELECT id
        FROM attendance
        WHERE telegram_id = ?
        AND lesson_id = ?
        AND lesson_date = ?
    """, (
        user_id,
        lesson_id,
        lesson["lesson_date"]
    ))

    already = cursor.fetchone()

    if already:
        await callback.answer(
            "Ты уже отмечен на этой паре ✅",
            show_alert=True
        )
        return

    cursor.execute("""
        INSERT INTO attendance (
            telegram_id,
            lesson_id,
            lesson_date,
            subject,
            start_time,
            end_time,
            marked_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        lesson_id,
        lesson["lesson_date"],
        lesson["subject"],
        lesson["start_time"],
        lesson["end_time"],
        now_str()
    ))

    db.commit()

    await callback.answer(
        "Ты успешно отмечен! ✅",
        show_alert=True
    )


# ============================================================
# ПЕРЕКЛИЧКА
# ============================================================

async def attendance_list_handler(message: Message):
    save_user(message)

    if not is_starosta(message.from_user.id):
        await message.answer(
            "⛔ Этот раздел доступен только старосте."
        )
        return

    current_date = today_str()

    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM lessons
        WHERE lesson_date = ?
        ORDER BY start_time
    """, (current_date,))

    lessons = cursor.fetchall()

    if not lessons:
        await message.answer(
            "📋 Сегодня пар нет."
        )
        return

    text = (
        f"📋 <b>Перекличка на "
        f"{datetime.now().strftime('%d.%m.%Y')}</b>\n\n"
    )

    for lesson in lessons:

        text += (
            f"📚 <b>{lesson['subject']}</b>\n"
            f"⏰ {lesson['start_time']}–{lesson['end_time']}\n"
        )

        cursor.execute("""
            SELECT
                u.full_name,
                u.username,
                u.subgroup,
                a.marked_at
            FROM attendance a
            JOIN users u
                ON u.telegram_id = a.telegram_id
            WHERE a.lesson_id = ?
            AND a.lesson_date = ?
            ORDER BY u.full_name
        """, (
            lesson["id"],
            current_date
        ))

        students = cursor.fetchall()

        if not students:
            text += "❌ Пока никто не отметился.\n\n"
            continue

        for student in students:
            name = student["full_name"] or "Без имени"

            if student["username"]:
                name += f" (@{student['username']})"

            text += f"✅ {name}\n"

        text += "\n"

    await message.answer(text)


# ============================================================
# СТАТИСТИКА
# ============================================================

async def statistics_handler(message: Message):
    save_user(message)

    if not is_starosta(message.from_user.id):
        await message.answer(
            "⛔ Статистика доступна только старосте."
        )
        return

    cursor = db.cursor()

    cursor.execute("""
        SELECT COUNT(*) AS count
        FROM users
    """)

    total_users = cursor.fetchone()["count"]

    cursor.execute("""
        SELECT COUNT(*) AS count
        FROM attendance
    """)

    total_attendance = cursor.fetchone()["count"]

    cursor.execute("""
        SELECT
            u.full_name,
            u.username,
            COUNT(a.id) AS visits
        FROM users u
        LEFT JOIN attendance a
            ON a.telegram_id = u.telegram_id
        GROUP BY u.telegram_id
        ORDER BY visits DESC, u.full_name
    """)

    students = cursor.fetchall()

    text = (
        "📊 <b>Статистика посещаемости</b>\n\n"
        f"👥 Студентов в базе: <b>{total_users}</b>\n"
        f"✅ Всего отметок: <b>{total_attendance}</b>\n\n"
    )

    if students:
        text += "<b>По студентам:</b>\n\n"

        for student in students:
            name = student["full_name"] or "Без имени"

            if student["username"]:
                name += f" (@{student['username']})"

            text += (
                f"👤 {name}\n"
                f"   Посещений: <b>{student['visits']}</b>\n\n"
            )

    await message.answer(text)


# ============================================================
# БАЗА
# ============================================================

async def database_handler(message: Message):
    save_user(message)

    if not is_starosta(message.from_user.id):
        await message.answer(
            "⛔ База доступна только старосте."
        )
        return

    cursor = db.cursor()

    cursor.execute("""
        SELECT COUNT(*) AS count
        FROM users
    """)

    users_count = cursor.fetchone()["count"]

    cursor.execute("""
        SELECT COUNT(*) AS count
        FROM attendance
    """)

    attendance_count = cursor.fetchone()["count"]

    cursor.execute("""
        SELECT COUNT(*) AS count
        FROM lessons
    """)

    lessons_count = cursor.fetchone()["count"]

    starosta_id = get_starosta_id()

    text = (
        "🗄 <b>База данных</b>\n\n"
        f"👥 Пользователей: <b>{users_count}</b>\n"
        f"📅 Пар в базе: <b>{lessons_count}</b>\n"
        f"✅ Отметок: <b>{attendance_count}</b>\n\n"
    )

    if starosta_id:
        text += f"👑 Староста: <code>{starosta_id}</code>\n"
    else:
        text += "👑 Староста пока не назначен.\n"

    await message.answer(text)


# ============================================================
# СТАРОСТА
# ============================================================

async def starosta_handler(message: Message):
    save_user(message)

    current_starosta = get_starosta_id()

    if is_starosta(message.from_user.id):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Снять себя со старосты",
                        callback_data="remove_starosta"
                    )
                ]
            ]
        )

        await message.answer(
            "👑 <b>Ты сейчас староста.</b>\n\n"
            "Тебе доступны:\n"
            "• перекличка\n"
            "• статистика\n"
            "• база\n"
            "• добавление пар\n"
            "• удаление пар",
            reply_markup=keyboard
        )
        return

    if current_starosta:
        await message.answer(
            "👑 Староста уже назначен."
        )
        return

    if OWNER_ID is not None and message.from_user.id != OWNER_ID:
        await message.answer(
            "⛔ Назначить старосту может только владелец бота."
        )
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👑 Стать старостой",
                    callback_data="become_starosta"
                )
            ]
        ]
    )

    await message.answer(
        "👑 <b>Староста ещё не назначен.</b>\n\n"
        "Если это ты — нажми кнопку:",
        reply_markup=keyboard
    )


async def become_starosta_callback(callback: CallbackQuery):
    user_id = callback.from_user.id

    current = get_starosta_id()

    if current:
        await callback.answer(
            "Староста уже назначен.",
            show_alert=True
        )
        return

    if OWNER_ID is not None and user_id != OWNER_ID:
        await callback.answer(
            "У тебя нет прав на назначение старосты.",
            show_alert=True
        )
        return

    set_starosta_id(user_id)

    await callback.message.edit_text(
        "👑 <b>Ты назначен старостой!</b>\n\n"
        "Теперь тебе доступны перекличка, статистика и база."
    )

    await callback.answer("Готово!")


async def remove_starosta_callback(callback: CallbackQuery):
    user_id = callback.from_user.id

    if not is_starosta(user_id):
        await callback.answer(
            "Ты не являешься старостой.",
            show_alert=True
        )
        return

    remove_starosta()

    await callback.message.edit_text(
        "✅ Ты больше не являешься старостой."
    )

    await callback.answer("Староста снят")


# ============================================================
# ДОБАВЛЕНИЕ ПАРЫ
# ============================================================

# Ввод:
#
# 28.09.2026 | 08:00 | 09:30 | Математика | Иванов И.И. | ауд. 101 | Лекция | 0
#
# subgroup:
# 0 = общая
# 1 = первая
# 2 = вторая


def parse_lesson_text(text: str):
    parts = [
        x.strip()
        for x in text.split("|")
    ]

    if len(parts) != 8:
        return None

    (
        lesson_date,
        start_time,
        end_time,
        subject,
        teacher,
        room,
        lesson_type,
        subgroup
    ) = parts

    try:
        datetime.strptime(
            lesson_date,
            "%d.%m.%Y"
        )

        datetime.strptime(
            start_time,
            "%H:%M"
        )

        datetime.strptime(
            end_time,
            "%H:%M"
        )

        subgroup = int(subgroup)

        if subgroup not in (0, 1, 2):
            return None

    except Exception:
        return None

    normalized_date = datetime.strptime(
        lesson_date,
        "%d.%m.%Y"
    ).strftime("%Y-%m-%d")

    return (
        normalized_date,
        start_time,
        end_time,
        subject,
        teacher,
        room,
        lesson_type,
        subgroup
    )


async def add_lesson_handler(message: Message):
    save_user(message)

    if not is_starosta(message.from_user.id):
        await message.answer(
            "⛔ Добавлять пары может только староста."
        )
        return

    await message.answer(
        "➕ <b>Добавление пары</b>\n\n"
        "Отправь одной строкой в формате:\n\n"
        "<code>"
        "28.09.2026 | 08:00 | 09:30 | Математика | "
        "Иванов И.И. | ауд. 101 | Лекция | 0"
        "</code>\n\n"
        "Последняя цифра:\n"
        "0 — общая пара\n"
        "1 — 1-я подгруппа\n"
        "2 — 2-я подгруппа"
    )


async def add_lesson_text_handler(message: Message):
    if not is_starosta(message.from_user.id):
        return

    # Не перехватываем обычные сообщения.
    text = message.text or ""

    if "|" not in text:
        return

    lesson = parse_lesson_text(text)

    if not lesson:
        return

    (
        lesson_date,
        start_time,
        end_time,
        subject,
        teacher,
        room,
        lesson_type,
        subgroup
    ) = lesson

    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO lessons (
            lesson_date,
            start_time,
            end_time,
            subject,
            teacher,
            room,
            lesson_type,
            subgroup,
            created_by,
            is_custom
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
    """, (
        lesson_date,
        start_time,
        end_time,
        subject,
        teacher,
        room,
        lesson_type,
        subgroup,
        message.from_user.id
    ))

    db.commit()

    await message.answer(
        "✅ <b>Пара добавлена.</b>\n\n"
        f"📅 {datetime.strptime(lesson_date, '%Y-%m-%d').strftime('%d.%m.%Y')}\n"
        f"⏰ {start_time}–{end_time}\n"
        f"📚 {subject}\n"
        f"👨‍🏫 {teacher}\n"
        f"📍 {room}\n"
        f"📝 {lesson_type}\n"
        f"👤 Подгруппа: {subgroup}"
    )


# ============================================================
# УДАЛЕНИЕ ПАР
# ============================================================

async def delete_lesson_handler(message: Message):
    save_user(message)

    if not is_starosta(message.from_user.id):
        await message.answer(
            "⛔ Удалять пары может только староста."
        )
        return

    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM lessons
        WHERE lesson_date >= ?
        ORDER BY lesson_date, start_time
    """, (today_str(),))

    lessons = cursor.fetchall()

    if not lessons:
        await message.answer(
            "🗑 Удалять пока нечего."
        )
        return

    buttons = []

    for lesson in lessons[:50]:
        date_text = datetime.strptime(
            lesson["lesson_date"],
            "%Y-%m-%d"
        ).strftime("%d.%m")

        buttons.append([
            InlineKeyboardButton(
                text=(
                    f"🗑 {date_text} "
                    f"{lesson['start_time']} "
                    f"{lesson['subject'][:25]}"
                ),
                callback_data=f"delete_{lesson['id']}"
            )
        ])

    await message.answer(
        "🗑 <b>Выбери пару для удаления:</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )


async def delete_lesson_callback(callback: CallbackQuery):
    if not is_starosta(callback.from_user.id):
        await callback.answer(
            "Только староста может удалять пары.",
            show_alert=True
        )
        return

    lesson_id = int(
        callback.data.split("_")[1]
    )

    cursor = db.cursor()

    cursor.execute(
        "SELECT * FROM lessons WHERE id = ?",
        (lesson_id,)
    )

    lesson = cursor.fetchone()

    if not lesson:
        await callback.answer(
            "Пара уже удалена.",
            show_alert=True
        )
        return

    cursor.execute(
        "DELETE FROM lessons WHERE id = ?",
        (lesson_id,)
    )

    db.commit()

    await callback.message.edit_text(
        "🗑 <b>Пара удалена.</b>\n\n"
        f"📚 {lesson['subject']}\n"
        f"📅 {datetime.strptime(lesson['lesson_date'], '%Y-%m-%d').strftime('%d.%m.%Y')}\n"
        f"⏰ {lesson['start_time']}–{lesson['end_time']}"
    )

    await callback.answer("Удалено")


# ============================================================
# ПОМОЩЬ
# ============================================================

async def help_handler(message: Message):
    save_user(message)

    text = (
        "ℹ️ <b>Помощь</b>\n\n"

        "📅 <b>Расписание</b>\n"
        "Показывает расписание по выбранной подгруппе.\n\n"

        "📌 <b>Сегодня</b>\n"
        "Показывает пары на сегодня и позволяет "
        "самостоятельно отметиться.\n\n"

        "👤 <b>Моя группа</b>\n"
        "Выбор 1-й или 2-й подгруппы.\n\n"

        "📋 <b>Перекличка</b>\n"
        "Староста видит, кто отметился на сегодняшних парах.\n\n"

        "📊 <b>Статистика</b>\n"
        "Сводная статистика посещаемости.\n\n"

        "🗄 <b>База</b>\n"
        "Количество студентов, пар и отметок.\n\n"

        "👑 <b>Староста</b>\n"
        "Назначение старосты.\n\n"

        "➕ <b>Добавить пару</b>\n"
        "Добавление новой пары вручную.\n\n"

        "🗑 <b>Удалить пару</b>\n"
        "Удаление пары из расписания."
    )

    await message.answer(text)


# ============================================================
# РАСПОЗНАВАНИЕ КНОПОК
# ============================================================

async def menu_router(message: Message):

    text = message.text

    if text == "📅 Расписание":
        await schedule_handler(message)

    elif text == "📌 Сегодня":
        await today_handler(message)

    elif text == "👤 Моя группа":
        await my_group_handler(message)

    elif text == "📋 Перекличка":
        await attendance_list_handler(message)

    elif text == "🗄 База":
        await database_handler(message)

    elif text == "📊 Статистика":
        await statistics_handler(message)

    elif text == "👑 Староста":
        await starosta_handler(message)

    elif text == "➕ Добавить пару":
        await add_lesson_handler(message)

    elif text == "🗑 Удалить пару":
        await delete_lesson_handler(message)

    elif text == "ℹ️ Помощь":
        await help_handler(message)


# ============================================================
# КОМАНДЫ ДЛЯ УДОБСТВА
# ============================================================

async def command_help(message: Message):
    await help_handler(message)


# ============================================================
# ЗАПУСК БОТА
# ============================================================

async def main():

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        )
    )

    dp = Dispatcher()

    # Команды
    dp.message.register(
        start_handler,
        CommandStart()
    )

    dp.message.register(
        command_help,
        Command("help")
    )

    # Callback-кнопки
    dp.callback_query.register(
        subgroup_callback,
        F.data.startswith("subgroup_")
    )

    dp.callback_query.register(
        attendance_callback,
        F.data.startswith("attend_")
    )

    dp.callback_query.register(
        become_starosta_callback,
        F.data == "become_starosta"
    )

    dp.callback_query.register(
        remove_starosta_callback,
        F.data == "remove_starosta"
    )

    dp.callback_query.register(
        delete_lesson_callback,
        F.data.startswith("delete_")
    )

    # Текстовые кнопки меню
    dp.message.register(
        menu_router,
        F.text
    )

    # Добавление пары через строку с |
    dp.message.register(
        add_lesson_text_handler,
        F.text
    )

    print("Бот запущен...")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
