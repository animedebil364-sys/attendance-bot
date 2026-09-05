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
    BufferedInputFile,
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

TZ = ZoneInfo(
    os.getenv("TZ", "Europe/Moscow")
)

DB_PATH = os.getenv(
    "DB_PATH",
    "attendance.db"
)

# Сайт расписания
SCHEDULE_URL = "https://tt2.vogu35.ru/"

# ============================================================
# ДАННЫЕ ГРУППЫ
# ============================================================
#
# ВАЖНО:
# ID группы здесь больше НЕ задаём.
#
# Бот сам попробует найти его на сайте
# по институту, курсу и названию группы.
#

INSTITUTE = "ИСИ"
COURSE = "1"
GROUP_NAME = "1Б08 №12"

# Здесь ID будет определён автоматически.
GROUP_ID = None


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

    # Перекличка
    selecting_lesson = State()

    # Ручная пара
    schedule_subject = State()
    schedule_day = State()
    schedule_time = State()


# ============================================================
# ВРЕМЯ
# ============================================================

def now():
    return datetime.now(TZ)


# ============================================================
# БАЗА ДАННЫХ
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
            updated_at TEXT,

            UNIQUE(
                chat_id,
                lesson_date,
                lesson_time,
                subject,
                subgroup
            )
        );

        """)

        # ----------------------------------------------------
        # МИГРАЦИЯ СТАРОЙ БАЗЫ
        # ----------------------------------------------------

        columns = [
            row["name"]
            for row in c.execute(
                "PRAGMA table_info(rollcalls)"
            ).fetchall()
        ]

        required_columns = {
            "lesson_date": "TEXT",
            "lesson_time": "TEXT",
            "teacher": "TEXT",
            "classroom": "TEXT",
        }

        for column, column_type in required_columns.items():

            if column not in columns:

                c.execute(
                    f"""
                    ALTER TABLE rollcalls
                    ADD COLUMN {column} {column_type}
                    """
                )


# ============================================================
# РЕГИСТРАЦИЯ
# ============================================================

def ensure(message: Message):

    if not message.chat:
        return

    with db() as c:

        c.execute(
            """
            INSERT INTO chats(
                chat_id,
                title
            )
            VALUES(?, ?)

            ON CONFLICT(chat_id)
            DO UPDATE SET
                title=excluded.title
            """,
            (
                message.chat.id,
                message.chat.title
                or "Личный чат",
            )
        )

        # Для CallbackQuery нельзя использовать
        # message.from_user, поэтому эта функция
        # вызывается только с настоящими Message.
        #
        # Проверяем наличие атрибута безопасно.

        user = getattr(
            message,
            "from_user",
            None
        )

        if user:

            c.execute(
                """
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
                """,
                (
                    message.chat.id,
                    user.id,
                    user.full_name,
                    user.username,
                    now().isoformat(),
                )
            )


# ============================================================
# ПРОВЕРКА СТАРОСТЫ
# ============================================================

def is_starosta(message: Message):

    user = getattr(
        message,
        "from_user",
        None
    )

    if not user:
        return False

    with db() as c:

        row = c.execute(
            """
            SELECT starosta_id
            FROM chats
            WHERE chat_id=?
            """,
            (
                message.chat.id,
            )
        ).fetchone()

    if not row:
        return False

    return (
        row["starosta_id"]
        == user.id
    )


async def admin_only(message: Message):

    if not is_starosta(message):

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

        return c.execute(
            """
            SELECT *
            FROM rollcalls

            WHERE chat_id=?
              AND status='active'

            ORDER BY id DESC

            LIMIT 1
            """,
            (
                chat_id,
            )
        ).fetchone()


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
# КНОПКИ ПАР
# ============================================================

def lesson_buttons(lessons):

    buttons = []

    for index, lesson in enumerate(lessons):

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
# ДАТЫ ДЛЯ ЗАПРОСА
# ============================================================

def schedule_dates():

    today = now().date()

    monday = (
        today
        - timedelta(
            days=today.weekday()
        )
    )

    return [
        (
            monday
            + timedelta(days=i)
        ).strftime("%Y-%m-%d")
        for i in range(14)
    ]


# ============================================================
# ОЧИСТКА
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
# ПОИСК ID ГРУППЫ
# ============================================================

async def find_group_id(session):

    global GROUP_ID

    # --------------------------------------------------------
    # Если ID уже найден ранее — используем его.
    # --------------------------------------------------------

    if GROUP_ID:
        return GROUP_ID

    print(
        "======================================"
    )

    print(
        "ПОИСК ID ГРУППЫ"
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
        "======================================"
    )

    # --------------------------------------------------------
    # Получаем главную страницу
    # --------------------------------------------------------

    async with session.get(
        SCHEDULE_URL
    ) as response:

        print(
            "GET сайта:",
            response.status
        )

        response.raise_for_status()

        html = await response.text()

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    # --------------------------------------------------------
    # Ищем все select.
    # --------------------------------------------------------

    selects = soup.find_all(
        "select"
    )

    print(
        "Найдено select:",
        len(selects)
    )

    # --------------------------------------------------------
    # Сначала ищем option с названием группы.
    # --------------------------------------------------------

    normalized_target = clean_text(
        GROUP_NAME
    ).casefold()

    for select in selects:

        for option in select.find_all(
            "option"
        ):

            text = clean_text(
                option.get_text(
                    " ",
                    strip=True
                )
            )

            value = (
                option.get("value")
                or ""
            ).strip()

            if not value:
                continue

            normalized_text = (
                text.casefold()
            )

            # Точное совпадение
            if (
                normalized_text
                == normalized_target
            ):

                GROUP_ID = value

                print(
                    "ID группы найден:",
                    GROUP_ID
                )

                return GROUP_ID

    # --------------------------------------------------------
    # Более мягкий поиск.
    # Например:
    #
    # "1Б08 №12"
    # "1Б08 №12 (ИСИ)"
    # --------------------------------------------------------

    target_without_spaces = (
        normalized_target
        .replace(" ", "")
    )

    for select in selects:

        for option in select.find_all(
            "option"
        ):

            text = clean_text(
                option.get_text(
                    " ",
                    strip=True
                )
            )

            value = (
                option.get("value")
                or ""
            ).strip()

            if not value:
                continue

            normalized_text = (
                text.casefold()
                .replace(" ", "")
            )

            if (
                target_without_spaces
                in normalized_text
            ):

                GROUP_ID = value

                print(
                    "ID группы найден:",
                    GROUP_ID
                )

                return GROUP_ID

    # --------------------------------------------------------
    # Иногда данные группы находятся
    # не в option, а прямо в HTML/JS.
    # --------------------------------------------------------

    html_lower = html.casefold()

    if (
        normalized_target
        in html_lower
    ):

        # Пытаемся найти значение
        # рядом с названием группы.

        patterns = [

            re.compile(
                r'value=["\']([^"\']+)["\'][^>]*>'
                r'\s*'
                + re.escape(GROUP_NAME),
                re.IGNORECASE
            ),

            re.compile(
                r'value=["\']([^"\']+)["\'][^>]*>'
                r'[^<]{0,50}'
                + re.escape(GROUP_NAME),
                re.IGNORECASE
            ),

        ]

        for pattern in patterns:

            match = pattern.search(
                html
            )

            if match:

                GROUP_ID = (
                    match.group(1)
                    .strip()
                )

                if GROUP_ID:

                    print(
                        "ID группы найден в HTML:",
                        GROUP_ID
                    )

                    return GROUP_ID

    # --------------------------------------------------------
    # Если не нашли
    # --------------------------------------------------------

    print(
        "ID группы автоматически "
        "найти не удалось."
    )

    return None


# ============================================================
# ЗАПРОС РАСПИСАНИЯ
# ============================================================

async def fetch_schedule_html():

    dates = schedule_dates()

    date_start = dates[0]

    date_end = dates[-1]

    headers = {

        "User-Agent":
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0.0.0 "
            "Safari/537.36",

        "Referer":
            SCHEDULE_URL,

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

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers
    ) as session:

        # ----------------------------------------------------
        # Сначала открываем сайт.
        # ----------------------------------------------------

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
                "Ошибка GET:",
                error
            )

        # ----------------------------------------------------
        # Автоматически ищем ID группы.
        # ----------------------------------------------------

        group_id = await find_group_id(
            session
        )

        if not group_id:

            raise RuntimeError(
                "Не удалось автоматически "
                "найти ID группы "
                f"«{GROUP_NAME}» "
                "на сайте."
            )

        payload = {

            "group_id":
                group_id,

            "date_start":
                date_start,

            "date_end":
                date_end,

            "selected_lesson_type":
                "typical"
        }

        print(
            "======================================"
        )

        print(
            "ЗАПРОС РАСПИСАНИЯ"
        )

        print(
            f"ID группы: {group_id}"
        )

        print(
            f"Дата начала: {date_start}"
        )

        print(
            f"Дата конца: {date_end}"
        )

        print(
            "======================================"
        )

        # ----------------------------------------------------
        # POST
        # ----------------------------------------------------

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
# ДАТА
# ============================================================

def normalize_date(value):

    value = clean_text(
        value
    )

    formats = [
        "%d.%m.%Y",
        "%d.%m.%y",
        "%Y-%m-%d",
    ]

    for fmt in formats:

        try:

            dt = datetime.strptime(
                value,
                fmt
            )

            return dt.strftime(
                "%Y-%m-%d"
            )

        except ValueError:
            pass

    return None


# ============================================================
# ВРЕМЯ
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
# ТИП ПАРЫ
# ============================================================

def detect_lesson_type(text):

    value = text.lower()

    if "лаборатор" in value:
        return "Лабораторная"

    if "практи" in value:
        return "Практика"

    if "лекц" in value:
        return "Лекция"

    if "семинар" in value:
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
# ДНИ
# ============================================================

def russian_day_name(date_string):

    try:

        dt = datetime.strptime(
            date_string,
            "%Y-%m-%d"
        )

    except Exception:

        return ""

    return day_name(
        dt.isoweekday()
    )


# ============================================================
# ПОИСК ДАТ
# ============================================================

def extract_dates(soup):

    dates = []

    patterns = [

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

        text = clean_text(
            text
        )

        for pattern in patterns:

            for value in pattern.findall(
                text
            ):

                normalized = normalize_date(
                    value
                )

                if (
                    normalized
                    and normalized not in dates
                ):

                    dates.append(
                        normalized
                    )

    return dates


# ============================================================
# ПАРСИНГ
# ============================================================

def parse_schedule(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    dates = extract_dates(
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

        container = parent

        # Поднимаемся максимум на 8 уровней.

        for _ in range(8):

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

        parts = []

        for value in container.stripped_strings:

            value = clean_text(
                value
            )

            if value:
                parts.append(value)

        # ----------------------------------------------------
        # ПРЕДМЕТ
        # ----------------------------------------------------

        subject = ""

        for tag in container.find_all(
            [
                "b",
                "strong",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6"
            ]
        ):

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

        # ----------------------------------------------------
        # Если заголовка нет
        # ----------------------------------------------------

        if not subject:

            ignored = {
                "лекция",
                "практика",
                "лабораторная",
                "семинар",
                "занятие"
            }

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

                if value.lower() in ignored:
                    continue

                if len(value) < 3:
                    continue

                subject = value

                break

        if not subject:
            continue

        # ----------------------------------------------------
        # ПРЕПОДАВАТЕЛЬ
        # ----------------------------------------------------

        teacher = detect_teacher(
            parts
        )

        # ----------------------------------------------------
        # АУДИТОРИЯ
        # ----------------------------------------------------

        classroom = detect_classroom(
            full_text
        )

        # ----------------------------------------------------
        # ТИП
        # ----------------------------------------------------

        lesson_type = detect_lesson_type(
            full_text
        )

        # ----------------------------------------------------
        # ПОДГРУППА
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

        lessons.append(
            {
                "date": "",
                "time": lesson_time,
                "subject": subject,
                "teacher": teacher,
                "classroom": classroom,
                "lesson_type": lesson_type,
                "subgroup": subgroup
            }
        )

    # --------------------------------------------------------
    # Убираем дубликаты
    # --------------------------------------------------------

    unique = []

    seen = set()

    for lesson in lessons:

        key = (
            lesson["time"],
            lesson["subject"],
            lesson["teacher"],
            lesson["classroom"],
            lesson["lesson_type"],
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
    # Распределение дат
    #
    # Если сайт отдаёт даты и пары
    # без явной связи в HTML,
    # используем последовательность дат.
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
# СОХРАНЕНИЕ РАСПИСАНИЯ
# ============================================================

def save_remote_schedule(
    chat_id,
    lessons
):

    saved = 0

    with db() as c:

        for lesson in lessons:

            lesson_date = lesson.get(
                "date",
                ""
            )

            if not lesson_date:
                continue

            existing = c.execute(
                """
                SELECT id
                FROM remote_schedule

                WHERE chat_id=?
                  AND lesson_date=?
                  AND lesson_time=?
                  AND subject=?
                  AND subgroup=?
                """,
                (
                    chat_id,
                    lesson_date,
                    lesson["time"],
                    lesson["subject"],
                    lesson.get(
                        "subgroup",
                        ""
                    )
                )
            ).fetchone()

            if existing:

                c.execute(
                    """
                    UPDATE remote_schedule

                    SET
                        day_name=?,
                        teacher=?,
                        classroom=?,
                        lesson_type=?,
                        subgroup=?,
                        source=?,
                        updated_at=?

                    WHERE id=?
                    """,
                    (
                        russian_day_name(
                            lesson_date
                        ),
                        lesson.get(
                            "teacher",
                            ""
                        ),
                        lesson.get(
                            "classroom",
                            ""
                        ),
                        lesson.get(
                            "lesson_type",
                            ""
                        ),
                        lesson.get(
                            "subgroup",
                            ""
                        ),
                        SCHEDULE_URL,
                        now().isoformat(),
                        existing["id"]
                    )
                )

            else:

                c.execute(
                    """
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
                    """,
                    (
                        chat_id,
                        lesson_date,
                        russian_day_name(
                            lesson_date
                        ),
                        lesson["time"],
                        lesson["subject"],
                        lesson.get(
                            "teacher",
                            ""
                        ),
                        lesson.get(
                            "classroom",
                            ""
                        ),
                        lesson.get(
                            "lesson_type",
                            ""
                        ),
                        lesson.get(
                            "subgroup",
                            ""
                        ),
                        SCHEDULE_URL,
                        now().isoformat()
                    )
                )

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

    return save_remote_schedule(
        chat_id,
        lessons
    )


# ============================================================
# ПОЛУЧИТЬ ПАРЫ СЕГОДНЯ
# ============================================================

def get_today_lessons(
    chat_id
):

    today_date = now().strftime(
        "%Y-%m-%d"
    )

    with db() as c:

        rows = c.execute(
            """
            SELECT *
            FROM remote_schedule

            WHERE chat_id=?
              AND lesson_date=?

            ORDER BY lesson_time
            """,
            (
                chat_id,
                today_date
            )
        ).fetchall()

    return rows


# ============================================================
# ФОРМАТ ПАРЫ
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
            f"👨‍🏫 "
            f"{lesson['teacher']}\n"
        )

    if lesson["classroom"]:

        text += (
            f"🏫 "
            f"{lesson['classroom']}\n"
        )

    if lesson["lesson_type"]:

        text += (
            f"📖 "
            f"{lesson['lesson_type']}\n"
        )

    if lesson["subgroup"]:

        text += (
            f"👥 "
            f"{lesson['subgroup']} "
            f"подгруппа\n"
        )

    return text


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

        "🗓 расписание — всё расписание\n"
        "📌 сегодня — пары сегодня\n"
        "🔄 обновить расписание — загрузить с сайта\n\n"

        "📅 добавить пару — ручная пара\n"
        "🗑 удалить пару — удалить ручную пару\n\n"

        "❌ отмена — отменить действие\n"
        "ℹ️ помощь — помощь"
    )


# ============================================================
# ПОМОЩЬ
# ============================================================

@dp.message(
    F.text.casefold() == "помощь"
)
async def help_text(
    message: Message
):

    ensure(message)

    await message.answer(
        "📚 КОМАНДЫ БОТА\n\n"

        "👑 староста\n"
        "Назначить себя старостой.\n\n"

        "📋 перекличка\n"
        "Начать перекличку и выбрать пару "
        "из расписания.\n\n"

        "📊 результаты\n"
        "Показать текущие ответы.\n\n"

        "🛑 завершить\n"
        "Закончить текущую перекличку.\n\n"

        "📚 история\n"
        "История перекличек.\n\n"

        "📈 статистика\n"
        "Статистика посещаемости.\n\n"

        "📥 выгрузка\n"
        "Скачать всю историю CSV.\n\n"

        "🗓 расписание\n"
        "Показать расписание.\n\n"

        "📌 сегодня\n"
        "Показать пары сегодня.\n\n"

        "🔄 обновить расписание\n"
        "Получить новое расписание "
        "с сайта ВоГУ.\n\n"

        "📅 добавить пару\n"
        "Добавить ручную пару.\n\n"

        "🗑 удалить пару\n"
        "Удалить ручную пару.\n\n"

        "❌ отмена\n"
        "Отменить действие."
    )


# ============================================================
# ОТМЕНА
# ============================================================

@dp.message(
    F.text.casefold() == "отмена"
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
    F.text.casefold() == "староста"
)
async def set_starosta(
    message: Message
):

    ensure(message)

    with db() as c:

        row = c.execute(
            """
            SELECT starosta_id
            FROM chats
            WHERE chat_id=?
            """,
            (
                message.chat.id,
            )
        ).fetchone()

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

        c.execute(
            """
            UPDATE chats

            SET
                starosta_id=?,
                starosta_name=?

            WHERE chat_id=?
            """,
            (
                message.from_user.id,
                message.from_user.full_name,
                message.chat.id
            )
        )

    await message.answer(
        "👑 Ты назначен старостой этой группы!"
    )


# ============================================================
# РАСПИСАНИЕ
# ============================================================

@dp.message(
    F.text.casefold() == "расписание"
)
async def schedule(
    message: Message
):

    ensure(message)

    with db() as c:

        rows = c.execute(
            """
            SELECT *
            FROM remote_schedule

            WHERE chat_id=?

            ORDER BY
                lesson_date,
                lesson_time
            """,
            (
                message.chat.id,
            )
        ).fetchall()

    # --------------------------------------------------------
    # Если ещё ничего нет — загружаем.
    # --------------------------------------------------------

    if not rows:

        await message.answer(
            "🔄 Расписание ещё не загружено.\n\n"
            "Автоматически ищу твою группу "
            f"«{GROUP_NAME}» на сайте..."
        )

        try:

            saved = await update_schedule(
                message.chat.id
            )

            if saved == 0:

                await message.answer(
                    "⚠️ Сайт ответил, "
                    "но расписание "
                    "не удалось распознать."
                )

                return

        except asyncio.TimeoutError:

            await message.answer(
                "❌ Сайт не ответил вовремя."
            )

            return

        except Exception as error:

            await message.answer(
                "❌ Не удалось получить "
                "расписание.\n\n"
                f"{type(error).__name__}: "
                f"{error}"
            )

            return

        with db() as c:

            rows = c.execute(
                """
                SELECT *
                FROM remote_schedule

                WHERE chat_id=?

                ORDER BY
                    lesson_date,
                    lesson_time
                """,
                (
                    message.chat.id,
                )
            ).fetchall()

    # --------------------------------------------------------
    # Формируем сообщение.
    # --------------------------------------------------------

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

    # Telegram ограничивает длину сообщения.
    # Разбиваем расписание на части.

    for i in range(
        0,
        len(text),
        3900
    ):

        await message.answer(
            text[i:i + 3900]
        )


# ============================================================
# СЕГОДНЯ
# ============================================================

@dp.message(
    F.text.casefold() == "сегодня"
)
async def today(
    message: Message
):

    ensure(message)

    rows = get_today_lessons(
        message.chat.id
    )

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
    F.text.casefold()
    == "обновить расписание"
)
async def refresh_schedule(
    message: Message
):

    ensure(message)

    if not await admin_only(
        message
    ):
        return

    await message.answer(
        "🔄 Обновляю расписание...\n\n"
        f"🏫 {INSTITUTE}\n"
        f"🎓 {COURSE} курс\n"
        f"👥 {GROUP_NAME}\n\n"
        "🔎 ID группы будет найден "
        "автоматически."
    )

    try:

        saved = await update_schedule(
            message.chat.id
        )

        if saved == 0:

            await message.answer(
                "⚠️ Сайт ответил, "
                "но расписание "
                "не удалось распознать."
            )

            return

        await message.answer(
            "✅ РАСПИСАНИЕ ОБНОВЛЕНО!\n\n"
            f"📚 Обработано занятий: {saved}\n\n"
            "Теперь можно написать:\n"
            "📌 сегодня\n"
            "или\n"
            "🗓 расписание"
        )

    except asyncio.TimeoutError:

        await message.answer(
            "❌ Сайт не ответил вовремя.\n\n"
            "Сервер расписания слишком "
            "долго отвечает."
        )

    except Exception as error:

        await message.answer(
            "❌ Не удалось получить "
            "расписание.\n\n"
            f"{type(error).__name__}: "
            f"{error}"
        )


# ============================================================
# НАЧАЛО ПЕРЕКЛИЧКИ
# ============================================================

@dp.message(
    F.text.casefold() == "перекличка"
)
async def rollcall(
    message: Message,
    state: FSMContext
):

    ensure(message)

    if not await admin_only(
        message
    ):
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

        except Exception as error:

            await message.answer(
                "❌ Не удалось загрузить "
                "расписание.\n\n"
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
            "пар в расписании."
        )

        return

    lesson_data = []

    for lesson in lessons:

        lesson_data.append(
            {
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
            }
        )

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

    with db() as c:

        row = c.execute(
            """
            SELECT starosta_id
            FROM chats
            WHERE chat_id=?
            """,
            (
                chat_id,
            )
        ).fetchone()

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
    # Закрываем старую активную
    # --------------------------------------------------------

    with db() as c:

        c.execute(
            """
            UPDATE rollcalls

            SET
                status='finished',
                finished_at=?

            WHERE
                chat_id=?
                AND status='active'
            """,
            (
                now().strftime(
                    "%d.%m.%Y %H:%M"
                ),
                chat_id
            )
        )

        c.execute(
            """
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
            """,
            (
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
            )
        )

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
# ОБНОВИТЬ ИЗ КНОПКИ
# ============================================================

@dp.callback_query(
    F.data == "schedule:refresh"
)
async def refresh_from_button(
    call: CallbackQuery
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

        row = c.execute(
            """
            SELECT starosta_id
            FROM chats
            WHERE chat_id=?
            """,
            (
                chat.id,
            )
        ).fetchone()

    if (
        not row
        or row["starosta_id"]
        != user.id
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

            lesson_data.append(
                {
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
                }
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
# ОТМЕТКА СТУДЕНТА
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

    # ВАЖНО:
    #
    # Здесь нельзя делать:
    #
    # call.message.from_user
    #
    # Потому что callback.message может быть
    # InaccessibleMessage.
    #
    # Правильно:
    #
    # call.from_user

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

    rollcall = active(
        chat_id
    )

    if not rollcall:

        await call.answer(
            "Перекличка уже завершена.",
            show_alert=True
        )

        return

    if call.data == "att:will":

        status = "Буду"

    else:

        status = "Не буду"

    with db() as c:

        c.execute(
            """
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
            """,
            (
                chat_id,
                user.id,
                user.full_name,
                user.username,
                now().isoformat()
            )
        )

        c.execute(
            """
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
            """,
            (
                rollcall["id"],
                user.id,
                user.full_name,
                status,
                now().isoformat()
            )
        )

    await call.answer(
        f"Ответ сохранён: {status}"
    )


# ============================================================
# РЕЗУЛЬТАТЫ
# ============================================================

def get_results(
    rollcall_id
):

    with db() as c:

        rollcall = c.execute(
            """
            SELECT *
            FROM rollcalls

            WHERE id=?
            """,
            (
                rollcall_id,
            )
        ).fetchone()

        rows = c.execute(
            """
            SELECT
                name,
                status
            FROM attendance

            WHERE rollcall_id=?

            ORDER BY
                name COLLATE NOCASE
            """,
            (
                rollcall_id,
            )
        ).fetchall()

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
        f"🕐 {rollcall['started_at']}\n"
    )

    if rollcall["lesson_date"]:

        text += (
            f"📅 Дата пары: "
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
    F.text.casefold()
    == "результаты"
)
async def attendance_command(
    message: Message
):

    ensure(message)

    if not await admin_only(
        message
    ):
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
# ЗАВЕРШЕНИЕ
# ============================================================

@dp.message(
    F.text.casefold()
    == "завершить"
)
async def finish(
    message: Message
):

    ensure(message)

    if not await admin_only(
        message
    ):
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

        c.execute(
            """
            UPDATE rollcalls

            SET
                status='finished',
                finished_at=?

            WHERE id=?
            """,
            (
                finished_at,
                rollcall["id"]
            )
        )

    await message.answer(
        "🛑 ПЕРЕКЛИЧКА ЗАВЕРШЕНА!\n\n"
        "💾 Результаты сохранены "
        "в базе.\n\n"
        + result
    )


# ============================================================
# ИСТОРИЯ
# ============================================================

@dp.message(
    F.text.casefold()
    == "история"
)
async def history(
    message: Message
):

    ensure(message)

    if not await admin_only(
        message
    ):
        return

    with db() as c:

        rows = c.execute(
            """
            SELECT *
            FROM rollcalls

            WHERE
                chat_id=?
                AND status='finished'

            ORDER BY id DESC

            LIMIT 30
            """,
            (
                message.chat.id,
            )
        ).fetchall()

    if not rows:

        await message.answer(
            "📚 История пока пустая."
        )

        return

    text = (
        "📚 ИСТОРИЯ ПЕРЕКЛИЧЕК\n\n"
    )

    with db() as c:

        for row in rows:

            will = c.execute(
                """
                SELECT COUNT(*)
                FROM attendance

                WHERE
                    rollcall_id=?
                    AND status='Буду'
                """,
                (
                    row["id"],
                )
            ).fetchone()[0]

            wont = c.execute(
                """
                SELECT COUNT(*)
                FROM attendance

                WHERE
                    rollcall_id=?
                    AND status='Не буду'
                """,
                (
                    row["id"],
                )
            ).fetchone()[0]

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
    F.text.casefold()
    == "статистика"
)
async def statistics(
    message: Message
):

    ensure(message)

    if not await admin_only(
        message
    ):
        return

    with db() as c:

        people = c.execute(
            """
            SELECT DISTINCT
                a.user_id,
                a.name

            FROM attendance a

            JOIN rollcalls r
                ON r.id=a.rollcall_id

            WHERE r.chat_id=?

            ORDER BY
                a.name COLLATE NOCASE
            """,
            (
                message.chat.id,
            )
        ).fetchall()

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

            total = c.execute(
                """
                SELECT COUNT(*)

                FROM attendance a

                JOIN rollcalls r
                    ON r.id=a.rollcall_id

                WHERE
                    r.chat_id=?
                    AND a.user_id=?
                    AND r.status='finished'
                """,
                (
                    message.chat.id,
                    person["user_id"]
                )
            ).fetchone()[0]

            present = c.execute(
                """
                SELECT COUNT(*)

                FROM attendance a

                JOIN rollcalls r
                    ON r.id=a.rollcall_id

                WHERE
                    r.chat_id=?
                    AND a.user_id=?
                    AND a.status='Буду'
                    AND r.status='finished'
                """,
                (
                    message.chat.id,
                    person["user_id"]
                )
            ).fetchone()[0]

            percent = (
                round(
                    present / total * 100,
                    1
                )
                if total
                else 0
            )

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
    F.text.casefold()
    == "выгрузка"
)
async def export_csv(
    message: Message
):

    ensure(message)

    if not await admin_only(
        message
    ):
        return

    with db() as c:

        rows = c.execute(
            """
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
            """,
            (
                message.chat.id,
            )
        ).fetchall()

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
    F.text.casefold()
    == "добавить пару"
)
async def add_lesson(
    message: Message,
    state: FSMContext
):

    ensure(message)

    if not await admin_only(
        message
    ):
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

        c.execute(
            """
            INSERT INTO schedule(
                chat_id,
                subject,
                day,
                lesson_time
            )

            VALUES(
                ?, ?, ?, ?
            )
            """,
            (
                message.chat.id,
                data["subject"],
                data["day"],
                value
            )
        )

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
    F.text.casefold()
    .startswith("удалить пару")
)
async def delete_lesson(
    message: Message
):

    ensure(message)

    if not await admin_only(
        message
    ):
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

                row = c.execute(
                    """
                    SELECT *
                    FROM schedule

                    WHERE
                        id=?
                        AND chat_id=?
                        AND enabled=1
                    """,
                    (
                        lesson_id,
                        message.chat.id
                    )
                ).fetchone()

                if not row:

                    await message.answer(
                        "❌ Пара с таким "
                        "ID не найдена."
                    )

                    return

                c.execute(
                    """
                    UPDATE schedule

                    SET enabled=0

                    WHERE id=?
                    """,
                    (
                        lesson_id,
                    )
                )

            await message.answer(
                "🗑 ПАРА УДАЛЕНА!\n\n"
                f"📚 {row['subject']}\n"
                f"📅 {day_name(row['day'])}\n"
                f"⏰ {row['lesson_time']}"
            )

            return

    with db() as c:

        rows = c.execute(
            """
            SELECT *
            FROM schedule

            WHERE
                chat_id=?
                AND enabled=1

            ORDER BY
                day,
                lesson_time
            """,
            (
                message.chat.id,
            )
        ).fetchall()

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

                rows = c.execute(
                    """
                    SELECT *
                    FROM schedule

                    WHERE
                        day=?
                        AND enabled=1
                    """,
                    (
                        day,
                    )
                ).fetchall()

            for row in rows:

                try:

                    hour, minute = map(
                        int,
                        row["lesson_time"].split(":")
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
                            f"мин. пара:\n\n"

                            f"📚 {row['subject']}\n\n"

                            "Староста может начать "
                            "перекличку командой:\n"
                            "перекличка"
                        )

                    sent.add(key)

            # Оставляем только сегодняшний день.

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
# КОМАНДА СТАРТ
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
        "ID группы: определяется автоматически"
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

    asyncio.run(
        main()
    )
