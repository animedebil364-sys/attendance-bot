import os
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
from aiogram.types import Message, CallbackQuery
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

TZ = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))

DB_PATH = os.getenv("DB_PATH", "attendance.db")

SCHEDULE_URL = "https://tt2.vogu35.ru/"

# ИСИ → 1 курс → 1Б08 №12
GROUP_ID = "543"
INSTITUTE = "ИСИ"
COURSE = "1"
GROUP_NAME = "1Б08 №12"


bot = Bot(TOKEN)

dp = Dispatcher(
    storage=MemoryStorage()
)


# ============================================================
# СОСТОЯНИЯ
# ============================================================

class States(StatesGroup):
    subject = State()

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
    connection = sqlite3.connect(DB_PATH)
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
            PRIMARY KEY(rollcall_id, user_id)
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


# ============================================================
# РЕГИСТРАЦИЯ ПОЛЬЗОВАТЕЛЯ
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
            message.chat.title or "Личный чат"
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

                ON CONFLICT(chat_id, user_id)
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
# ПРОВЕРКА СТАРОСТЫ
# ============================================================

def is_starosta(message: Message):

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


# ============================================================
# КЛАВИАТУРА ПЕРЕКЛИЧКИ
# ============================================================

def attendance_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🟢 Буду",
                    callback_data="will"
                ),
                InlineKeyboardButton(
                    text="🔴 Не буду",
                    callback_data="wont"
                )
            ]
        ]
    )


# ============================================================
# START
# ============================================================

@dp.message(Command("start"))
async def start(message: Message):

    ensure(message)

    await message.answer(
        "👋 Привет!\n\n"
        "Я бот для учёта посещаемости группы.\n\n"

        "👑 СТАРОСТА\n"
        "set староста — назначить себя старостой\n\n"

        "📝 ПЕРЕКЛИЧКА\n"
        "начать перекличку — начать\n"
        "результаты — результаты\n"
        "финиш — завершить\n\n"

        "📅 РАСПИСАНИЕ\n"
        "расписание — показать расписание\n"
        "сегодня — пары сегодня\n"
        "обновить расписание — загрузить с сайта\n\n"

        "➕ СВОИ ПАРЫ\n"
        "добавить пару — добавить\n"
        "удалить пару — удалить\n"
        "мое расписание — сохранённые пары\n\n"

        "📊 БАЗА\n"
        "студенты — список студентов\n"
        "история — история перекличек\n"
        "статистика — статистика"
    )


# ============================================================
# СТАРОСТА
# ============================================================

@dp.message(Command("set_starosta"))
async def set_starosta(message: Message):

    ensure(message)

    with db() as c:

        row = c.execute("""
            SELECT starosta_id
            FROM chats
            WHERE chat_id=?
        """, (
            message.chat.id,
        )).fetchone()

        if row and row["starosta_id"]:

            if row["starosta_id"] == message.from_user.id:

                await message.answer(
                    "👑 Ты уже являешься старостой."
                )

            else:

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
        f"👑 {message.from_user.full_name}, "
        "ты назначен старостой."
    )


# ============================================================
# ПЕРЕКЛИЧКА
# ============================================================

@dp.message(Command("rollcall"))
async def rollcall(
    message: Message,
    state: FSMContext
):

    ensure(message)

    if not is_starosta(message):

        await message.answer(
            "❌ Только староста может "
            "запускать перекличку."
        )

        return

    await message.answer(
        "📝 Напиши название предмета.\n\n"
        "Например:\n"
        "Физика"
    )

    await state.set_state(
        States.subject
    )


@dp.message(States.subject)
async def rollcall_subject(
    message: Message,
    state: FSMContext
):

    ensure(message)

    if not is_starosta(message):

        await state.clear()
        return

    subject = (
        message.text or ""
    ).strip()

    if not subject:

        await message.answer(
            "❌ Название предмета не может "
            "быть пустым."
        )

        return

    with db() as c:

        c.execute("""
            UPDATE rollcalls
            SET status='finished'
            WHERE chat_id=?
              AND status='active'
        """, (
            message.chat.id,
        ))

        c.execute("""
            INSERT INTO rollcalls(
                chat_id,
                subject,
                started_at,
                status
            )
            VALUES(
                ?, ?, ?, 'active'
            )
        """, (
            message.chat.id,
            subject,
            now().isoformat()
        ))

    await state.clear()

    await message.answer(
        "📋 ПЕРЕКЛИЧКА НАЧАТА!\n\n"
        f"📚 Предмет: {subject}\n"
        f"🕐 {now().strftime('%d.%m.%Y %H:%M')}\n\n"
        "Студенты, нажмите кнопку:",
        reply_markup=attendance_keyboard()
    )


