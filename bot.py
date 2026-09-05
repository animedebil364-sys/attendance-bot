import os
import csv
import io
import asyncio
import sqlite3
import re

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

TZ = ZoneInfo(
    os.getenv(
        "TZ",
        "Europe/Moscow"
    )
)

DB_PATH = os.getenv(
    "DB_PATH",
    "attendance.db"
)

# Сайт расписания
SCHEDULE_URL = "https://tt2.vogu35.ru/"

# Твоя группа
GROUP_ID = "543"

INSTITUTE = "ИСИ"
COURSE = "1"
GROUP_NAME = "1Б08 №12"


# ============================================================
# BOT
# ============================================================

bot = Bot(TOKEN)

dp = Dispatcher(
    storage=MemoryStorage()
)


# ============================================================
# СОСТОЯНИЯ
# ============================================================

class States(StatesGroup):

    # Выбор предмета для переклички
    selecting_lesson = State()

    # Добавление своей пары
    schedule_subject = State()
    schedule_day = State()
    schedule_time = State()


# ============================================================
# ВРЕМЯ
# ============================================================

def now():
    return datetime.now(TZ)


# ============================================================
# БАЗА
# ============================================================

def db():

    connection = sqlite3.connect(
        DB_PATH
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_db():

    with db() as c:

        c.executescript("""
        
        CREATE TABLE IF NOT EXISTS chats(
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            starosta_id INTEGER,
            starosta_name TEXT
        );

        CREATE TABLE IF NOT EXISTS students(
            chat_id INTEGER,
            user_id INTEGER,
            name TEXT,
            username TEXT,
            first_seen TEXT,
            PRIMARY KEY(chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS rollcalls(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            subject TEXT,
            lesson_date TEXT,
            lesson_time TEXT,
            teacher TEXT,
            classroom TEXT,
            started_at TEXT,
            finished_at TEXT,
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS attendance(
            rollcall_id INTEGER,
            user_id INTEGER,
            name TEXT,
            status TEXT,
            answered_at TEXT,
            PRIMARY KEY(
                rollcall_id,
                user_id
            )
        );

        CREATE TABLE IF NOT EXISTS schedule(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            subject TEXT,
            day INTEGER,
            lesson_time TEXT,
            reminder INTEGER DEFAULT 30,
            enabled INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS remote_schedule(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            lesson_date TEXT,
            day_name TEXT,
            lesson_time TEXT,
            subject TEXT,
            teacher TEXT,
            classroom TEXT,
            lesson_type TEXT,
            subgroup TEXT,
            source TEXT,
            updated_at TEXT
        );

        """)

        # ----------------------------------------------------
        # Мягкая миграция старой базы
        # ----------------------------------------------------

        columns = [
            row["name"]
            for row in c.execute(
                "PRAGMA table_info(rollcalls)"
            ).fetchall()
        ]

        if "lesson_date" not in columns:

            c.execute("""
                ALTER TABLE rollcalls
                ADD COLUMN lesson_date TEXT
            """)

        if "lesson_time" not in columns:

            c.execute("""
                ALTER TABLE rollcalls
                ADD COLUMN lesson_time TEXT
            """)

        if "teacher" not in columns:

            c.execute("""
                ALTER TABLE rollcalls
                ADD COLUMN teacher TEXT
            """)

        if "classroom" not in columns:

            c.execute("""
                ALTER TABLE rollcalls
                ADD COLUMN classroom TEXT
            """)


# ============================================================
# РЕГИСТРАЦИЯ
# ============================================================

def ensure(message: Message):

    if not message.chat:
        return

    with db() as c:

        c.execute("""
            INSERT INTO chats(
                chat_id,
                title
            )
            VALUES(?, ?)

            ON CONFLICT(chat_id)
            DO UPDATE SET
                title=excluded.title
        """, (
            message.chat.id,
            message.chat.title
            or "Личный чат"
        ))

        if message.from_user:

            c.execute("""
                INSERT INTO students(
                    chat_id,
                    user_id,
                    name,
                    username,
                    first_seen
                )
                VALUES(?, ?, ?, ?, ?)

                ON CONFLICT(
                    chat_id,
                    user_id
                )

                DO UPDATE SET
                    name=excluded.name,
                    username=excluded.username
            """, (
                message.chat.id,
                message.from_user.id,
                message.from_user.full_name,
                message.from_user.username,
                now().isoformat()
            ))


# ============================================================
# СТАРОСТА
# ============================================================

def is_admin(message: Message):

    if not message.from_user:
        return False

    with db() as c:

        row = c.execute("""
            SELECT starosta_id
            FROM chats
            WHERE chat_id=?
        """, (
            message.chat.id,
        )).fetchone()

    if not row:
        return False

    return (
        row["starosta_id"]
        == message.from_user.id
    )


async def admin_only(message: Message):

    if not is_admin(message):

        await message.answer(
            "❌ Эта команда доступна "
            "только старосте."
        )

        return False

    return True


# ============================================================
# АКТИВНАЯ ПЕРЕКЛИЧКА
# ============================================================

def active(chat_id):

    with db() as c:

        return c.execute("""
            SELECT *
            FROM rollcalls

            WHERE chat_id=?
              AND status='active'

            ORDER BY id DESC

            LIMIT 1
        """, (
            chat_id,
        )).fetchone()


# ============================================================
# КНОПКИ ПЕРЕКЛИЧКИ
# ============================================================

def attendance_buttons():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🟢 Буду",
                    callback_data="att:will"
                ),
                InlineKeyboardButton(
                    text="🔴 Не буду",
                    callback_data="att:wont"
                )
            ]
        ]
    )


# ============================================================
# КНОПКИ ПАР НА СЕГОДНЯ
# ============================================================

