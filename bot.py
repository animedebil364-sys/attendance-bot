import os
import asyncio
import sqlite3
import re
from datetime import datetime, date, timedelta
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
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

TZ = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))
DB_PATH = os.getenv("DB_PATH", "attendance.db")

SCHEDULE_URL = "https://tt2.vogu35.ru/"
GROUP_ID = "543"

# ИСИ, 1 курс, 1Б08 №12
INSTITUTE = "ИСИ"
COURSE = "1"
GROUP_NAME = "1Б08 №12"

bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ============================================================
# СОСТОЯНИЯ
# ============================================================

class States(StatesGroup):
    subject = State()

    schedule_subject = State()
    schedule_day = State()
    schedule_time = State()


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def now():
    return datetime.now(TZ)


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
            INSERT INTO chats(chat_id, title)
            VALUES(?, ?)
            ON CONFLICT(chat_id)
            DO UPDATE SET title=excluded.title
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
# СТАРОСТА
# ============================================================

def is_starosta(message: Message):
    with db() as c:
        row = c.execute("""
            SELECT starosta_id
            FROM chats
            WHERE chat_id=?
        """, (message.chat.id,)).fetchone()

    if not row:
        return False

    return row["starosta_id"] == message.from_user.id


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
# /START
# ============================================================

@dp.message(Command("start"))
async def start(message: Message):
    ensure(message)

    await message.answer(
        "👋 Привет!\n\n"
        "Я бот для учёта посещаемости группы.\n\n"

        "👑 Староста:\n"
        "/set_starosta — назначить себя старостой\n\n"

        "📝 Перекличка:\n"
        "/rollcall — начать перекличку\n"
        "/attendance — результаты\n"
        "/finish — завершить перекличку\n\n"

        "📅 Расписание:\n"
        "/schedule — расписание\n"
        "/today — пары сегодня\n"
        "/refresh_schedule — обновить расписание с сайта\n\n"

        "➕ Свои пары:\n"
        "/add_lesson — добавить пару\n"
        "/delete_lesson — удалить пару\n"
        "/my_schedule — показать сохранённые пары\n\n"

        "📊 База:\n"
        "/students — список студентов\n"
        "/history — история перекличек\n"
        "/stats — статистика посещаемости"
    )


# ============================================================
# НАЗНАЧЕНИЕ СТАРОСТЫ
# ============================================================