# ============================================================
# ОТМЕТКА СТУДЕНТА
# ============================================================

@dp.callback_query(
    F.data.in_(["will", "wont"])
)
async def attendance_callback(
    callback: CallbackQuery
):

    # ВАЖНО:
    # Пользователя берём именно из callback.from_user.
    # callback.message может быть InaccessibleMessage,
    # поэтому нельзя использовать message.from_user.

    user = callback.from_user
    message = callback.message

    if message is None:

        await callback.answer(
            "❌ Сообщение недоступно.",
            show_alert=True
        )

        return

    chat = getattr(
        message,
        "chat",
        None
    )

    if chat is None:

        await callback.answer(
            "❌ Не удалось определить чат.",
            show_alert=True
        )

        return

    chat_id = chat.id

    chat_title = getattr(
        chat,
        "title",
        None
    ) or "Чат"

    # --------------------------------------------------------
    # Регистрируем пользователя напрямую
    # --------------------------------------------------------

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
            chat_id,
            chat_title
        ))

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
            chat_id,
            user.id,
            user.full_name,
            user.username,
            now().isoformat()
        ))

        # ----------------------------------------------------
        # Ищем активную перекличку
        # ----------------------------------------------------

        rollcall = c.execute("""
            SELECT *
            FROM rollcalls

            WHERE chat_id=?
              AND status='active'

            ORDER BY id DESC

            LIMIT 1
        """, (
            chat_id,
        )).fetchone()

        if not rollcall:

            await callback.answer(
                "Перекличка сейчас не проводится.",
                show_alert=True
            )

            return

        status = (
            "present"
            if callback.data == "will"
            else "absent"
        )

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
            VALUES(?, ?, ?, ?, ?)

            ON CONFLICT(
                rollcall_id,
                user_id
            )

            DO UPDATE SET
                status=excluded.status,
                name=excluded.name,
                answered_at=excluded.answered_at
        """, (
            rollcall["id"],
            user.id,
            user.full_name,
            status,
            now().isoformat()
        ))

    if status == "present":

        await callback.answer(
            "🟢 Ты отмечен!"
        )

    else:

        await callback.answer(
            "🔴 Ты отмечен как отсутствующий!"
        )


# ============================================================
# РЕЗУЛЬТАТЫ
# ============================================================

@dp.message(Command("attendance"))
async def attendance(message: Message):

    ensure(message)

    with db() as c:

        rollcall = c.execute("""
            SELECT *
            FROM rollcalls

            WHERE chat_id=?

            ORDER BY id DESC

            LIMIT 1
        """, (
            message.chat.id,
        )).fetchone()

        if not rollcall:

            await message.answer(
                "📊 Перекличек пока нет."
            )

            return

        rows = c.execute("""
            SELECT *
            FROM attendance

            WHERE rollcall_id=?

            ORDER BY name
        """, (
            rollcall["id"],
        )).fetchall()

    present = [
        x for x in rows
        if x["status"] == "present"
    ]

    absent = [
        x for x in rows
        if x["status"] == "absent"
    ]

    text = (
        "📊 РЕЗУЛЬТАТЫ ПЕРЕКЛИЧКИ\n\n"
        f"📚 {rollcall['subject']}\n"
        f"🕐 {rollcall['started_at']}\n\n"
        f"🟢 Присутствуют: {len(present)}\n"
        f"🔴 Отсутствуют: {len(absent)}\n\n"
    )

    if present:

        text += "🟢 ПРИСУТСТВУЮТ:\n"

        for student in present:

            text += (
                f"• {student['name']}\n"
            )

    if absent:

        text += "\n🔴 ОТСУТСТВУЮТ:\n"

        for student in absent:

            text += (
                f"• {student['name']}\n"
            )

    await message.answer(text)


# ============================================================
# ФИНИШ
# ============================================================

@dp.message(Command("finish"))
async def finish(message: Message):

    ensure(message)

    if not is_starosta(message):

        await message.answer(
            "❌ Только староста может "
            "завершить перекличку."
        )

        return

    with db() as c:

        rollcall = c.execute("""
            SELECT *
            FROM rollcalls

            WHERE chat_id=?
              AND status='active'

            ORDER BY id DESC

            LIMIT 1
        """, (
            message.chat.id,
        )).fetchone()

        if not rollcall:

            await message.answer(
                "❌ Активной переклички нет."
            )

            return

        c.execute("""
            UPDATE rollcalls

            SET
                status='finished',
                finished_at=?

            WHERE id=?
        """, (
            now().isoformat(),
            rollcall["id"]
        ))

        rows = c.execute("""
            SELECT status
            FROM attendance

            WHERE rollcall_id=?
        """, (
            rollcall["id"],
        )).fetchall()

    present = sum(
        1
        for x in rows
        if x["status"] == "present"
    )

    absent = sum(
        1
        for x in rows
        if x["status"] == "absent"
    )

    await message.answer(
        "🛑 ПЕРЕКЛИЧКА ЗАВЕРШЕНА!\n\n"
        f"📚 {rollcall['subject']}\n"
        f"🟢 Присутствуют: {present}\n"
        f"🔴 Отсутствуют: {absent}\n"
        f"🕐 {now().strftime('%d.%m.%Y %H:%M')}"
    )


# ============================================================
# ПОЛУЧЕНИЕ РАСПИСАНИЯ
# ============================================================

async def fetch_schedule_from_site():

    today = now().date()

    # Понедельник текущей недели
    date_start_obj = (
        today -
        timedelta(days=today.weekday())
    )

    # Воскресенье следующей недели
    date_end_obj = (
        date_start_obj +
        timedelta(days=13)
    )

    date_start = date_start_obj.strftime(
        "%Y-%m-%d"
    )

    date_end = date_end_obj.strftime(
        "%Y-%m-%d"
    )

    payload = {
        "group_id": GROUP_ID,
        "date_start": date_start,
        "date_end": date_end,
        "selected_lesson_type": "typical"
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0.0.0 "
            "Safari/537.36"
        ),
        "Referer": SCHEDULE_URL,
        "Accept": (
            "text/html,"
            "application/xhtml+xml,"
            "application/xml;q=0.9,"
            "*/*;q=0.8"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9"
    }

    timeout = aiohttp.ClientTimeout(
        total=90,
        connect=30
    )

    print("====================================")
    print("ЗАПРОС РАСПИСАНИЯ")
    print(f"Группа: {GROUP_ID}")
    print(f"Начало: {date_start}")
    print(f"Конец: {date_end}")
    print("====================================")

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers
    ) as session:

        async with session.post(
            SCHEDULE_URL,
            data=payload
        ) as response:

            print(
                "HTTP STATUS:",
                response.status
            )

            response.raise_for_status()

            html = await response.text()

            print(
                "РАЗМЕР ОТВЕТА:",
                len(html)
            )

            print(
                "ПЕРВЫЕ 500 СИМВОЛОВ:"
            )

            print(html[:500])

            return html


# ============================================================
# ОЧИСТКА ТЕКСТА
# ============================================================

def clean_text(text):

    if not text:
        return ""

    return re.sub(
        r"\s+",
        " ",
        text.replace("\xa0", " ")
    ).strip()


# ============================================================
# ДЕНЬ НЕДЕЛИ
# ============================================================

def russian_day_name(date_string):

    try:

        dt = datetime.strptime(
            date_string,
            "%Y-%m-%d"
        )

    except Exception:

        return ""

    days = {
        0: "Понедельник",
        1: "Вторник",
        2: "Среда",
        3: "Четверг",
        4: "Пятница",
        5: "Суббота",
        6: "Воскресенье"
    }

    return days.get(
        dt.weekday(),
        ""
    )


# ============================================================
# ДАТЫ
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

        text = clean_text(text)

        for pattern in patterns:

            for value in pattern.findall(text):

                if value not in dates:
                    dates.append(value)

    result = []

    for value in dates:

        try:

            if re.match(
                r"^\d{2}\.\d{2}\.\d{4}$",
                value
            ):

                dt = datetime.strptime(
                    value,
                    "%d.%m.%Y"
                )

            elif re.match(
                r"^\d{2}\.\d{2}\.\d{2}$",
                value
            ):

                dt = datetime.strptime(
                    value,
                    "%d.%m.%y"
                )

            else:

                dt = datetime.strptime(
                    value,
                    "%Y-%m-%d"
                )

            result.append(
                dt.strftime("%Y-%m-%d")
            )

        except ValueError:
            pass

    return result


# ============================================================
# ПАРСИНГ РАСПИСАНИЯ
# ============================================================

def parse_schedule_html(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    dates = extract_dates(soup)

    time_pattern = re.compile(
        r"\b"
        r"(\d{1,2}:\d{2})"
        r"\s*[-–—]\s*"
        r"(\d{1,2}:\d{2})"
        r"\b"
    )

    found = []

    for element in soup.find_all(
        string=time_pattern
    ):

        raw = clean_text(element)

        match = time_pattern.search(raw)

        if not match:
            continue

        start_time = match.group(1)
        end_time = match.group(2)

        container = element.parent

        for _ in range(8):

            if not container:
                break

            text = clean_text(
                container.get_text(
                    " ",
                    strip=True
                )
            )

            if (
                len(text) >= 20
                and len(text) <= 2000
            ):
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

        subject = ""

        important = container.find_all([
            "b",
            "strong",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5"
        ])

        for item in important:

            value = clean_text(
                item.get_text(
                    " ",
                    strip=True
                )
            )

            if not value:
                continue

            if time_pattern.search(value):
                continue

            if len(value) < 3:
                continue

            subject = value
            break

        if not subject:

            parts = [
                clean_text(x)
                for x in container.stripped_strings
            ]

            for part in parts:

                if not part:
                    continue

                if time_pattern.search(part):
                    continue

                if part.lower() in {
                    "лекция",
                    "практика",
                    "лабораторная",
                    "лабораторное занятие",
                    "семинар"
                }:
                    continue

                if len(part) >= 3:

                    subject = part
                    break

        if not subject:
            continue

        # ----------------------------------------------------
        # ПРЕПОДАВАТЕЛЬ
        # ----------------------------------------------------

        teacher = ""

        teacher_keywords = [
            "преп.",
            "преподаватель",
            "доц.",
            "проф.",
            "ст.пр.",
            "ассистент"
        ]

        parts = [
            clean_text(x)
            for x in container.stripped_strings
        ]

        for part in parts:

            lower = part.lower()

            if any(
                keyword in lower
                for keyword in teacher_keywords
            ):

                teacher = part
                break

        # ----------------------------------------------------
        # АУДИТОРИЯ
        # ----------------------------------------------------

        classroom = ""

        classroom_patterns = [

            r"к\.\s*[^,;]+,\s*ауд\.\s*[^,;]+",

            r"ауд\.\s*[\wА-Яа-яЁё./-]+",

            r"аудитория\s*[\wА-Яа-яЁё./-]+"
        ]

        for pattern in classroom_patterns:

            match_class = re.search(
                pattern,
                full_text,
                re.IGNORECASE
            )

            if match_class:

                classroom = clean_text(
                    match_class.group(0)
                )

                break

        # ----------------------------------------------------
        # ТИП
        # ----------------------------------------------------

        lesson_type = ""

        lower_text = full_text.lower()

        if "лаборатор" in lower_text:

            lesson_type = "Лабораторная"

        elif "практи" in lower_text:

            lesson_type = "Практика"

        elif "лекц" in lower_text:

            lesson_type = "Лекция"

        elif "семинар" in lower_text:

            lesson_type = "Семинар"

        # ----------------------------------------------------
        # ПОДГРУППА
        # ----------------------------------------------------

        subgroup = ""

        subgroup_match = re.search(
            r"([12])\s*(?:подгруппа|п/г)",
            full_text,
            re.IGNORECASE
        )

        if subgroup_match:

            subgroup = subgroup_match.group(1)

        found.append({

            "time":
                f"{start_time}-{end_time}",

            "subject":
                subject,

            "teacher":
                teacher,

            "classroom":
                classroom,

            "lesson_type":
                lesson_type,

            "subgroup":
                subgroup
        })

    # --------------------------------------------------------
    # УДАЛЕНИЕ ДУБЛИКАТОВ
    # --------------------------------------------------------

    unique = []

    seen = set()

    for lesson in found:

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

        unique.append(lesson)

    result = []

    # --------------------------------------------------------
    # ПРИВЯЗКА ДАТ
    # --------------------------------------------------------

    if dates:

        for index, lesson in enumerate(unique):

            item = dict(lesson)

            item["date"] = dates[
                index % len(dates)
            ]

            item["day_name"] = russian_day_name(
                item["date"]
            )

            result.append(item)

    else:

        for lesson in unique:

            item = dict(lesson)

            item["date"] = ""
            item["day_name"] = ""

            result.append(item)

    return result


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

                ON CONFLICT(
                    chat_id,
                    lesson_date,
                    lesson_time,
                    subject,
                    subgroup
                )

                DO UPDATE SET

                    day_name=excluded.day_name,
                    teacher=excluded.teacher,
                    classroom=excluded.classroom,
                    lesson_type=excluded.lesson_type,
                    source=excluded.source,
                    updated_at=excluded.updated_at

            """, (

                chat_id,

                lesson_date,

                lesson.get(
                    "day_name",
                    ""
                ),

                lesson.get(
                    "time",
                    ""
                ),

                lesson.get(
                    "subject",
                    ""
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

                now().isoformat()
            ))

            saved += 1

    return saved