def lesson_buttons(lessons):

    buttons = []

    for index, lesson in enumerate(
        lessons
    ):

        text = (
            f"🕐 {lesson['time']} — "
            f"{lesson['subject']}"
        )

        if lesson.get("classroom"):

            text += (
                f" ({lesson['classroom']})"
            )

        buttons.append([
            InlineKeyboardButton(
                text=text[:60],
                callback_data=(
                    f"lesson:{index}"
                )
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="🔄 Обновить расписание",
            callback_data="schedule:refresh"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


# ============================================================
# ДНИ НЕДЕЛИ
# ============================================================

def day_name(day):

    days = {
        1: "Понедельник",
        2: "Вторник",
        3: "Среда",
        4: "Четверг",
        5: "Пятница",
        6: "Суббота",
        7: "Воскресенье"
    }

    return days.get(
        day,
        "Неизвестно"
    )


# ============================================================
# START
# ============================================================

@dp.message(Command("start"))
async def start(
    message: Message,
    state: FSMContext
):

    await state.clear()

    ensure(message)

    await message.answer(
        "👋 Бот группы запущен!\n\n"

        f"🏫 {INSTITUTE}\n"
        f"🎓 {COURSE} курс\n"
        f"👥 {GROUP_NAME}\n\n"

        "📋 ОСНОВНЫЕ КОМАНДЫ\n\n"

        "👑 староста — назначить себя\n"
        "📋 перекличка — начать перекличку\n"
        "📊 результаты — текущие результаты\n"
        "🛑 завершить — закончить перекличку\n"
        "📚 история — история перекличек\n"
        "📈 статистика — посещаемость\n"
        "📥 выгрузка — скачать CSV\n\n"

        "📅 РАСПИСАНИЕ\n\n"

        "🗓 расписание — всё расписание\n"
        "📌 сегодня — пары сегодня\n"
        "🔄 обновить расписание — загрузить с сайта\n\n"

        "📅 СВОИ ПАРЫ\n\n"

        "добавить пару — добавить пару\n"
        "удалить пару ID — удалить пару\n\n"

        "❌ отмена — отменить действие\n"
        "ℹ️ помощь — помощь"
    )


# ============================================================
# ПОМОЩЬ
# ============================================================

async def help_command(
    message: Message
):

    await message.answer(
        "📚 КОМАНДЫ БОТА\n\n"

        "👑 староста\n"
        "Назначить себя старостой.\n\n"

        "📋 перекличка\n"
        "Показать пары на сегодня и "
        "выбрать нужную пару.\n\n"

        "📊 результаты\n"
        "Показать текущие ответы.\n\n"

        "🛑 завершить\n"
        "Закончить текущую перекличку.\n\n"

        "📚 история\n"
        "Показать историю перекличек.\n\n"

        "📈 статистика\n"
        "Показать статистику студентов.\n\n"

        "📥 выгрузка\n"
        "Скачать историю в CSV.\n\n"

        "🗓 расписание\n"
        "Показать загруженное расписание.\n\n"

        "📌 сегодня\n"
        "Показать пары на сегодня.\n\n"

        "🔄 обновить расписание\n"
        "Заново загрузить расписание с сайта.\n\n"

        "📅 добавить пару\n"
        "Добавить ручную пару.\n\n"

        "🗑 удалить пару ID\n"
        "Удалить ручную пару.\n\n"

        "❌ отмена\n"
        "Отменить текущее действие."
    )


@dp.message(
    F.text.lower() == "помощь"
)
async def help_text(
    message: Message
):

    ensure(message)

    await help_command(message)


# ============================================================
# ОТМЕНА
# ============================================================

@dp.message(
    F.text.lower() == "отмена"
)
async def cancel_text(
    message: Message,
    state: FSMContext
):

    await state.clear()

    await message.answer(
        "❌ Текущее действие отменено."
    )


# ============================================================
# СТАРОСТА
# ============================================================

@dp.message(
    F.text.lower() == "староста"
)
async def set_starosta(
    message: Message
):

    ensure(message)

    with db() as c:

        row = c.execute("""
            SELECT starosta_id
            FROM chats
            WHERE chat_id=?
        """, (
            message.chat.id,
        )).fetchone()

        if (
            row
            and row["starosta_id"]
            and row["starosta_id"]
            != message.from_user.id
        ):

            await message.answer(
                "❌ Староста уже назначен."
            )

            return

        c.execute("""
            UPDATE chats

            SET
                starosta_id=?,
                starosta_name=?

            WHERE chat_id=?
        """, (
            message.from_user.id,
            message.from_user.full_name,
            message.chat.id
        ))

    await message.answer(
        "👑 Ты назначен старостой этой группы!"
    )


# ============================================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ:
# ДАТА ТЕКУЩЕЙ НЕДЕЛИ
# ============================================================

def schedule_dates():

    today = now().date()

    monday = (
        today -
        timedelta(
            days=today.weekday()
        )
    )

    dates = []

    for i in range(14):

        date = monday + timedelta(
            days=i
        )

        dates.append(
            date.strftime("%Y-%m-%d")
        )

    return dates


# ============================================================
# ПОЛУЧЕНИЕ РАСПИСАНИЯ С САЙТА
# ============================================================

async def fetch_schedule_html():

    dates = schedule_dates()

    date_start = dates[0]
    date_end = dates[-1]

    payload = {
        "group_id": GROUP_ID,
        "date_start": date_start,
        "date_end": date_end,
        "selected_lesson_type": "typical"
    }

    headers = {
        "User-Agent":
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0.0.0 "
            "Safari/537.36",

        "Referer": SCHEDULE_URL,

        "Accept":
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8",

        "Accept-Language":
            "ru-RU,ru;q=0.9"
    }

    timeout = aiohttp.ClientTimeout(
        total=90,
        connect=30,
        sock_read=60
    )

    print(
        "Запрашиваю расписание:"
    )

    print(
        f"Группа: {GROUP_ID}"
    )

    print(
        f"Дата начала: {date_start}"
    )

    print(
        f"Дата конца: {date_end}"
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers
    ) as session:

        # Сначала открываем сайт.
        # Это помогает получить cookies,
        # если сервер их использует.

        try:

            async with session.get(
                SCHEDULE_URL
            ) as response:

                print(
                    "GET:",
                    response.status
                )

                await response.text()

        except Exception as error:

            print(
                "Ошибка начального GET:",
                error
            )

        # Основной запрос

        async with session.post(
            SCHEDULE_URL,
            data=payload
        ) as response:

            print(
                "POST:",
                response.status
            )

            response.raise_for_status()

            html = await response.text()

            print(
                "Размер ответа:",
                len(html)
            )

            return html


# ============================================================
# ОЧИСТКА ТЕКСТА
# ============================================================

def clean_text(text):

    if not text:
        return ""

    text = text.replace(
        "\xa0",
        " "
    )

    return re.sub(
        r"\s+",
        " ",
        text
    ).strip()


# ============================================================
# РАСПОЗНАВАНИЕ ДАТЫ
# ============================================================

def normalize_date(value):

    value = clean_text(value)

    patterns = [
        "%d.%m.%Y",
        "%d.%m.%y",
        "%Y-%m-%d",
        "%Y-%m-%d"
    ]

    for pattern in patterns:

        try:

            dt = datetime.strptime(
                value,
                pattern
            )

            return dt.strftime(
                "%Y-%m-%d"
            )

        except ValueError:
            pass

    return None


# ============================================================
# РАСПОЗНАВАНИЕ ВРЕМЕНИ
# ============================================================

TIME_PATTERN = re.compile(
    r"\b"
    r"(\d{1,2}:\d{2})"
    r"\s*[-–—]\s*"
    r"(\d{1,2}:\d{2})"
    r"\b"
)


def extract_time(text):

    match = TIME_PATTERN.search(
        text
    )

    if not match:
        return None

    return (
        f"{match.group(1)}-"
        f"{match.group(2)}"
    )


# ============================================================
# ПОПЫТКА ОПРЕДЕЛИТЬ ТИП ЗАНЯТИЯ
# ============================================================

def detect_lesson_type(text):

    text_lower = text.lower()

    if "лаборатор" in text_lower:
        return "Лабораторная"

    if "практи" in text_lower:
        return "Практика"

    if "лекц" in text_lower:
        return "Лекция"

    if "семинар" in text_lower:
        return "Семинар"

    return ""


# ============================================================
# АУДИТОРИЯ
# ============================================================

def detect_classroom(text):

    patterns = [

        r"ауд\.?\s*[\wА-Яа-яЁё./-]+",

        r"аудитория\s*[\wА-Яа-яЁё./-]+",

        r"каб\.?\s*[\wА-Яа-яЁё./-]+",

        r"к\.\s*[\wА-Яа-яЁё./-]+"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            return clean_text(
                match.group(0)
            )

    return ""


# ============================================================
# ПРЕПОДАВАТЕЛЬ
# ============================================================

def detect_teacher(parts):

    keywords = [
        "преподаватель",
        "преп.",
        "доц.",
        "проф.",
        "ассистент",
        "старший преподаватель"
    ]

    for part in parts:

        lower = part.lower()

        for keyword in keywords:

            if keyword in lower:

                return part

    return ""


# ============================================================
# ДАТЫ ИЗ HTML
# ============================================================

def find_all_dates(soup):

    result = []

    date_patterns = [

        re.compile(
            r"\b\d{2}\.\d{2}\.\d{4}\b"
        ),

        re.compile(
            r"\b\d{2}\.\d{2}\.\d{2}\b"
        ),

        re.compile(
            r"\b\d{4}-\d{2}-\d{2}\b"
        )
    ]

    for text in soup.stripped_strings:

        value = clean_text(text)

        for pattern in date_patterns:

            for match in pattern.findall(
                value
            ):

                normalized = normalize_date(
                    match
                )

                if (
                    normalized
                    and normalized not in result
                ):

                    result.append(
                        normalized
                    )

    return result


# ============================================================
# ПАРСИНГ РАСПИСАНИЯ
# ============================================================

def parse_schedule(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    dates = find_all_dates(
        soup
    )

    lessons = []

    # --------------------------------------------------------
    # Ищем элементы со временем
    # --------------------------------------------------------

    time_elements = soup.find_all(
        string=TIME_PATTERN
    )

    for element in time_elements:

        raw = clean_text(
            str(element)
        )

        lesson_time = extract_time(
            raw
        )

        if not lesson_time:
            continue

        parent = element.parent

        # ----------------------------------------------------
        # Поднимаемся вверх по HTML
        # ----------------------------------------------------

        container = parent

        for _ in range(7):

            if not container:
                break

            text = clean_text(
                container.get_text(
                    " ",
                    strip=True
                )
            )

            if len(text) >= 20:

                break

            container = container.parent

        if not container:
            continue

        full_text = clean_text(
            container.get_text(
                " ",
                strip=True
            )
        )

        if not full_text:
            continue

        # ----------------------------------------------------
        # Части текста
        # ----------------------------------------------------

        parts = []

        for value in container.stripped_strings:

            value = clean_text(
                value
            )

            if value:
                parts.append(value)

        # ----------------------------------------------------
        # Предмет
        # ----------------------------------------------------

        subject = ""

        # Сначала ищем заголовки
        for tag in container.find_all([
            "b",
            "strong",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6"
        ]):

            value = clean_text(
                tag.get_text(
                    " ",
                    strip=True
                )
            )

            if not value:
                continue

            if TIME_PATTERN.search(
                value
            ):
                continue

            if len(value) < 3:
                continue

            subject = value

            break

        # Если заголовка нет —
        # ищем подходящую строку.

        if not subject:

            for value in parts:

                if TIME_PATTERN.search(
                    value
                ):
                    continue

                if re.match(
                    r"^\d+$",
                    value
                ):
                    continue

                lower = value.lower()

                if lower in {
                    "лекция",
                    "практика",
                    "лабораторная",
                    "семинар",
                    "занятие"
                }:
                    continue

                if len(value) < 3:
                    continue

                subject = value

                break

        if not subject:
            continue

        # ----------------------------------------------------
        # Преподаватель
        # ----------------------------------------------------

        teacher = detect_teacher(
            parts
        )

        # ----------------------------------------------------
        # Аудитория
        # ----------------------------------------------------

        classroom = detect_classroom(
            full_text
        )

        # ----------------------------------------------------
        # Тип
        # ----------------------------------------------------

        lesson_type = detect_lesson_type(
            full_text
        )

        # ----------------------------------------------------
        # Подгруппа
        # ----------------------------------------------------

        subgroup = ""

        subgroup_match = re.search(
            r"([12])\s*"
            r"(?:подгруппа|п/г)",
            full_text,
            re.IGNORECASE
        )

        if subgroup_match:

            subgroup = (
                subgroup_match.group(1)
            )

        lesson = {

            "date": "",

            "time": lesson_time,

            "subject": subject,

            "teacher": teacher,

            "classroom": classroom,

            "lesson_type": lesson_type,

            "subgroup": subgroup
        }

        lessons.append(
            lesson
        )

    # --------------------------------------------------------
    # Удаляем дубликаты
    # --------------------------------------------------------

    unique = []

    seen = set()

    for lesson in lessons:

        key = (
            lesson["time"],
            lesson["subject"],
            lesson["teacher"],
            lesson["classroom"],
            lesson["subgroup"]
        )

        if key in seen:
            continue

        seen.add(key)

        unique.append(
            lesson
        )

    lessons = unique

    # --------------------------------------------------------
    # Если даты удалось найти,
    # пытаемся распределить пары по датам.
    # --------------------------------------------------------

    if dates:

        for index, lesson in enumerate(
            lessons
        ):

            lesson["date"] = dates[
                index % len(dates)
            ]

    return lessons


# ============================================================
# СОХРАНЕНИЕ РАСПИСАНИЯ С САЙТА
# ============================================================

def save_remote_schedule(
    chat_id,
    lessons
):

    saved = 0

    with db() as c:

        # Старое расписание этого чата
        # не удаляем полностью.
        # Обновляем/добавляем новые записи.

        for lesson in lessons:

            lesson_date = lesson.get(
                "date",
                ""
            )

            if not lesson_date:
                continue

            existing = c.execute("""
                SELECT id
                FROM remote_schedule

                WHERE chat_id=?
                  AND lesson_date=?
                  AND lesson_time=?
                  AND subject=?
            """, (
                chat_id,
                lesson_date,
                lesson["time"],
                lesson["subject"]
            )).fetchone()

            if existing:

                c.execute("""
                    UPDATE remote_schedule

                    SET
                        teacher=?,
                        classroom=?,
                        lesson_type=?,
                        subgroup=?,
                        updated_at=?

                    WHERE id=?
                """, (
                    lesson["teacher"],
                    lesson["classroom"],
                    lesson["lesson_type"],
                    lesson["subgroup"],
                    now().isoformat(),
                    existing["id"]
                ))

            else:

                c.execute("""
                    INSERT INTO remote_schedule(
                        chat_id,
                        lesson_date,
                        day_name,
                        lesson_time,
                        subject,
                        teacher,
                        classroom,
                        lesson_type,
                        subgroup,
                        source,
                        updated_at
                    )

                    VALUES(
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                """, (

                    chat_id,

                    lesson_date,

                    russian_day_name(
                        datetime.strptime(
                            lesson_date,
                            "%Y-%m-%d"
                        ).isoweekday()
                    ),

                    lesson["time"],

                    lesson["subject"],

                    lesson["teacher"],

                    lesson["classroom"],

                    lesson["lesson_type"],

                    lesson["subgroup"],

                    SCHEDULE_URL,

                    now().isoformat()
                ))

            saved += 1

    return saved


# ============================================================
# ОБНОВЛЕНИЕ
# ============================================================

async def update_schedule(
    chat_id
):

    html = await fetch_schedule_html()

    lessons = parse_schedule(
        html
    )

    if not lessons:

        return 0

    saved = save_remote_schedule(
        chat_id,
        lessons
    )

    return saved


# ============================================================
# ПОЛУЧИТЬ ПАРЫ НА СЕГОДНЯ
# ============================================================

def get_today_lessons(
    chat_id
):

    today_date = now().strftime(
        "%Y-%m-%d"
    )

    with db() as c:

        rows = c.execute("""
            SELECT *
            FROM remote_schedule

            WHERE chat_id=?
              AND lesson_date=?

            ORDER BY lesson_time
        """, (
            chat_id,
            today_date
        )).fetchall()

    return rows


# ============================================================
# ПОКАЗ РАСПИСАНИЯ
# ============================================================

def format_lesson(
    lesson
):

    text = (
        f"🕐 {lesson['lesson_time']}\n"
        f"📚 {lesson['subject']}\n"
    )

    if lesson["teacher"]:

        text += (
            f"👨‍🏫 {lesson['teacher']}\n"
        )

    if lesson["classroom"]:

        text += (
            f"🏫 {lesson['classroom']}\n"
        )

    if lesson["lesson_type"]:

        text += (
            f"📖 {lesson['lesson_type']}\n"
        )

    if lesson["subgroup"]:

        text += (
            f"👥 {lesson['subgroup']} "
            "подгруппа\n"
        )

    return text


# ============================================================
# КОМАНДА РАСПИСАНИЕ
# ============================================================

@dp.message(
    F.text.lower() == "расписание"
)
async def schedule(
    message: Message
):

    ensure(message)

    with db() as c:

        rows = c.execute("""
            SELECT *
            FROM remote_schedule

            WHERE chat_id=?

            ORDER BY
                lesson_date,
                lesson_time
        """, (
            message.chat.id,
        )).fetchall()

    if not rows:

        await message.answer(
            "🔄 Расписание ещё не загружено.\n\n"
            "Пробую получить его с сайта..."
        )

        try:

            saved = await update_schedule(
                message.chat.id
            )

            if saved == 0:

                await message.answer(
                    "⚠️ Не удалось распознать "
                    "расписание на сайте."
                )

                return

        except asyncio.TimeoutError:

            await message.answer(
                "❌ Сайт не ответил вовремя."
            )

            return

        except Exception as error:

            await message.answer(
                "❌ Ошибка при загрузке "
                "расписания:\n\n"
                f"{type(error).__name__}: "
                f"{error}"
            )

            return

        with db() as c:

            rows = c.execute("""
                SELECT *
                FROM remote_schedule

                WHERE chat_id=?

                ORDER BY
                    lesson_date,
                    lesson_time
            """, (
                message.chat.id,
            )).fetchall()

    text = (
        "🗓 РАСПИСАНИЕ\n\n"
        f"🏫 {INSTITUTE}\n"
        f"🎓 {COURSE} курс\n"
        f"👥 {GROUP_NAME}\n"
    )

    current_date = None

    for row in rows:

        if row["lesson_date"] != current_date:

            current_date = row[
                "lesson_date"
            ]

            try:

                dt = datetime.strptime(
                    current_date,
                    "%Y-%m-%d"
                )

                text += (
                    "\n📅 "
                    f"{day_name(dt.isoweekday())} "
                    f"{dt.strftime('%d.%m.%Y')}\n"
                )

            except Exception:

                text += (
                    f"\n📅 {current_date}\n"
                )

        text += "\n"

        text += format_lesson(
            row
        )

    await message.answer(
        text[:4000]
    )


# ============================================================
# КОМАНДА СЕГОДНЯ
# ============================================================

@dp.message(
    F.text.lower() == "сегодня"
)
async def today(
    message: Message
):

    ensure(message)

    rows = get_today_lessons(
        message.chat.id
    )

    # Если расписания ещё нет —
    # автоматически загружаем.

    if not rows:

        try:

            await update_schedule(
                message.chat.id
            )

        except Exception:
            pass

        rows = get_today_lessons(
            message.chat.id
        )

    today_date = now().strftime(
        "%d.%m.%Y"
    )

    day = now().isoweekday()

    if not rows:

        await message.answer(
            "📌 СЕГОДНЯ\n\n"
            f"{day_name(day)}, "
            f"{today_date}\n\n"
            "📭 Пар сегодня не найдено."
        )

        return

    text = (
        "📌 ПАРЫ СЕГОДНЯ\n\n"
        f"📅 {day_name(day)}, "
        f"{today_date}\n"
    )

    for row in rows:

        text += "\n"

        text += format_lesson(
            row
        )

    await message.answer(
        text
    )


# ============================================================
# ОБНОВИТЬ РАСПИСАНИЕ
# ============================================================

@dp.message(
    F.text.lower() ==
    "обновить расписание"
)
async def refresh_schedule(
    message: Message
):

    ensure(message)

    await message.answer(
        "🔄 Обновляю расписание...\n\n"
        f"🏫 {INSTITUTE}\n"
        f"🎓 {COURSE} курс\n"
        f"👥 {GROUP_NAME}\n"
        f"🆔 ID группы: {GROUP_ID}"
    )

    try:

        saved = await update_schedule(
            message.chat.id
        )

        if saved == 0:

            await message.answer(
                "⚠️ Сайт ответил, но "
                "расписание не удалось "
                "распознать."
            )

            return

        await message.answer(
            "✅ Расписание обновлено!\n\n"
            f"📚 Обработано занятий: {saved}\n\n"
            "Теперь можно написать:\n"
            "📌 сегодня\n"
            "или\n"
            "🗓 расписание"
        )

    except asyncio.TimeoutError:

        await message.answer(
            "❌ Сайт не ответил вовремя.\n\n"
            "Сервер расписания слишком долго "
            "отвечает на запрос."
        )

    except aiohttp.ClientError as error:

        await message.answer(
            "❌ Ошибка соединения с сайтом:\n\n"
            f"{error}"
        )

    except Exception as error:

        await message.answer(
            "❌ Не удалось получить расписание:\n\n"
            f"{type(error).__name__}: "
            f"{error}"
        )


# ============================================================
# НАЧАЛО ПЕРЕКЛИЧКИ
# ============================================================

@dp.message(
    F.text.lower() == "перекличка"
)
async def rollcall(
    message: Message,
    state: FSMContext
):

    ensure(message)

    if not await admin_only(message):

        return

    if active(
        message.chat.id
    ):

        await message.answer(
            "⚠️ Перекличка уже идёт.\n\n"
            "Сначала напиши:\n"
            "завершить"
        )

        return

    # --------------------------------------------------------
    # Берём расписание
    # --------------------------------------------------------

    lessons = get_today_lessons(
        message.chat.id
    )

    if not lessons:

        await message.answer(
            "🔄 Расписание на сегодня "
            "не найдено.\n\n"
            "Пробую загрузить его "
            "с сайта..."
        )

        try:

            await update_schedule(
                message.chat.id
            )

        except asyncio.TimeoutError:

            await message.answer(
                "❌ Сайт не ответил вовремя."
            )

            return

        except Exception as error:

            await message.answer(
                "❌ Не удалось получить "
                "расписание:\n\n"
                f"{type(error).__name__}: "
                f"{error}"
            )

            return

        lessons = get_today_lessons(
            message.chat.id
        )

    if not lessons:

        await message.answer(
            "📭 На сегодня бот не нашёл "
            "пар в расписании.\n\n"
            "Проверь командой:\n"
            "📌 сегодня"
        )

        return

    # --------------------------------------------------------
    # Сохраняем пары во временное состояние
    # --------------------------------------------------------

    lesson_data = []

    for lesson in lessons:

        lesson_data.append({
            "date":
                lesson["lesson_date"],

            "time":
                lesson["lesson_time"],

            "subject":
                lesson["subject"],

            "teacher":
                lesson["teacher"],

            "classroom":
                lesson["classroom"],

            "lesson_type":
                lesson["lesson_type"],

            "subgroup":
                lesson["subgroup"]
        })

    await state.update_data(
        lessons=lesson_data
    )

    await state.set_state(
        States.selecting_lesson
    )

    await message.answer(
        "📋 НАЧАЛО ПЕРЕКЛИЧКИ\n\n"
        "Выбери пару из расписания "
        "на сегодня:",
        reply_markup=lesson_buttons(
            lesson_data
        )
    )


# ============================================================
# ВЫБОР ПАРЫ
# ============================================================

@dp.callback_query(
    F.data.startswith("lesson:")
)
async def select_lesson(
    call: CallbackQuery,
    state: FSMContext
):

    # --------------------------------------------------------
    # ВАЖНО:
    # здесь используем call.from_user,
    # а НЕ call.message.from_user.
    #
    # Это исправляет ошибку:
    #
    # AttributeError:
    # 'InaccessibleMessage'
    # object has no attribute 'from_user'
    # --------------------------------------------------------

    user = call.from_user

    # Callback может прийти из недоступного сообщения.
    # Поэтому нам достаточно chat_id из message,
    # если он доступен.

    if call.message is None:

        await call.answer(
            "❌ Сообщение недоступно.",
            show_alert=True
        )

        return

    chat = getattr(
        call.message,
        "chat",
        None
    )

    if chat is None:

        await call.answer(
            "❌ Не удалось определить чат.",
            show_alert=True
        )

        return

    chat_id = chat.id

    # --------------------------------------------------------
    # Проверяем старосту
    # --------------------------------------------------------

    with db() as c:

        row = c.execute("""
            SELECT starosta_id
            FROM chats
            WHERE chat_id=?
        """, (
            chat_id,
        )).fetchone()

    if (
        not row
        or row["starosta_id"]
        != user.id
    ):

        await call.answer(
            "❌ Только староста может "
            "выбрать пару.",
            show_alert=True
        )

        return

    # --------------------------------------------------------
    # Индекс
    # --------------------------------------------------------

    try:

        index = int(
            call.data.split(":")[1]
        )

    except Exception:

        await call.answer(
            "❌ Ошибка выбора пары.",
            show_alert=True
        )

        return

    data = await state.get_data()

    lessons = data.get(
        "lessons",
        []
    )

    if (
        index < 0
        or index >= len(lessons)
    ):

        await call.answer(
            "❌ Пара больше недоступна.",
            show_alert=True
        )

        return

    lesson = lessons[index]

    # --------------------------------------------------------
    # Создаём перекличку
    # --------------------------------------------------------

    with db() as c:

        # На всякий случай закрываем
        # предыдущую активную.

        c.execute("""
            UPDATE rollcalls

            SET
                status='finished',
                finished_at=?

            WHERE
                chat_id=?
                AND status='active'
        """, (
            now().strftime(
                "%d.%m.%Y %H:%M"
            ),
            chat_id
        ))

        c.execute("""
            INSERT INTO rollcalls(
                chat_id,
                subject,
                lesson_date,
                lesson_time,
                teacher,
                classroom,
                started_at,
                status
            )

            VALUES(
                ?, ?, ?, ?, ?, ?, ?, 'active'
            )
        """, (

            chat_id,

            lesson["subject"],

            lesson["date"],

            lesson["time"],

            lesson.get(
                "teacher",
                ""
            ),

            lesson.get(
                "classroom",
                ""
            ),

            now().strftime(
                "%d.%m.%Y %H:%M"
            )
        ))

    await state.clear()

    text = (
        "📋 ПЕРЕКЛИЧКА НАЧАТА!\n\n"
        f"📚 {lesson['subject']}\n"
        f"📅 {lesson['date']}\n"
        f"🕐 {lesson['time']}\n"
    )

    if lesson.get("teacher"):

        text += (
            f"👨‍🏫 "
            f"{lesson['teacher']}\n"
        )

    if lesson.get("classroom"):

        text += (
            f"🏫 "
            f"{lesson['classroom']}\n"
        )

    text += (
        "\nСтуденты, нажмите кнопку:"
    )

    await call.message.answer(
        text,
        reply_markup=attendance_buttons()
    )

    await call.answer(
        "Перекличка начата!"
    )


# ============================================================
# ОБНОВИТЬ РАСПИСАНИЕ ИЗ МЕНЮ
# ============================================================

@dp.callback_query(
    F.data == "schedule:refresh"
)
async def refresh_from_button(
    call: CallbackQuery,
    state: FSMContext
):

    if call.message is None:

        await call.answer(
            "❌ Сообщение недоступно.",
            show_alert=True
        )

        return

    user = call.from_user

    chat = getattr(
        call.message,
        "chat",
        None
    )

    if chat is None:

        await call.answer(
            "❌ Чат недоступен.",
            show_alert=True
        )

        return

    with db() as c:

        row = c.execute("""
            SELECT starosta_id
            FROM chats
            WHERE chat_id=?
        """, (
            chat.id,
        )).fetchone()

    if (
        not row
        or row["starosta_id"] != user.id
    ):

        await call.answer(
            "❌ Только староста может "
            "обновлять расписание.",
            show_alert=True
        )

        return

    await call.answer(
        "🔄 Обновляю..."
    )

    try:

        saved = await update_schedule(
            chat.id
        )

        lessons = get_today_lessons(
            chat.id
        )

        if not lessons:

            await call.message.answer(
                "⚠️ Расписание на сегодня "
                "не найдено."
            )

            return

        lesson_data = []

        for lesson in lessons:

            lesson_data.append({
                "date":
                    lesson["lesson_date"],

                "time":
                    lesson["lesson_time"],

                "subject":
                    lesson["subject"],

                "teacher":
                    lesson["teacher"],

                "classroom":
                    lesson["classroom"],

                "lesson_type":
                    lesson["lesson_type"],

                "subgroup":
                    lesson["subgroup"]
            })

        await state.update_data(
            lessons=lesson_data
        )

        await state.set_state(
            States.selecting_lesson
        )

        await call.message.answer(
            "📋 Пары на сегодня:\n\n"
            "Выбери нужную:",
            reply_markup=lesson_buttons(
                lesson_data
            )
        )

    except Exception as error:

        await call.message.answer(
            "❌ Ошибка обновления:\n\n"
            f"{type(error).__name__}: "
            f"{error}"
        )


# ============================================================
# ОТВЕТ СТУДЕНТА
# ============================================================

@dp.callback_query(
    F.data.in_({
        "att:will",
        "att:wont"
    })
)
async def vote(
    call: CallbackQuery
):

    # ========================================================
    # ГЛАВНОЕ ИСПРАВЛЕНИЕ
    #
    # НЕ:
    # call.message.from_user
    #
    # А:
    # call.from_user
    # ========================================================

    user = call.from_user

    if call.message is None:

        await call.answer(
            "❌ Сообщение недоступно.",
            show_alert=True
        )

        return

    chat = getattr(
        call.message,
        "chat",
        None
    )

    if chat is None:

        await call.answer(
            "❌ Не удалось определить чат.",
            show_alert=True
        )

        return

    chat_id = chat.id

    # --------------------------------------------------------
    # Ищем активную перекличку
    # --------------------------------------------------------

    rollcall = active(
        chat_id
    )

    if not rollcall:

        await call.answer(
            "Перекличка уже завершена.",
            show_alert=True
        )

        return

    # --------------------------------------------------------
    # Статус
    # --------------------------------------------------------

    if call.data == "att:will":

        status = "Буду"

    else:

        status = "Не буду"

    # --------------------------------------------------------
    # Сохраняем студента
    # --------------------------------------------------------

    with db() as c:

        c.execute("""
            INSERT INTO students(
                chat_id,
                user_id,
                name,
                username,
                first_seen
            )

            VALUES(
                ?, ?, ?, ?, ?
            )

            ON CONFLICT(
                chat_id,
                user_id
            )

            DO UPDATE SET
                name=excluded.name,
                username=excluded.username
        """, (

            chat_id,

            user.id,

            user.full_name,

            user.username,

            now().isoformat()
        ))

        # ----------------------------------------------------
        # Сохраняем ответ
        # ----------------------------------------------------

        c.execute("""
            INSERT INTO attendance(
                rollcall_id,
                user_id,
                name,
                status,
                answered_at
            )

            VALUES(
                ?, ?, ?, ?, ?
            )

            ON CONFLICT(
                rollcall_id,
                user_id
            )

            DO UPDATE SET
                name=excluded.name,
                status=excluded.status,
                answered_at=excluded.answered_at
        """, (

            rollcall["id"],

            user.id,

            user.full_name,

            status,

            now().isoformat()
        ))

    await call.answer(
        f"Ответ сохранён: {status}"
    )

    # --------------------------------------------------------
    # Не отправляем отдельное сообщение
    # каждому студенту.
    # --------------------------------------------------------
    #
    # Это специально, чтобы чат не
    # превращался в поток сообщений.
    #
    # Кнопка просто сообщает:
    # "Ответ сохранён".
    # --------------------------------------------------------


# ============================================================
# РЕЗУЛЬТАТЫ
# ============================================================

def get_results(
    rollcall_id
):

    with db() as c:

        rollcall = c.execute("""
            SELECT *
            FROM rollcalls
            WHERE id=?
        """, (
            rollcall_id,
        )).fetchone()

        rows = c.execute("""
            SELECT
                name,
                status
            FROM attendance

            WHERE rollcall_id=?

            ORDER BY
                name COLLATE NOCASE
        """, (
            rollcall_id,
        )).fetchall()

    will = [
        row["name"]
        for row in rows
        if row["status"] == "Буду"
    ]

    wont = [
        row["name"]
        for row in rows
        if row["status"] == "Не буду"
    ]

    text = (
        "📊 РЕЗУЛЬТАТЫ\n\n"
        f"📚 {rollcall['subject']}\n"
        f"📅 {rollcall['started_at']}\n"
    )

    if rollcall["lesson_date"]:

        text += (
            f"🗓 Дата пары: "
            f"{rollcall['lesson_date']}\n"
        )

    if rollcall["lesson_time"]:

        text += (
            f"🕐 Время: "
            f"{rollcall['lesson_time']}\n"
        )

    text += "\n"

    text += (
        f"🟢 Будут — {len(will)}\n"
    )

    if will:

        text += "\n".join(
            "• " + name
            for name in will
        )

    else:

        text += "—"

    text += (
        f"\n\n🔴 Не будут — "
        f"{len(wont)}\n"
    )

    if wont:

        text += "\n".join(
            "• " + name
            for name in wont
        )

    else:

        text += "—"

    return text


@dp.message(
    F.text.lower() == "результаты"
)
async def attendance_command(
    message: Message
):

    ensure(message)

    if not await admin_only(message):
        return

    rollcall = active(
        message.chat.id
    )

    if not rollcall:

        await message.answer(
            "ℹ️ Сейчас нет активной "
            "переклички."
        )

        return

    await message.answer(
        get_results(
            rollcall["id"]
        )
    )


# ============================================================
# ЗАВЕРШЕНИЕ ПЕРЕКЛИЧКИ
# ============================================================

@dp.message(
    F.text.lower() == "завершить"
)
async def finish(
    message: Message
):

    ensure(message)

    if not await admin_only(message):
        return

    rollcall = active(
        message.chat.id
    )

    if not rollcall:

        await message.answer(
            "ℹ️ Активной переклички нет."
        )

        return

    result = get_results(
        rollcall["id"]
    )

    finished_at = now().strftime(
        "%d.%m.%Y %H:%M"
    )

    with db() as c:

        c.execute("""
            UPDATE rollcalls

            SET
                status='finished',
                finished_at=?

            WHERE id=?
        """, (
            finished_at,
            rollcall["id"]
        ))

    await message.answer(
        "🛑 ПЕРЕКЛИЧКА ЗАВЕРШЕНА!\n\n"
        "💾 Результаты сохранены в базе.\n\n"
        + result
    )


# ============================================================
# ИСТОРИЯ
# ============================================================

@dp.message(
    F.text.lower() == "история"
)
async def history(
    message: Message
):

    ensure(message)

    if not await admin_only(message):
        return

    with db() as c:

        rows = c.execute("""
            SELECT *
            FROM rollcalls

            WHERE
                chat_id=?
                AND status='finished'

            ORDER BY id DESC

            LIMIT 30
        """, (
            message.chat.id,
        )).fetchall()

    if not rows:

        await message.answer(
            "📚 История пока пустая."
        )

        return

    text = (
        "📚 ИСТОРИЯ ПЕРЕКЛИЧЕК\n\n"
    )

    for row in rows:

        with db() as c:

            will = c.execute("""
                SELECT COUNT(*)
                FROM attendance

                WHERE
                    rollcall_id=?
                    AND status='Буду'
            """, (
                row["id"],
            )).fetchone()[0]

            wont = c.execute("""
                SELECT COUNT(*)
                FROM attendance

                WHERE
                    rollcall_id=?
                    AND status='Не буду'
            """, (
                row["id"],
            )).fetchone()[0]

        text += (
            f"#{row['id']}\n"
            f"📚 {row['subject']}\n"
        )

        if row["lesson_date"]:

            text += (
                f"📅 {row['lesson_date']}\n"
            )

        if row["lesson_time"]:

            text += (
                f"🕐 {row['lesson_time']}\n"
            )

        text += (
            f"🟢 {will}  "
            f"🔴 {wont}\n"
            f"📝 Начата: "
            f"{row['started_at']}\n\n"
        )

    await message.answer(
        text[:4000]
    )


# ============================================================
# СТАТИСТИКА
# ============================================================

@dp.message(
    F.text.lower() == "статистика"
)
async def statistics(
    message: Message
):

    ensure(message)

    if not await admin_only(message):
        return

    with db() as c:

        people = c.execute("""
            SELECT DISTINCT
                a.user_id,
                a.name

            FROM attendance a

            JOIN rollcalls r
                ON r.id=a.rollcall_id

            WHERE r.chat_id=?

            ORDER BY
                a.name COLLATE NOCASE
        """, (
            message.chat.id,
        )).fetchall()

    if not people:

        await message.answer(
            "📈 Статистики пока нет."
        )

        return

    text = (
        "📈 СТАТИСТИКА ПОСЕЩАЕМОСТИ\n\n"
    )

    with db() as c:

        for person in people:

            total = c.execute("""
                SELECT COUNT(*)

                FROM attendance a

                JOIN rollcalls r
                    ON r.id=a.rollcall_id

                WHERE
                    r.chat_id=?
                    AND a.user_id=?
                    AND r.status='finished'
            """, (
                message.chat.id,
                person["user_id"]
            )).fetchone()[0]

            present = c.execute("""
                SELECT COUNT(*)

                FROM attendance a

                JOIN rollcalls r
                    ON r.id=a.rollcall_id

                WHERE
                    r.chat_id=?
                    AND a.user_id=?
                    AND a.status='Буду'
                    AND r.status='finished'
            """, (
                message.chat.id,
                person["user_id"]
            )).fetchone()[0]

            if total:

                percent = round(
                    present / total * 100,
                    1
                )

            else:

                percent = 0

            text += (
                f"👤 {person['name']}\n"
                f"Посещено: "
                f"{present}/{total} "
                f"({percent}%)\n\n"
            )

    await message.answer(
        text[:4000]
    )


# ============================================================
# ВЫГРУЗКА CSV
# ============================================================

@dp.message(
    F.text.lower() == "выгрузка"
)
async def export_csv(
    message: Message
):

    ensure(message)

    if not await admin_only(message):
        return

    with db() as c:

        rows = c.execute("""
            SELECT
                r.id,
                r.subject,
                r.lesson_date,
                r.lesson_time,
                r.teacher,
                r.classroom,
                r.started_at,
                r.finished_at,

                a.user_id,
                a.name,
                a.status,
                a.answered_at

            FROM attendance a

            JOIN rollcalls r
                ON r.id=a.rollcall_id

            WHERE r.chat_id=?

            ORDER BY
                r.id DESC,
                a.name COLLATE NOCASE
        """, (
            message.chat.id,
        )).fetchall()

    if not rows:

        await message.answer(
            "📥 Пока нет данных "
            "для выгрузки."
        )

        return

    output = io.StringIO()

    writer = csv.writer(
        output
    )

    writer.writerow([
        "ID переклички",
        "Предмет",
        "Дата пары",
        "Время пары",
        "Преподаватель",
        "Аудитория",
        "Начало переклички",
        "Конец переклички",
        "Telegram ID",
        "ФИО",
        "Статус",
        "Время ответа"
    ])

    for row in rows:

        writer.writerow([
            row["id"],
            row["subject"],
            row["lesson_date"],
            row["lesson_time"],
            row["teacher"],
            row["classroom"],
            row["started_at"],
            row["finished_at"],
            row["user_id"],
            row["name"],
            row["status"],
            row["answered_at"]
        ])

    data = output.getvalue().encode(
        "utf-8-sig"
    )

    await message.answer_document(
        BufferedInputFile(
            data,
            filename="посещаемость.csv"
        ),
        caption=(
            "📥 История посещаемости"
        )
    )


# ============================================================
# ДОБАВИТЬ РУЧНУЮ ПАРУ
# ============================================================

@dp.message(
    F.text.lower() == "добавить пару"
)
async def add_lesson(
    message: Message,
    state: FSMContext
):

    ensure(message)

    if not await admin_only(message):
        return

    await state.set_state(
        States.schedule_subject
    )

    await message.answer(
        "📚 Напиши название предмета."
    )


@dp.message(
    States.schedule_subject
)
async def schedule_subject(
    message: Message,
    state: FSMContext
):

    subject = (
        message.text or ""
    ).strip()

    if not subject:

        await message.answer(
            "❌ Напиши название предмета."
        )

        return

    await state.update_data(
        subject=subject
    )

    await state.set_state(
        States.schedule_day
    )

    await message.answer(
        "📅 Напиши день недели числом:\n\n"

        "1 — Понедельник\n"
        "2 — Вторник\n"
        "3 — Среда\n"
        "4 — Четверг\n"
        "5 — Пятница\n"
        "6 — Суббота\n"
        "7 — Воскресенье"
    )


@dp.message(
    States.schedule_day
)
async def schedule_day(
    message: Message,
    state: FSMContext
):

    value = (
        message.text or ""
    ).strip()

    if value not in (
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7"
    ):

        await message.answer(
            "❌ Напиши число от 1 до 7."
        )

        return

    await state.update_data(
        day=int(value)
    )

    await state.set_state(
        States.schedule_time
    )

    await message.answer(
        "⏰ Напиши время начала пары.\n\n"
        "Например:\n"
        "09:00"
    )


@dp.message(
    States.schedule_time
)
async def schedule_time(
    message: Message,
    state: FSMContext
):

    value = (
        message.text or ""
    ).strip()

    try:

        datetime.strptime(
            value,
            "%H:%M"
        )

    except ValueError:

        await message.answer(
            "❌ Неверный формат.\n\n"
            "Например: 09:00"
        )

        return

    data = await state.get_data()

    with db() as c:

        c.execute("""
            INSERT INTO schedule(
                chat_id,
                subject,
                day,
                lesson_time
            )

            VALUES(
                ?, ?, ?, ?
            )
        """, (
            message.chat.id,
            data["subject"],
            data["day"],
            value
        ))

    await state.clear()

    await message.answer(
        "✅ ПАРА ДОБАВЛЕНА!\n\n"
        f"📚 {data['subject']}\n"
        f"📅 {day_name(data['day'])}\n"
        f"⏰ {value}"
    )


# ============================================================
# УДАЛИТЬ РУЧНУЮ ПАРУ
# ============================================================

@dp.message(
    F.text.lower().startswith(
        "удалить пару"
    )
)
async def delete_lesson(
    message: Message
):

    ensure(message)

    if not await admin_only(message):
        return

    parts = (
        message.text or ""
    ).split()

    if len(parts) == 3:

        lesson_id = parts[2]

        if lesson_id.isdigit():

            lesson_id = int(
                lesson_id
            )

            with db() as c:

                row = c.execute("""
                    SELECT *
                    FROM schedule

                    WHERE
                        id=?
                        AND chat_id=?
                        AND enabled=1
                """, (
                    lesson_id,
                    message.chat.id
                )).fetchone()

                if not row:

                    await message.answer(
                        "❌ Пара с таким "
                        "ID не найдена."
                    )

                    return

                c.execute("""
                    UPDATE schedule

                    SET enabled=0

                    WHERE id=?
                """, (
                    lesson_id,
                ))

            await message.answer(
                "🗑 ПАРА УДАЛЕНА!\n\n"
                f"📚 {row['subject']}\n"
                f"📅 {day_name(row['day'])}\n"
                f"⏰ {row['lesson_time']}"
            )

            return

    # Показываем список

    with db() as c:

        rows = c.execute("""
            SELECT *
            FROM schedule

            WHERE
                chat_id=?
                AND enabled=1

            ORDER BY
                day,
                lesson_time
        """, (
            message.chat.id,
        )).fetchall()

    if not rows:

        await message.answer(
            "📭 Ручных пар нет."
        )

        return

    text = (
        "🗑 РУЧНЫЕ ПАРЫ\n\n"
    )

    for row in rows:

        text += (
            f"ID: {row['id']}\n"
            f"📚 {row['subject']}\n"
            f"📅 {day_name(row['day'])}\n"
            f"⏰ {row['lesson_time']}\n\n"
        )

    text += (
        "Для удаления напиши:\n"
        "удалить пару ID\n\n"
        "Например:\n"
        "удалить пару 3"
    )

    await message.answer(
        text
    )


# ============================================================
# НАПОМИНАНИЯ
# ============================================================

async def reminder_loop():

    sent = set()

    while True:

        try:

            current = now()

            day = current.isoweekday()

            with db() as c:

                rows = c.execute("""
                    SELECT *
                    FROM schedule

                    WHERE
                        day=?
                        AND enabled=1
                """, (
                    day,
                )).fetchall()

            for row in rows:

                try:

                    hour, minute = map(
                        int,
                        row[
                            "lesson_time"
                        ].split(":")
                    )

                except Exception:

                    continue

                lesson = current.replace(
                    hour=hour,
                    minute=minute,
                    second=0,
                    microsecond=0
                )

                minutes = (
                    lesson - current
                ).total_seconds() / 60

                key = (
                    row["id"],
                    current.date()
                )

                reminder = (
                    row["reminder"]
                    or 30
                )

                if (
                    0 <= minutes <= reminder
                    and key not in sent
                ):

                    if not active(
                        row["chat_id"]
                    ):

                        await bot.send_message(
                            row["chat_id"],

                            "⏰ НАПОМИНАНИЕ\n\n"

                            f"Через "
                            f"{max(1, round(minutes))} "
                            f"мин. пара:\n"

                            f"📚 {row['subject']}\n\n"

                            "Староста может начать "
                            "перекличку командой:\n"

                            "перекличка"
                        )

                    sent.add(key)

            # Оставляем только сегодняшний день

            sent = {
                item
                for item in sent

                if item[1]
                == current.date()
            }

        except Exception as error:

            print(
                "Ошибка напоминаний:",
                error
            )

        await asyncio.sleep(
            60
        )


# ============================================================
# РУССКИЕ КОМАНДЫ-АЛИАСЫ
# ============================================================

@dp.message(
    F.text.casefold() == "старт"
)
async def start_text(
    message: Message,
    state: FSMContext
):

    await start(
        message,
        state
    )


# ============================================================
# ЗАПУСК
# ============================================================

async def main():

    init_db()

    print(
        "======================================"
    )

    print(
        "БОТ ЗАПУЩЕН"
    )

    print(
        f"Институт: {INSTITUTE}"
    )

    print(
        f"Курс: {COURSE}"
    )

    print(
        f"Группа: {GROUP_NAME}"
    )

    print(
        f"ID группы: {GROUP_ID}"
    )

    print(
        f"Сайт: {SCHEDULE_URL}"
    )

    print(
        "======================================"
    )

    await asyncio.gather(

        dp.start_polling(
            bot
        ),

        reminder_loop()
    )


if __name__ == "__main__":

    asyncio.run(main())
