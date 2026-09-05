import os
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(TOKEN)
dp = Dispatcher()
attendance = {}

keyboard = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🟢 Буду", callback_data="will"),
    InlineKeyboardButton(text="🔴 Не буду", callback_data="wont")
]])

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Привет! Я бот для учёта посещаемости.\n\n"
        "/rollcall — начать перекличку\n"
        "/attendance — посмотреть результаты"
    )

@dp.message(Command("rollcall"))
async def rollcall(message: Message):
    attendance.clear()
    await message.answer("📋 Перекличка начата\n\nОтметьтесь:", reply_markup=keyboard)

@dp.callback_query(F.data.in_({"will", "wont"}))
async def vote(callback: CallbackQuery):
    attendance[callback.from_user.id] = "Буду" if callback.data == "will" else "Не буду"
    await callback.answer("Ответ сохранён")
    await callback.message.answer(
        f"✅ {callback.from_user.full_name}, ответ сохранён: {attendance[callback.from_user.id]}"
    )

@dp.message(Command("attendance"))
async def show(message: Message):
    if not attendance:
        await message.answer("Пока никто не отметился.")
        return
    will = []
    wont = []
    for uid, status in attendance.items():
        try:
            user = await bot.get_chat(uid)
            name = user.full_name
        except Exception:
            name = str(uid)
        (will if status == "Буду" else wont).append(name)

    text = f"📊 Результаты\n\n🟢 Будут: {len(will)}\n"
    text += "\n".join("• " + x for x in will) or "—"
    text += f"\n\n🔴 Не будут: {len(wont)}\n"
    text += "\n".join("• " + x for x in wont) or "—"
    await message.answer(text)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