# ============================================================
# ОБНОВЛЕНИЕ РАСПИСАНИЯ
# ============================================================

async def update_schedule(chat_id):

    html = await fetch_schedule_from_site()

    if not html:

        raise RuntimeError(
            "Сайт вернул пустой ответ"
        )

    lessons = parse_schedule_html(html)

    if not lessons:

        return 0, len(html)

    saved = save_remote_schedule(
        chat_id,
        lessons
    )

    return saved, len(html)


# ============================================================
# ОБНОВИТЬ РАСПИСАНИЕ
# ============================================================

@dp.message(Command("refresh_schedule"))
async def refresh_schedule(
    message: Message
):

    ensure(message)

    await message.answer(
        "🔄 Загружаю расписание с сайта...\n\n"
        f"🏫 Институт: {INSTITUTE}\n"
        f"🎓 Курс: {COURSE}\n"
        f"👥 Группа: {GROUP_NAME}\n"
        f"🆔 ID: {GROUP_ID}"
    )

    try:

        saved, response_size = await update_schedule(
            message.chat.id
        )

        if saved == 0:

            await message.answer(
                "⚠️ Сайт ответил, но бот не смог "
                "распознать расписание.\n\n"
                f"Размер ответа сайта: "
                f"{response_size} символов."
            )

            return

        await message.answer(
            "✅ РАСПИСАНИЕ ОБНОВЛЕНО!\n\n"
            f"📚 Сохранено занятий: {saved}\n\n"
            "Теперь напиши:\n"
            "расписание\n"
            "или\n"
            "сегодня"
        )

    except aiohttp.ClientResponseError as e:

        await message.answer(
            "❌ Сайт отклонил запрос.\n\n"
            f"HTTP-код: {e.status}\n"
            f"{e.message or ''}"
        )

    except asyncio.TimeoutError:

        await message.answer(
            "❌ Сайт не ответил вовремя.\n\n"
            "Сервер расписания слишком долго "
            "отвечает на запрос."
        )

    except aiohttp.ClientError as e:

        await message.answer(
            "❌ Ошибка соединения с сайтом.\n\n"
            f"{type(e).__name__}: {str(e)}"
        )

    except Exception as e:

        await message.answer(
            "❌ Не удалось получить расписание.\n\n"
            f"{type(e).__name__}: {str(e)}"
        )


