import os
import json
import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

bot = Bot(TOKEN)
dp = Dispatcher()

DATA_FILE = "attendance.json"

# ID старосты
# Первый раз бот сам запомнит человека, который использует /set_starosta
STAROSTA_ID = None

attendance = {}
current_lesson = None


def load_data():
    global STAROSTA_ID, attendance, current_lesson

    if not os.path.exists(DATA_FILE):
        return

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        STAROSTA_ID = data.get("starosta_id")
        attendance = data.get("attendance", {})
        current_lesson = data.get("current_lesson")

    except Exception:
        STAROSTA_ID = None
        attendance = {}
        current_lesson = None


def save_data():
    data = {
        "starosta_id": STAROSTA_ID,
        "attendance": attendance,
        "current_lesson": current_lesson
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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


def is_starosta(user_id):
    return STAROSTA_ID is not None and str(user_id) == str(STAROSTA_ID)


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Привет! Я бот для учёта посещаемости.\n\n"
        "📝 /rollcall — начать перекличку\n"
        "📊 /attendance — посмотреть результаты\n"
        "🛑 /finish — завершить перекличку\n"
        "👑 /set_starosta — назначить себя старостой"
    )


@dp.message(Command("set_starosta"))
async def set_starosta(message: Message):
    global STAROSTA_ID

    if STAROSTA_ID is not None:
        if not is_starosta(message.from_user.id):
            await message.answer(
                "❌ Староста уже назначен."
            )
            return

    STAROSTA_ID = message.from_user.id
    save_data()

    await message.answer(
        f"👑 {message.from_user.full_name}, "
        "ты назначен старостой."
    )


@dp.message(Command("rollcall"))
async def rollcall(message: Message):
    if not is_starosta(message.from_user.id):
        await message.answer(
            "❌ Только староста может запускать перекличку."
        )
        return

    global attendance, current_lesson

    attendance = {}

    current_lesson = {
        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "chat_id": message.chat.id
    }

    save_data()

    await message.answer(
        "📋 ПЕРЕКЛИЧКА\n\n"
        "Отметьте, будете ли вы на занятии.\n\n"
        "Ответ можно изменить повторным нажатием.",
        reply_markup=attendance_keyboard()
    )


@dp.callback_query(F.data.in_({"will", "wont"}))
async def vote(callback: CallbackQuery):
    if current_lesson is None:
        await callback.answer(
            "Перекличка сейчас не проводится.",
            show_alert=True
        )
        return

    user_id = str(callback.from_user.id)

    if callback.data == "will":
        attendance[user_id] = {
            "name": callback.from_user.full_name,
            "status": "Буду"
        }
        status = "🟢 Буду"

    else:
        attendance[user_id] = {
            "name": callback.from_user.full_name,
            "status": "Не буду"
        }
        status = "🔴 Не буду"

    save_data()

    await callback.answer("Ответ сохранён")

    await callback.message.answer(
        f"✅ {callback.from_user.full_name}, "
        f"твой ответ: {status}"
    )


@dp.message(Command("attendance"))
async def show_attendance(message: Message):
    if not is_starosta(message.from_user.id):
        await message.answer(
            "❌ Результаты доступны только старосте."
        )
        return

    if current_lesson is None:
        await message.answer(
            "Сейчас активной переклички нет."
        )
        return

    will = []
    wont = []

    for student in attendance.values():
        if student["status"] == "Буду":
            will.append(student["name"])
        else:
            wont.append(student["name"])

    text = (
        "📊 РЕЗУЛЬТАТЫ ПЕРЕКЛИЧКИ\n\n"
        f"📅 {current_lesson['date']}\n\n"
        f"🟢 Будут — {len(will)}\n"
    )

    if will:
        text += "\n".join(
            f"• {name}" for name in will
        )
    else:
        text += "—"

    text += f"\n\n🔴 Не будут — {len(wont)}\n"

    if wont:
        text += "\n".join(
            f"• {name}" for name in wont
        )
    else:
        text += "—"

    await message.answer(text)


@dp.message(Command("finish"))
async def finish(message: Message):
    global current_lesson

    if not is_starosta(message.from_user.id):
        await message.answer(
            "❌ Только староста может завершить перекличку."
        )
        return

    if current_lesson is None:
        await message.answer(
            "Активной переклички нет."
        )
        return

    will = sum(
        1 for x in attendance.values()
        if x["status"] == "Буду"
    )

    wont = sum(
        1 for x in attendance.values()
        if x["status"] == "Не буду"
    )

    current_lesson = None
    save_data()

    await message.answer(
        "🛑 Перекличка завершена.\n\n"
        f"🟢 Будут: {will}\n"
        f"🔴 Не будут: {wont}"
    )


async def main():
    load_data()

    print("Бот запущен")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