@dp.message(Command("set_starosta"))
async def set_starosta(message: Message):
    ensure(message)

    with db() as c:
        row = c.execute("""
            SELECT starosta_id
            FROM chats
            WHERE chat_id=?
        """, (message.chat.id,)).fetchone()

        if row and row["starosta_id"]:
            if row["starosta_id"] == message.from_user.id:
                await message.answer("👑 Ты уже являешься старостой.")
            else:
                await message.answer("❌ Староста уже назначен.")
            return

        c.execute("""
            UPDATE chats
            SET starosta_id=?,
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
# НАЧАТЬ ПЕРЕКЛИЧКУ
# ============================================================

@dp.message(Command("rollcall"))
async def rollcall(message: Message, state: FSMContext):
    ensure(message)

    if not is_starosta(message):
        await message.answer(
            "❌ Только староста может запускать перекличку."
        )
        return

    await message.answer(
        "📝 Напиши название предмета.\n\n"
        "Например:\n"
        "Физика"
    )

    await state.set_state(States.subject)


@dp.message(States.subject)
async def rollcall_subject(message: Message, state: FSMContext):
    ensure(message)

    if not is_starosta(message):
        await state.clear()
        return

    subject = message.text.strip()

    if not subject:
        await message.answer("❌ Название предмета не может быть пустым.")
        return

    with db() as c:
        c.execute("""
            UPDATE rollcalls
            SET status='finished'
            WHERE chat_id=? AND status='active'
        """, (message.chat.id,))

        c.execute("""
            INSERT INTO rollcalls(
                chat_id,
                subject,
                started_at,
                status
            )
            VALUES(?, ?, ?, 'active')
        """, (
            message.chat.id,
            subject,
            now().isoformat()
        ))

        rollcall_id = c.lastrowid

    await state.clear()

    await message.answer(
        f"📋 Перекличка начата!\n\n"
        f"📚 Предмет: {subject}\n"
        f"🕐 {now().strftime('%d.%m.%Y %H:%M')}\n\n"
        "Студенты, нажмите кнопку ниже:",
        reply_markup=attendance_keyboard()
    )


# ============================================================
# ОТМЕТКА СТУДЕНТА
# ============================================================

@dp.callback_query(F.data.in_(["will", "wont"]))
async def attendance_callback(callback: CallbackQuery):
    message = callback.message
    user = callback.from_user

    if not message:
        return

    ensure(message)

    with db() as c:
        rollcall = c.execute("""
            SELECT *
            FROM rollcalls
            WHERE chat_id=? AND status='active'
            ORDER BY id DESC
            LIMIT 1
        """, (message.chat.id,)).fetchone()

        if not rollcall:
            await callback.answer(
                "Перекличка сейчас не проводится.",
                show_alert=True
            )
            return

        status = "present" if callback.data == "will" else "absent"

        c.execute("""
            INSERT INTO attendance(
                rollcall_id,
                user_id,
                name,
                status,
                answered_at
            )
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(rollcall_id, user_id)
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
        await callback.answer("🟢 Ты отмечен как присутствующий!")
    else:
        await callback.answer("🔴 Ты отмечен как отсутствующий!")


# ============================================================
# РЕЗУЛЬТАТЫ ПЕРЕКЛИЧКИ
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
        """, (message.chat.id,)).fetchone()

        if not rollcall:
            await message.answer("📊 Перекличек пока нет.")
            return

        rows = c.execute("""
            SELECT *
            FROM attendance
            WHERE rollcall_id=?
            ORDER BY name
        """, (rollcall["id"],)).fetchall()

    present = [x for x in rows if x["status"] == "present"]
    absent = [x for x in rows if x["status"] == "absent"]

    text = (
        f"📊 Результаты переклички\n\n"
        f"📚 {rollcall['subject']}\n"
        f"🕐 {rollcall['started_at']}\n\n"
        f"🟢 Присутствуют: {len(present)}\n"
        f"🔴 Отсутствуют: {len(absent)}\n\n"
    )

    if present:
        text += "🟢 Присутствуют:\n"
        for student in present:
            text += f"• {student['name']}\n"

    if absent:
        text += "\n🔴 Отсутствуют:\n"
        for student in absent:
            text += f"• {student['name']}\n"

    await message.answer(text)


# ============================================================
# ЗАВЕРШИТЬ ПЕРЕКЛИЧКУ
# ============================================================

@dp.message(Command("finish"))
async def finish(message: Message):
    ensure(message)

    if not is_starosta(message):
        await message.answer(
            "❌ Только староста может завершить перекличку."
        )
        return

    with db() as c:
        rollcall = c.execute("""
            SELECT *
            FROM rollcalls
            WHERE chat_id=? AND status='active'
            ORDER BY id DESC
            LIMIT 1
        """, (message.chat.id,)).fetchone()

        if not rollcall:
            await message.answer("❌ Активной переклички нет.")
            return

        c.execute("""
            UPDATE rollcalls
            SET status='finished',
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
        """, (rollcall["id"],)).fetchall()

    present = sum(1 for x in rows if x["status"] == "present")
    absent = sum(1 for x in rows if x["status"] == "absent")

    await message.answer(
        f"🛑 Перекличка завершена.\n\n"
        f"📚 {rollcall['subject']}\n"
        f"🟢 Присутствуют: {present}\n"
        f"🔴 Отсутствуют: {absent}\n"
        f"🕐 Завершено: {now().strftime('%d.%m.%Y %H:%M')}"
    )


# ============================================================
# ПАРСИНГ РАСПИСАНИЯ С САЙТА
# ============================================================

async def fetch_schedule_from_site():
    """
    Получает HTML расписания с сайта.
    Для группы 1Б08 №12 используется group_id=543.
    """

    payload = {
        "group_id": GROUP_ID,
        "date_start": now().strftime("%Y-%m-%d"),
        "date_end": (now() + timedelta(days=120)).strftime("%Y-%m-%d"),
        "selected_lesson_type": "typical"
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/140 Safari/537.36"
        ),
        "Referer": SCHEDULE_URL,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers
    ) as session:

        async with session.post(
            SCHEDULE_URL,
            data=payload
        ) as response:

            if response.status != 200:
                raise RuntimeError(
                    f"Сайт вернул HTTP {response.status}"
                )

            html = await response.text()

    return html


def clean_text(text):
    if not text:
        return ""

    return re.sub(
        r"\s+",
        " ",
        text.replace("\xa0", " ")
    ).strip()


def parse_schedule_html(html):
    """
    Пытается извлечь занятия из HTML страницы.
    """

    soup = BeautifulSoup(html, "html.parser")

    lessons = []

    # --------------------------------------------------------
    # Ищем элементы с временем.
    # На сайте время имеет формат:
    # 08:00 - 09:30
    # --------------------------------------------------------

    time_pattern = re.compile(
        r"\b(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})\b"
    )

    for element in soup.find_all(
        string=time_pattern
    ):

        text = clean_text(element)

        match = time_pattern.search(text)

        if not match:
            continue

        start_time = match.group(1)
        end_time = match.group(2)

        parent = element.parent

        # Берём разумный контейнер занятия
        container = parent

        for _ in range(5):
            if container and len(
                clean_text(container.get_text(" ", strip=True))
            ) > 100:
                break

            if container and container.parent:
                container = container.parent

        if not container:
            continue

        full_text = clean_text(
            container.get_text(" ", strip=True)
        )

        # ----------------------------------------------------
        # Убираем время из текста
        # ----------------------------------------------------

        remaining = time_pattern.sub(
            "",
            full_text,
            count=1
        ).strip()

        if len(remaining) < 3:
            continue

        # ----------------------------------------------------
        # Попытка определить предмет.
        # Обычно он находится отдельным жирным элементом.
        # ----------------------------------------------------

        subject = ""

        bolds = container.find_all(
            ["b", "strong", "h1", "h2", "h3", "h4"]
        )

        for b in bolds:
            value = clean_text(
                b.get_text(" ", strip=True)
            )

            if not value:
                continue

            if time_pattern.search(value):
                continue

            if len(value) > 2:
                subject = value
                break

        if not subject:
            # Берём первую строку после времени
            parts = [
                clean_text(x)
                for x in container.stripped_strings
            ]

            for part in parts:
                if not part:
                    continue

                if time_pattern.search(part):
                    continue

                if len(part) >= 3:
                    subject = part
                    break

        if not subject:
            continue

        # ----------------------------------------------------
        # Преподаватель
        # ----------------------------------------------------

        teacher = ""

        teacher_patterns = [
            r"(ст\.пр\.,?\s*.+?)(?=к\.|ауд\.|Практика|Лекция|$)",
            r"(доц\.,?\s*.+?)(?=к\.|ауд\.|Практика|Лекция|$)",
            r"(проф\.,?\s*.+?)(?=к\.|ауд\.|Практика|Лекция|$)",
            r"(зав\.каф\.,?\s*.+?)(?=к\.|ауд\.|Практика|Лекция|$)"
        ]

        for pattern in teacher_patterns:
            m = re.search(
                pattern,
                full_text,
                re.IGNORECASE
            )

            if m:
                teacher = clean_text(m.group(1))
                break

        # ----------------------------------------------------
        # Аудитория
        # ----------------------------------------------------

        classroom = ""

        classroom_match = re.search(
            r"(к\.\s*[^,]+,\s*ауд\.\s*[^,]+)",
            full_text,
            re.IGNORECASE
        )

        if classroom_match:
            classroom = clean_text(
                classroom_match.group(1)
            )

        # ----------------------------------------------------
        # Тип занятия
        # ----------------------------------------------------

        lesson_type = ""

        if "Практика" in full_text:
            lesson_type = "Практика"
        elif "Лабораторная" in full_text:
            lesson_type = "Лабораторная"
        elif "Лекция" in full_text:
            lesson_type = "Лекция"

        # ----------------------------------------------------
        # Подгруппа
        # ----------------------------------------------------

        subgroup = ""

        subgroup_match = re.search(
            r"([12])\s*подгруппа",
            full_text,
            re.IGNORECASE
        )

        if subgroup_match:
            subgroup = subgroup_match.group(1)

        lessons.append({
            "time": f"{start_time}-{end_time}",
            "subject": subject,
            "teacher": teacher,
            "classroom": classroom,
            "lesson_type": lesson_type,
            "subgroup": subgroup
        })

    return lessons


# ============================================================
# СОХРАНЕНИЕ РАСПИСАНИЯ
# ============================================================

def save_remote_schedule(chat_id, lessons):
    with db() as c:

        for lesson in lessons:

            lesson_date = lesson.get("date", "")

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
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

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
                lesson.get("day_name", ""),
                lesson.get("time", ""),
                lesson.get("subject", ""),
                lesson.get("teacher", ""),
                lesson.get("classroom", ""),
                lesson.get("lesson_type", ""),
                lesson.get("subgroup", ""),
                SCHEDULE_URL,
                now().isoformat()
            ))


# ============================================================
# ОБНОВЛЕНИЕ РАСПИСАНИЯ
# ============================================================

async def update_schedule(chat_id):
    html = await fetch_schedule_from_site()

    lessons = parse_schedule_html(html)

    if not lessons:
        return 0

    # --------------------------------------------------------
    # Если сайт отдаёт даты в элементах,
    # пытаемся распределить найденные пары по датам.
    # --------------------------------------------------------

    soup = BeautifulSoup(html, "html.parser")

    date_pattern = re.compile(
        r"\b(\d{2}\.\d{2}\.\d{4})\b"
    )

    dates = []

    for text in soup.stripped_strings:
        text = clean_text(text)

        for match in date_pattern.finditer(text):
            dates.append(match.group(1))

    # Убираем дубли
    unique_dates = []

    for d in dates:
        if d not in unique_dates:
            unique_dates.append(d)

    # --------------------------------------------------------
    # Если даты найдены, связываем занятия с ними.
    # --------------------------------------------------------

    enriched = []

    if unique_dates:

        for index, lesson in enumerate(lessons):

            # Если на странице несколько недель,
            # распределяем найденные даты циклически.
            d = unique_dates[index % len(unique_dates)]

            try:
                parsed = datetime.strptime(
                    d,
                    "%d.%m.%Y"
                )

                iso_date = parsed.strftime("%Y-%m-%d")
                day_name = parsed.strftime("%A")

                russian_days = {
                    "Monday": "Понедельник",
                    "Tuesday": "Вторник",
                    "Wednesday": "Среда",
                    "Thursday": "Четверг",
                    "Friday": "Пятница",
                    "Saturday": "Суббота",
                    "Sunday": "Воскресенье"
                }

                day_name = russian_days.get(
                    day_name,
                    day_name
                )

            except Exception:
                iso_date = ""
                day_name = ""

            item = dict(lesson)
            item["date"] = iso_date
            item["day_name"] = day_name

            enriched.append(item)

    else:
        # Если даты определить не получилось,
        # сохраняем найденные занятия с пустой датой.
        enriched = lessons

    # --------------------------------------------------------
    # Сохраняем
    # --------------------------------------------------------

    save_remote_schedule(
        chat_id,
        enriched
    )

    return len(enriched)


# ============================================================
# КОМАНДА ОБНОВИТЬ РАСПИСАНИЕ
# ============================================================

@dp.message(Command("refresh_schedule"))
async def refresh_schedule(message: Message):
    ensure(message)

    await message.answer(
        "🔄 Загружаю расписание с сайта ВоГУ...\n\n"
        f"Группа: {INSTITUTE}, {COURSE} курс, {GROUP_NAME}"
    )

    try:
        count = await update_schedule(
            message.chat.id
        )

        if count == 0:
            await message.answer(
                "⚠️ Сайт ответил, но бот не смог найти "
                "занятия в полученном расписании.\n\n"
                "Проверь, что на сайте выбраны:\n"
                "ИСИ → 1 курс → 1Б08 №12."
            )
            return

        await message.answer(
            "✅ Расписание обновлено!\n\n"
            f"📚 Группа: {INSTITUTE}, {COURSE} курс, {GROUP_NAME}\n"
            f"📅 Найдено записей: {count}\n\n"
            "Теперь используй:\n"
            "🗓 /schedule\n"
            "📌 /today"
        )

    except Exception as e:
        await message.answer(
            "❌ Не удалось получить расписание с сайта.\n\n"
            f"Ошибка: {str(e)[:500]}"
        )


# ============================================================
# ФОРМАТИРОВАНИЕ РАСПИСАНИЯ
# ============================================================

def format_schedule(rows, title):
    if not rows:
        return (
            f"{title}\n\n"
            "📭 Занятий не найдено."
        )

    text = f"{title}\n\n"

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

                russian_days = {
                    0: "Понедельник",
                    1: "Вторник",
                    2: "Среда",
                    3: "Четверг",
                    4: "Пятница",
                    5: "Суббота",
                    6: "Воскресенье"
                }

                text += (
                    f"\n📅 {russian_days[dt.weekday()]}, "
                    f"{dt.strftime('%d.%m.%Y')}\n"
                )

            except Exception:
                text += f"\n📅 {lesson_date}\n"

        text += (
            f"\n🕐 {row['lesson_time']}\n"
            f"📚 {row['subject']}\n"
        )

        if row["teacher"]:
            text += f"👨‍🏫 {row['teacher']}\n"

        if row["classroom"]:
            text += f"🏫 {row['classroom']}\n"

        if row["lesson_type"]:
            text += f"📖 {row['lesson_type']}\n"

        if row["subgroup"]:
            text += f"👥 {row['subgroup']} подгруппа\n"

    return text


# ============================================================
# РАСПИСАНИЕ
# ============================================================

@dp.message(Command("schedule"))
async def schedule(message: Message):
    ensure(message)

    # Если данных нет — сначала пробуем обновить
    with db() as c:
        count = c.execute("""
            SELECT COUNT(*)
            FROM remote_schedule
            WHERE chat_id=?
        """, (message.chat.id,)).fetchone()[0]

    if count == 0:
        await message.answer(
            "🔄 В базе ещё нет расписания.\n"
            "Сейчас загружу его с сайта..."
        )

        try:
            await update_schedule(
                message.chat.id
            )
        except Exception as e:
            await message.answer(
                "❌ Не удалось загрузить расписание.\n\n"
                f"{str(e)[:500]}"
            )
            return

    with db() as c:
        rows = c.execute("""
            SELECT *
            FROM remote_schedule
            WHERE chat_id=?
            ORDER BY lesson_date, lesson_time
        """, (message.chat.id,)).fetchall()

    # Показываем ближайшие 14 дней
    today = now().date()
    limit_date = today + timedelta(days=14)

    filtered = []

    for row in rows:
        try:
            d = datetime.strptime(
                row["lesson_date"],
                "%Y-%m-%d"
            ).date()

            if today <= d <= limit_date:
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
async def today_schedule(message: Message):
    ensure(message)

    today_value = now().strftime("%Y-%m-%d")

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
# ДОБАВИТЬ ПАРУ В СВОЁ РАСПИСАНИЕ
# ============================================================

@dp.message(Command("add_lesson"))
async def add_lesson(message: Message, state: FSMContext):
    ensure(message)

    if not is_starosta(message):
        await message.answer(
            "❌ Только староста может добавлять пары."
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
        subject=message.text.strip()
    )

    await state.set_state(
        States.schedule_day
    )

    await message.answer(
        "📅 Введи день недели числом:\n\n"
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
    value = message.text.strip()

    if not value.isdigit() or not 1 <= int(value) <= 7:
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
    value = message.text.strip()

    if not re.match(
        r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$",
        value
    ):
        await message.answer(
            "❌ Неверный формат.\n"
            "Пример: 08:00-09:30"
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
        "✅ Пара добавлена!\n\n"
        f"📚 {data['subject']}\n"
        f"🕐 {value}"
    )


# ============================================================
# УДАЛИТЬ ПАРУ
# ============================================================

@dp.message(Command("delete_lesson"))
async def delete_lesson(message: Message):
    ensure(message)

    if not is_starosta(message):
        await message.answer(
            "❌ Только староста может удалять пары."
        )
        return

    with db() as c:
        rows = c.execute("""
            SELECT *
            FROM schedule
            WHERE chat_id=?
            ORDER BY day, lesson_time
        """, (message.chat.id,)).fetchall()

    if not rows:
        await message.answer(
            "📭 Ручных пар пока нет."
        )
        return

    text = "🗑 Твои сохранённые пары:\n\n"

    for row in rows:
        text += (
            f"ID: {row['id']}\n"
            f"📚 {row['subject']}\n"
            f"🕐 {row['lesson_time']}\n\n"
        )

    text += (
        "Чтобы удалить пару, напиши:\n"
        "/delete_1\n\n"
        "Например, если ID пары 5:\n"
        "/delete_5"
    )

    await message.answer(text)


@dp.message(F.text.regexp(r"^/delete_\d+$"))
async def delete_lesson_by_id(message: Message):
    ensure(message)

    if not is_starosta(message):
        await message.answer(
            "❌ Только староста может удалять пары."
        )
        return

    lesson_id = int(
        message.text.split("_")[1]
    )

    with db() as c:
        row = c.execute("""
            SELECT *
            FROM schedule
            WHERE id=? AND chat_id=?
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
            WHERE id=? AND chat_id=?
        """, (
            lesson_id,
            message.chat.id
        ))

    await message.answer(
        "🗑 Пара удалена:\n\n"
        f"📚 {row['subject']}\n"
        f"🕐 {row['lesson_time']}"
    )


# ============================================================
# МОЁ СОХРАНЁННОЕ РАСПИСАНИЕ
# ============================================================

@dp.message(Command("my_schedule"))
async def my_schedule(message: Message):
    ensure(message)

    with db() as c:
        rows = c.execute("""
            SELECT *
            FROM schedule
            WHERE chat_id=?
            ORDER BY day, lesson_time
        """, (message.chat.id,)).fetchall()

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
                f"\n📌 {days.get(row['day'], '')}\n"
            )

        text += (
            f"• {row['lesson_time']} — "
            f"{row['subject']}\n"
        )

    await message.answer(text)


# ============================================================
# СПИСОК СТУДЕНТОВ
# ============================================================

@dp.message(Command("students"))
async def students(message: Message):
    ensure(message)

    if not is_starosta(message):
        await message.answer(
            "❌ Только староста может смотреть список студентов."
        )
        return

    with db() as c:
        rows = c.execute("""
            SELECT *
            FROM students
            WHERE chat_id=?
            ORDER BY name
        """, (message.chat.id,)).fetchall()

    if not rows:
        await message.answer(
            "📭 Студенты пока не зарегистрированы."
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
            f"{index}. {row['name']}{username}\n"
        )

    await message.answer(text)


# ============================================================
# ИСТОРИЯ ПЕРЕКЛИЧЕК
# ============================================================

@dp.message(Command("history"))
async def history(message: Message):
    ensure(message)

    if not is_starosta(message):
        await message.answer(
            "❌ Только староста может смотреть историю."
        )
        return

    with db() as c:
        rows = c.execute("""
            SELECT *
            FROM rollcalls
            WHERE chat_id=?
            ORDER BY id DESC
            LIMIT 20
        """, (message.chat.id,)).fetchall()

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
                            THEN 1 ELSE 0
                        END
                    ) AS present,
                    SUM(
                        CASE
                            WHEN status='absent'
                            THEN 1 ELSE 0
                        END
                    ) AS absent
                FROM attendance
                WHERE rollcall_id=?
            """, (row["id"],)).fetchone()

        present = stats["present"] or 0
        absent = stats["absent"] or 0

        text += (
            f"📅 {row['started_at']}\n"
            f"📚 {row['subject']}\n"
            f"🟢 {present} | 🔴 {absent}\n"
            f"Статус: {row['status']}\n\n"
        )

    await message.answer(text)