# ============================================================
# ФОРМАТ РАСПИСАНИЯ
# ============================================================

def format_schedule(rows, title):

    if not rows:

        return (
            f"{title}\n\n"
            "📭 Занятий не найдено."
        )

    text = (
        f"{title}\n\n"
        f"🏫 {INSTITUTE}\n"
        f"🎓 {COURSE} курс\n"
        f"👥 {GROUP_NAME}\n"
    )

    current_date = None

    for row in rows:

        lesson_date = row["lesson_date"]

        if lesson_date != current_date:

            current_date = lesson_date

            try:

                dt = datetime.strptime(
                    lesson_date,
                    "%Y-%m-%d"
                )

                text += (
                    f"\n📅 "
                    f"{russian_day_name(lesson_date)}, "
                    f"{dt.strftime('%d.%m.%Y')}\n"
                )

            except Exception:

                text += (
                    f"\n📅 {lesson_date}\n"
                )

        text += (
            f"\n🕐 {row['lesson_time']}\n"
            f"📚 {row['subject']}\n"
        )

        if row["teacher"]:

            text += (
                f"👨‍🏫 {row['teacher']}\n"
            )

        if row["classroom"]:

            text += (
                f"🏫 {row['classroom']}\n"
            )

        if row["lesson_type"]:

            text += (
                f"📖 {row['lesson_type']}\n"
            )

        if row["subgroup"]:

            text += (
                f"👥 {row['subgroup']} подгруппа\n"
            )

    return text


