import os
import csv
import io
import asyncio
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

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


# =========================
# НАСТРОЙКИ
# =========================

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

TZ = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))
DB_PATH = os.getenv("DB_PATH", "attendance.db")

bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# =========================
# СОСТОЯНИЯ
# =========================

class States(StatesGroup):
    subject = State()

    schedule_subject = State()
    schedule_day = State()
    schedule_time = State()


# =========================
# БАЗА ДАННЫХ
# =========================

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
        """)


# =========================
# РЕГИСТРАЦИЯ ПОЛЬЗОВАТЕЛЯ
# =========================

def ensure(message: Message):

    with db() as c:

        c.execute("""
        INSERT INTO chats(chat_id, title)
        VALUES(?, ?)

        ON CONFLICT(chat_id)
        DO UPDATE SET title=excluded.title
        """,
        (
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
            """,
            (
                message.chat.id,
                message.from_user.id,
                message.from_user.full_name,
                message.from_user.username,
                now().isoformat()
            ))


# =========================
# ПРОВЕРКА СТАРОСТЫ
# =========================

def is_admin(message):

    with db() as c:

        row = c.execute("""
        SELECT starosta_id
        FROM chats
        WHERE chat_id=?
        """,
        (message.chat.id,)).fetchone()

    if not row:
        return False

    return row["starosta_id"] == message.from_user.id


async def admin_only(message):

    if not is_admin(message):

        await message.answer(
            "❌ Эта команда доступна только старосте."
        )

        return False

    return True


# =========================
# ТЕКУЩАЯ ПЕРЕКЛИЧКА
# =========================

def active(chat_id):

    with db() as c:

        return c.execute("""
        SELECT *
        FROM rollcalls
        WHERE chat_id=?
        AND status='active'
        ORDER BY id DESC
        LIMIT 1
        """,
        (chat_id,)).fetchone()


# =========================
# КНОПКИ ПЕРЕКЛИЧКИ
# =========================

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


# =========================
# ДНИ НЕДЕЛИ
# =========================

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

    return days[day]


# =========================
# START
# =========================

@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):

    await state.clear()

    ensure(message)

    await message.answer(
        "👋 Бот старосты запущен!\n\n"

        "Пиши команды обычным сообщением:\n\n"

        "👑 староста — назначить себя старостой\n"
        "📋 перекличка — начать перекличку\n"
        "📊 результаты — текущие результаты\n"
        "🛑 завершить — завершить перекличку\n"
        "📚 история — история перекличек\n"
        "📈 статистика — статистика посещаемости\n"
        "📥 выгрузка — скачать всю историю\n\n"

        "📅 добавить пару — добавить пару\n"
        "🗓 расписание — показать расписание\n"
        "📌 сегодня — пары сегодня\n"
        "🗑 удалить пару — удалить пару\n\n"

        "❌ отмена — отменить действие\n"
        "ℹ️ помощь — список команд"
    )


# =========================
# ПОМОЩЬ
# =========================

async def help_command(message: Message):

    await message.answer(
        "📚 КОМАНДЫ БОТА\n\n"

        "👑 староста\n"
        "Назначить себя старостой группы.\n\n"

        "📋 перекличка\n"
        "Начать новую перекличку.\n"
        "Бот спросит название предмета.\n\n"

        "📊 результаты\n"
        "Показать ответы текущей переклички.\n\n"

        "🛑 завершить\n"
        "Закончить текущую перекличку.\n"
        "Она сохраняется в историю.\n\n"

        "📚 история\n"
        "Показать последние сохранённые переклички.\n\n"

        "📈 статистика\n"
        "Показать посещаемость студентов.\n\n"

        "📥 выгрузка\n"
        "Получить CSV-файл со всей историей.\n\n"

        "📅 добавить пару\n"
        "Добавить пару в расписание.\n\n"

        "🗓 расписание\n"
        "Показать всё расписание.\n\n"

        "📌 сегодня\n"
        "Показать пары на сегодня.\n\n"

        "🗑 удалить пару\n"
        "Удалить пару из расписания.\n\n"

        "❌ отмена\n"
        "Отменить текущий ввод."
    )