# ============================================================
# СТАТИСТИКА
# ============================================================

@dp.message(Command("stats"))
async def stats(message: Message):
    ensure(message)

    with db() as c:
        students = c.execute("""
            SELECT COUNT(*)
            FROM students
            WHERE chat_id=?
        """, (message.chat.id,)).fetchone()[0]

        total = c.execute("""
            SELECT COUNT(*)
            FROM attendance a
            JOIN rollcalls r
              ON r.id=a.rollcall_id
            WHERE r.chat_id=?
        """, (message.chat.id,)).fetchone()[0]

        present = c.execute("""
            SELECT COUNT(*)
            FROM attendance a
            JOIN rollcalls r
              ON r.id=a.rollcall_id
            WHERE r.chat_id=?
              AND a.status='present'
        """, (message.chat.id,)).fetchone()[0]

        absent = c.execute("""
            SELECT COUNT(*)
            FROM attendance a
            JOIN rollcalls r
              ON r.id=a.rollcall_id
            WHERE r.chat_id=?
              AND a.status='absent'
        """, (message.chat.id,)).fetchone()[0]

        rollcalls = c.execute("""
            SELECT COUNT(*)
            FROM rollcalls
            WHERE chat_id=?
        """, (message.chat.id,)).fetchone()[0]

    percent = (
        (present / total) * 100
        if total
        else 0
    )

    await message.answer(
        "📊 СТАТИСТИКА\n\n"
        f"👥 Студентов: {students}\n"
        f"📝 Перекличек: {rollcalls}\n"
        f"📌 Всего отметок: {total}\n"
        f"🟢 Присутствий: {present}\n"
        f"🔴 Отсутствий: {absent}\n"
        f"📈 Посещаемость: {percent:.1f}%"
    )