# ============================================================
# РАСПИСАНИЕ
# ============================================================

@dp.message(Command("schedule"))
async def schedule(message: Message):

    ensure(message)

    with db() as c:

        count = c.execute("""
            SELECT COUNT(*)
            FROM remote_schedule
            WHERE chat_id=?
        """, (
            message.chat.id,
        )).fetchone()[0]

    if count == 0:

        await message.answer(
            "🔄 Расписание ещё не загружено.\n\n"
            "Получаю его с сайта..."
        )

        try:

            saved, response_size = await update_schedule(
                message.chat.id
            )

            if saved == 0:

                await message.answer(
                    "⚠️ Бот получил ответ от сайта, "
                    "но не смог распознать расписание.\n\n"
                    f"Размер ответа: {response_size} символов."
                )

                return

        except Exception as e:

            await message.answer(
                "❌ Не удалось загрузить расписание.\n\n"
                f"{type(e).__name__}: {str(e)}"
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

    today = now().date()

    limit_date = today + timedelta(days=14)

    filtered = []

    for row in rows:

        try:

            lesson_date = datetime.strptime(
                row["lesson_date"],
                "%Y-%m-%d"
            ).date()

            if today <= lesson_date <= limit_date:

                filtered.append(row)

        except Exception:
            pass

    await message.answer(
        format_schedule(
            filtered,
            "🗓 РАСПИСАНИЕ"
        )
    )


# ============================================================
# СЕГОДНЯ
# ============================================================

@dp.message(Command("today"))
async def today_schedule(
    message: Message
):

    ensure(message)

    today_value = now().strftime(
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
            message.chat.id,
            today_value
        )).fetchall()

    if not rows:

        try:

            await update_schedule(
                message.chat.id
            )

        except Exception:
            pass

        with db() as c:

            rows = c.execute("""
                SELECT *
                FROM remote_schedule

                WHERE chat_id=?
                  AND lesson_date=?

                ORDER BY lesson_time
            """, (
                message.chat.id,
                today_value
            )).fetchall()

    await message.answer(
        format_schedule(
            rows,
            "📌 ПАРЫ СЕГОДНЯ"
        )
    )