# =========================
# РУССКИЕ ТЕКСТОВЫЕ КОМАНДЫ
# =========================

@dp.message(F.text.lower() == "помощь")
async def help_text(message: Message):

    ensure(message)

    await help_command(message)


@dp.message(F.text.lower() == "отмена")
async def cancel_text(message: Message, state: FSMContext):

    await state.clear()

    await message.answer(
        "❌ Текущее действие отменено."
    )


# =========================
# СТАРОСТА
# =========================

@dp.message(F.text.lower() == "староста")
async def set_starosta(message: Message):

    ensure(message)

    with db() as c:

        row = c.execute("""
        SELECT starosta_id
        FROM chats
        WHERE chat_id=?
        """,
        (message.chat.id,)).fetchone()

        if (
            row
            and row["starosta_id"]
            and row["starosta_id"] != message.from_user.id
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
        """,
        (
            message.from_user.id,
            message.from_user.full_name,
            message.chat.id
        ))

    await message.answer(
        "👑 Ты назначен старостой этой группы!"
    )


# =========================
# НАЧАЛО ПЕРЕКЛИЧКИ
# =========================

@dp.message(F.text.lower() == "перекличка")
async def rollcall(
    message: Message,
    state: FSMContext
):

    ensure(message)

    if not await admin_only(message):
        return

    if active(message.chat.id):

        await message.answer(
            "⚠️ Перекличка уже идёт.\n"
            "Сначала напиши «завершить»."
        )

        return

    await state.set_state(
        States.subject
    )

    await message.answer(
        "📚 Напиши название предмета."
    )


# =========================
# ПРЕДМЕТ ПЕРЕКЛИЧКИ
# =========================

@dp.message(States.subject)
async def rollcall_subject(
    message: Message,
    state: FSMContext
):

    if not await admin_only(message):
        return

    subject = (
        message.text or ""
    ).strip()

    if len(subject) < 2:

        await message.answer(
            "❌ Название предмета слишком короткое."
        )

        return

    date_text = now().strftime(
        "%d.%m.%Y %H:%M"
    )

    with db() as c:

        c.execute("""
        INSERT INTO rollcalls(
            chat_id,
            subject,
            started_at,
            status
        )

        VALUES(
            ?,
            ?,
            ?,
            'active'
        )
        """,
        (
            message.chat.id,
            subject,
            date_text
        ))

    await state.clear()

    await message.answer(
        "📋 ПЕРЕКЛИЧКА\n\n"
        f"📚 Предмет: {subject}\n"
        f"📅 Дата: {date_text}\n\n"
        "Выберите ответ:",
        reply_markup=attendance_buttons()
    )


# =========================
# ОТВЕТ НА ПЕРЕКЛИЧКУ
# =========================

@dp.callback_query(
    F.data.in_({
        "att:will",
        "att:wont"
    })
)
async def vote(call: CallbackQuery):

    chat_id = call.message.chat.id

    rollcall = active(chat_id)

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

        c.execute("""
        INSERT INTO attendance(
            rollcall_id,
            user_id,
            name,
            status,
            answered_at
        )

        VALUES(
            ?,
            ?,
            ?,
            ?,
            ?
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
            call.from_user.id,
            call.from_user.full_name,
            status,
            now().isoformat()
        ))

        c.execute("""
        INSERT INTO students(
            chat_id,
            user_id,
            name,
            username,
            first_seen
        )

        VALUES(
            ?,
            ?,
            ?,
            ?,
            ?
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
            call.from_user.id,
            call.from_user.full_name,
            call.from_user.username,
            now().isoformat()
        ))

    await call.answer(
        "Ответ сохранён!"
    )

    await call.message.answer(
        f"✅ {call.from_user.full_name}: {status}"
    )


# =========================
# ФОРМИРОВАНИЕ РЕЗУЛЬТАТОВ
# =========================

def get_results(rollcall_id):

    with db() as c:

        rollcall = c.execute("""
        SELECT *
        FROM rollcalls
        WHERE id=?
        """,
        (rollcall_id,)).fetchone()

        rows = c.execute("""
        SELECT name, status
        FROM attendance
        WHERE rollcall_id=?
        ORDER BY name COLLATE NOCASE
        """,
        (rollcall_id,)).fetchall()

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
        f"📅 {rollcall['started_at']}\n\n"
    )

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
        f"\n\n🔴 Не будут — {len(wont)}\n"
    )

    if wont:

        text += "\n".join(
            "• " + name
            for name in wont
        )

    else:

        text += "—"

    return text


# =========================
# РЕЗУЛЬТАТЫ
# =========================

@dp.message(F.text.lower() == "результаты")
async def attendance_command(message: Message):

    ensure(message)

    if not await admin_only(message):
        return

    rollcall = active(
        message.chat.id
    )

    if not rollcall:

        await message.answer(
            "ℹ️ Сейчас нет активной переклички."
        )

        return

    await message.answer(
        get_results(
            rollcall["id"]
        )
    )


# =========================
# ЗАВЕРШЕНИЕ
# =========================

@dp.message(F.text.lower() == "завершить")
async def finish(message: Message):

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
        """,
        (
            finished_at,
            rollcall["id"]
        ))

    await message.answer(
        "🛑 Перекличка завершена!\n\n"
        "💾 Она сохранена в истории.\n\n"
        + result
    )