# ============================================================
# РУССКИЕ ТЕКСТОВЫЕ КОМАНДЫ
# ============================================================

@dp.message(F.text.casefold() == "расписание")
async def text_schedule(message: Message):
    await schedule(message)


@dp.message(F.text.casefold() == "сегодня")
async def text_today(message: Message):
    await today_schedule(message)


@dp.message(F.text.casefold() == "обновить расписание")
async def text_refresh(message: Message):
    await refresh_schedule(message)


@dp.message(F.text.casefold() == "начать перекличку")
async def text_rollcall(message: Message, state: FSMContext):
    await rollcall(message, state)


@dp.message(F.text.casefold() == "результаты")
async def text_attendance(message: Message):
    await attendance(message)


@dp.message(F.text.casefold() == "финиш")
async def text_finish(message: Message):
    await finish(message)


@dp.message(F.text.casefold() == "сет староста")
async def text_starosta(message: Message):
    await set_starosta(message)


@dp.message(F.text.casefold() == "студенты")
async def text_students(message: Message):
    await students(message)


@dp.message(F.text.casefold() == "история")
async def text_history(message: Message):
    await history(message)


@dp.message(F.text.casefold() == "статистика")
async def text_stats(message: Message):
    await stats(message)


# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    init_db()

    print("====================================")
    print("БОТ ЗАПУЩЕН")
    print(f"Группа: {INSTITUTE} {COURSE} курс {GROUP_NAME}")
    print(f"Group ID: {GROUP_ID}")
    print("====================================")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