# ============================================================
# ДОБАВИТЬ ПАРУ
# ============================================================

@dp.message(Command("add_lesson"))
async def add_lesson(
    message: Message,
    state: FSMContext
):

    ensure(message)

    if not is_starosta(message):

        await message.answer(
            "❌ Только староста может "
            "добавлять пары."
        )

        return

    await state.set_state(
        States.schedule_subject
    )

    await message.answer(
        "📚 Введи название предмета:"
    )


@dp.message(States.schedule_subject)
async def add_lesson_subject(
    message: Message,
    state: FSMContext
):

    await state.update_data(
        subject=(
            message.text or ""
        ).strip()
    )

    await state.set_state(
        States.schedule_day
    )

    await message.answer(
        "📅 Введи день недели:\n\n"
        "1 — Понедельник\n"
        "2 — Вторник\n"
        "3 — Среда\n"
        "4 — Четверг\n"
        "5 — Пятница\n"
        "6 — Суббота\n"
        "7 — Воскресенье"
    )


@dp.message(States.schedule_day)
async def add_lesson_day(
    message: Message,
    state: FSMContext
):

    value = (
        message.text or ""
    ).strip()

    if (
        not value.isdigit()
        or not 1 <= int(value) <= 7
    ):

        await message.answer(
            "❌ Введи число от 1 до 7."
        )

        return

    await state.update_data(
        day=int(value)
    )

    await state.set_state(
        States.schedule_time
    )

    await message.answer(
        "🕐 Введи время пары.\n\n"
        "Например:\n"
        "08:00-09:30"
    )


@dp.message(States.schedule_time)
async def add_lesson_time(
    message: Message,
    state: FSMContext
):

    value = (
        message.text or ""
    ).strip()

    if not re.match(
        r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$",
        value
    ):

        await message.answer(
            "❌ Неверный формат.\n\n"
            "Пример:\n"
            "08:00-09:30"
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
            VALUES(?, ?, ?, ?)
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
        f"🕐 {value}"
    )


# ============================================================
# УДАЛИТЬ ПАРУ
# ============================================================

@dp.message(Command("delete_lesson"))
async def delete_lesson(
    message: Message
):

    ensure(message)

    if not is_starosta(message):

        await message.answer(
            "❌ Только староста может "
            "удалять пары."
        )

        return

    with db() as c:

        rows = c.execute("""
            SELECT *
            FROM schedule

            WHERE chat_id=?

            ORDER BY day, lesson_time
        """, (
            message.chat.id,
        )).fetchall()

    if not rows:

        await message.answer(
            "📭 Ручных пар пока нет."
        )

        return

    text = "🗑 СОХРАНЁННЫЕ ПАРЫ\n\n"

    for row in rows:

        text += (
            f"ID: {row['id']}\n"
            f"📚 {row['subject']}\n"
            f"🕐 {row['lesson_time']}\n\n"
        )

    text += (
        "Чтобы удалить пару:\n"
        "/delete_1\n\n"
        "Например:\n"
        "/delete_5"
    )

    await message.answer(text)