# =========================
# ИСТОРИЯ
# =========================

@dp.message(F.text.lower() == "история")
async def history(message: Message):

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

        LIMIT 20
        """,
        (message.chat.id,)).fetchall()

    if not rows:

        await message.answer(
            "📚 История пока пустая."
        )

        return

    text = "📚 ИСТОРИЯ ПЕРЕКЛИЧЕК\n\n"

    for row in rows:

        with db() as c:

            will = c.execute("""
            SELECT COUNT(*)
            FROM attendance

            WHERE
                rollcall_id=?
                AND status='Буду'
            """,
            (row["id"],)).fetchone()[0]

            wont = c.execute("""
            SELECT COUNT(*)
            FROM attendance

            WHERE
                rollcall_id=?
                AND status='Не буду'
            """,
            (row["id"],)).fetchone()[0]

        text += (
            f"#{row['id']} — "
            f"{row['subject']}\n"
            f"📅 {row['started_at']}\n"
            f"🟢 {will}  🔴 {wont}\n\n"
        )

    await message.answer(text)


# =========================
# СТАТИСТИКА
# =========================

@dp.message(F.text.lower() == "статистика")
async def statistics(message: Message):

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

        WHERE
            r.chat_id=?

        ORDER BY
            a.name COLLATE NOCASE
        """,
        (message.chat.id,)).fetchall()

    if not people:

        await message.answer(
            "📈 Статистики пока нет."
        )

        return

    text = "📈 СТАТИСТИКА ПОСЕЩАЕМОСТИ\n\n"

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
            """,
            (
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
            """,
            (
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

    await message.answer(text)


# =========================
# ВЫГРУЗКА CSV
# =========================

@dp.message(F.text.lower() == "выгрузка")
async def export_csv(message: Message):

    ensure(message)

    if not await admin_only(message):
        return

    with db() as c:

        rows = c.execute("""
        SELECT
            r.id,
            r.subject,
            r.started_at,
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
            a.name
        """,
        (message.chat.id,)).fetchall()

    if not rows:

        await message.answer(
            "📥 Пока нет данных для выгрузки."
        )

        return

    output = io.StringIO()

    writer = csv.writer(output)

    writer.writerow([
        "ID",
        "Предмет",
        "Дата",
        "Telegram ID",
        "ФИО",
        "Статус",
        "Время ответа"
    ])

    for row in rows:

        writer.writerow([
            row["id"],
            row["subject"],
            row["started_at"],
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
        caption="📥 История посещаемости"
    )


# =========================
# ДОБАВЛЕНИЕ ПАРЫ
# =========================

@dp.message(F.text.lower() == "добавить пару")
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


# =========================
# ПРЕДМЕТ ПАРЫ
# =========================

@dp.message(States.schedule_subject)
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


# =========================
# ДЕНЬ ПАРЫ
# =========================

@dp.message(States.schedule_day)
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
        "Например: 09:00"
    )


# =========================
# ВРЕМЯ ПАРЫ
# =========================

@dp.message(States.schedule_time)
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
            "❌ Неверный формат.\n"
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
            ?,
            ?,
            ?,
            ?
        )
        """,
        (
            message.chat.id,
            data["subject"],
            data["day"],
            value
        ))

    await state.clear()

    await message.answer(
        "✅ Пара добавлена!\n\n"
        f"📚 {data['subject']}\n"
        f"📅 {day_name(data['day'])}\n"
        f"⏰ {value}"
    )


# =========================
# РАСПИСАНИЕ
# =========================

@dp.message(F.text.lower() == "расписание")
async def schedule(message: Message):

    ensure(message)

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
        """,
        (message.chat.id,)).fetchall()

    if not rows:

        await message.answer(
            "📅 Расписание пока пустое."
        )

        return

    text = "📅 РАСПИСАНИЕ\n"

    current_day = 0

    for row in rows:

        if row["day"] != current_day:

            current_day = row["day"]

            text += (
                f"\n📌 {day_name(current_day)}\n"
            )

        text += (
            f"⏰ {row['lesson_time']} — "
            f"{row['subject']} "
            f"(ID: {row['id']})\n"
        )

    await message.answer(text)