@dp.message(
    F.text.regexp(
        r"^/delete_\d+$"
    )
)
async def delete_lesson_by_id(
    message: Message
):

    ensure(message)

    if not is_starosta(message):

        await message.answer(
            "❌ Только староста может "
            "удалять пары."
        )

        return

    lesson_id = int(
        message.text.split("_")[1]
    )

    with db() as c:

        row = c.execute("""
            SELECT *
            FROM schedule

            WHERE id=?
              AND chat_id=?
        """, (
            lesson_id,
            message.chat.id
        )).fetchone()

        if not row:

            await message.answer(
                "❌ Пара не найдена."
            )

            return

        c.execute("""
            DELETE FROM schedule

            WHERE id=?
              AND chat_id=?
        """, (
            lesson_id,
            message.chat.id
        ))

    await message.answer(
        "🗑 ПАРА УДАЛЕНА!\n\n"
        f"📚 {row['subject']}\n"
        f"🕐 {row['lesson_time']}"
    )


# ============================================================
# МОЁ РАСПИСАНИЕ
# ============================================================

@dp.message(Command("my_schedule"))
async def my_schedule(
    message: Message
):

    ensure(message)

    with db() as c:

        rows = c.execute("""
            SELECT *
            FROM schedule

            WHERE chat_id=?

            ORDER BY day, lesson_time
        """, (
            message.chat.id,
        )).fetchall()

    if not rows:

        await message.answer(
            "📭 Ручных пар нет."
        )

        return

    days = {
        1: "Понедельник",
        2: "Вторник",
        3: "Среда",
        4: "Четверг",
        5: "Пятница",
        6: "Суббота",
        7: "Воскресенье"
    }

    text = "📅 СОХРАНЁННЫЕ ПАРЫ\n\n"

    current_day = None

    for row in rows:

        if row["day"] != current_day:

            current_day = row["day"]

            text += (
                f"\n📌 "
                f"{days.get(row['day'], '')}\n"
            )

        text += (
            f"• {row['lesson_time']} — "
            f"{row['subject']}\n"
        )

    await message.answer(text)


# ============================================================
# СТУДЕНТЫ
# ============================================================

@dp.message(Command("students"))
async def students(
    message: Message
):

    ensure(message)

    if not is_starosta(message):

        await message.answer(
            "❌ Только староста может "
            "смотреть список студентов."
        )

        return

    with db() as c:

        rows = c.execute("""
            SELECT *
            FROM students

            WHERE chat_id=?

            ORDER BY name
        """, (
            message.chat.id,
        )).fetchall()

    if not rows:

        await message.answer(
            "📭 Студенты пока "
            "не зарегистрированы."
        )

        return

    text = (
        f"👥 СТУДЕНТЫ\n\n"
        f"Всего: {len(rows)}\n\n"
    )

    for index, row in enumerate(rows, 1):

        username = (
            f" @{row['username']}"
            if row["username"]
            else ""
        )

        text += (
            f"{index}. "
            f"{row['name']}"
            f"{username}\n"
        )

    await message.answer(text)


# ============================================================
# ИСТОРИЯ
# ============================================================

@dp.message(Command("history"))
async def history(
    message: Message
):

    ensure(message)

    if not is_starosta(message):

        await message.answer(
            "❌ Только староста может "
            "смотреть историю."
        )

        return

    with db() as c:

        rows = c.execute("""
            SELECT *
            FROM rollcalls

            WHERE chat_id=?

            ORDER BY id DESC

            LIMIT 20
        """, (
            message.chat.id,
        )).fetchall()

    if not rows:

        await message.answer(
            "📭 История перекличек пуста."
        )

        return

    text = "📚 ИСТОРИЯ ПЕРЕКЛИЧЕК\n\n"

    for row in rows:

        with db() as c:

            stats = c.execute("""
                SELECT

                    SUM(
                        CASE
                            WHEN status='present'
                            THEN 1
                            ELSE 0
                        END
                    ) AS present,

                    SUM(
                        CASE
                            WHEN status='absent'
                            THEN 1
                            ELSE 0
                        END
                    ) AS absent

                FROM attendance

                WHERE rollcall_id=?
            """, (
                row["id"],
            )).fetchone()

        present = stats["present"] or 0
        absent = stats["absent"] or 0

        text += (
            f"📅 {row['started_at']}\n"
            f"📚 {row['subject']}\n"
            f"🟢 {present} | "
            f"🔴 {absent}\n"
            f"Статус: {row['status']}\n\n"
        )

    await message.answer(text)


# ============================================================
# СТАТИСТИКА
# ============================================================

@dp.message(Command("stats"))
async def stats(
    message: Message
):

    ensure(message)

    with db() as c:

        students_count = c.execute("""
            SELECT COUNT(*)
            FROM students

            WHERE chat_id=?
        """, (
            message.chat.id,
        )).fetchone()[0]

        total = c.execute("""
            SELECT COUNT(*)
            FROM attendance a

            JOIN rollcalls r
                ON r.id=a.rollcall_id

            WHERE r.chat_id=?
        """, (
            message.chat.id,
        )).fetchone()[0]

        present = c.execute("""
            SELECT COUNT(*)
            FROM attendance a

            JOIN rollcalls r
                ON r.id=a.rollcall_id

            WHERE r.chat_id=?
              AND a.status='present'
        """, (
            message.chat.id,
        )).fetchone()[0]

        absent = c.execute("""
            SELECT COUNT(*)
            FROM attendance a

            JOIN rollcalls r
                ON r.id=a.rollcall_id

            WHERE r.chat_id=?
              AND a.status='absent'
        """, (
            message.chat.id,
        )).fetchone()[0]

        rollcalls = c.execute("""
            SELECT COUNT(*)
            FROM rollcalls

            WHERE chat_id=?
        """, (
            message.chat.id,
        )).fetchone()[0]

    percent = (
        (present / total) * 100
        if total
        else 0
    )

    await message.answer(
        "📊 СТАТИСТИКА\n\n"
        f"👥 Студентов: {students_count}\n"
        f"📝 Перекличек: {rollcalls}\n"
        f"📌 Всего отметок: {total}\n"
        f"🟢 Присутствий: {present}\n"
        f"🔴 Отсутствий: {absent}\n"
        f"📈 Посещаемость: {percent:.1f}%"
    )


# ============================================================
# РУССКИЕ КОМАНДЫ
# ============================================================

@dp.message(
    F.text.casefold() == "расписание"
)
async def text_schedule(
    message: Message
):

    await schedule(message)


@dp.message(
    F.text.casefold() == "сегодня"
)
async def text_today(
    message: Message
):

    await today_schedule(message)


@dp.message(
    F.text.casefold() == "обновить расписание"
)
async def text_refresh(
    message: Message
):

    await refresh_schedule(message)


@dp.message(
    F.text.casefold() == "начать перекличку"
)
async def text_rollcall(
    message: Message,
    state: FSMContext
):

    await rollcall(
        message,
        state
    )


@dp.message(
    F.text.casefold() == "результаты"
)
async def text_attendance(
    message: Message
):

    await attendance(message)


@dp.message(
    F.text.casefold() == "финиш"
)
async def text_finish(
    message: Message
):

    await finish(message)


@dp.message(
    F.text.casefold() == "сет староста"
)
async def text_starosta(
    message: Message
):

    await set_starosta(message)


@dp.message(
    F.text.casefold() == "студенты"
)
async def text_students(
    message: Message
):

    await students(message)


@dp.message(
    F.text.casefold() == "история"
)
async def text_history(
    message: Message
):

    await history(message)


@dp.message(
    F.text.casefold() == "статистика"
)
async def text_stats(
    message: Message
):

    await stats(message)


@dp.message(
    F.text.casefold() == "добавить пару"
)
async def text_add_lesson(
    message: Message,
    state: FSMContext
):

    await add_lesson(
        message,
        state
    )


@dp.message(
    F.text.casefold() == "удалить пару"
)
async def text_delete_lesson(
    message: Message
):

    await delete_lesson(message)


@dp.message(
    F.text.casefold() == "мое расписание"
)
async def text_my_schedule(
    message: Message
):

    await my_schedule(message)


# ============================================================
# ЗАПУСК
# ============================================================

async def main():

    init_db()

    print(
        "===================================="
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
        f"Group ID: {GROUP_ID}"
    )

    print(
        "===================================="
    )

    await dp.start_polling(bot)


if __name__ == "__main__":

    asyncio.run(main())