# =========================
# СЕГОДНЯ
# =========================

@dp.message(F.text.lower() == "сегодня")
async def today(message: Message):

    ensure(message)

    day = now().isoweekday()

    with db() as c:

        rows = c.execute("""
        SELECT *
        FROM schedule

        WHERE
            chat_id=?
            AND day=?
            AND enabled=1

        ORDER BY lesson_time
        """,
        (
            message.chat.id,
            day
        )).fetchall()

    if not rows:

        await message.answer(
            f"📌 Сегодня — {day_name(day)}.\n\n"
            "Пар нет."
        )

        return

    text = (
        f"📌 СЕГОДНЯ — "
        f"{day_name(day)}\n\n"
    )

    for row in rows:

        text += (
            f"⏰ {row['lesson_time']} — "
            f"{row['subject']}\n"
        )

    await message.answer(text)


# =========================
# УДАЛЕНИЕ ПАРЫ
# =========================

@dp.message(F.text.lower().startswith("удалить пару"))
async def delete_lesson(message: Message):

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
                """,
                (
                    lesson_id,
                    message.chat.id
                )).fetchone()

                if not row:

                    await message.answer(
                        "❌ Пара с таким ID не найдена."
                    )

                    return

                c.execute("""
                UPDATE schedule

                SET enabled=0

                WHERE id=?
                """,
                (lesson_id,))

            await message.answer(
                "🗑 Пара удалена!\n\n"
                f"📚 {row['subject']}\n"
                f"📅 {day_name(row['day'])}\n"
                f"⏰ {row['lesson_time']}"
            )

            return

    await message.answer(
        "Используй:\n\n"
        "удалить пару ID\n\n"
        "Например:\n"
        "удалить пару 3"
    )


# =========================
# НАПОМИНАНИЯ
# =========================

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
                """,
                (day,)).fetchall()

            for row in rows:

                hour, minute = map(
                    int,
                    row["lesson_time"].split(":")
                )

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

                if (
                    0 <= minutes <= row["reminder"]
                    and key not in sent
                ):

                    if not active(
                        row["chat_id"]
                    ):

                        await bot.send_message(
                            row["chat_id"],
                            "⏰ НАПОМИНАНИЕ\n\n"
                            f"Через {max(1, round(minutes))} "
                            f"мин. пара:\n"
                            f"📚 {row['subject']}\n\n"
                            "Староста может начать "
                            "перекличку, написав:\n"
                            "перекличка"
                        )

                    sent.add(key)

            sent = {
                item
                for item in sent
                if item[1] == current.date()
            }

        except Exception as error:

            print(
                "Ошибка напоминаний:",
                error
            )

        await asyncio.sleep(60)


# =========================
# ЗАПУСК
# =========================

async def main():

    init_db()

    print(
        "BOT STARTED"
    )

    await asyncio.gather(
        dp.start_polling(bot),
        reminder_loop()
    )


if __name__ == "__main__":

    asyncio.run(main())
