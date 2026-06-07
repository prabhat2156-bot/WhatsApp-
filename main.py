import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv
from faker import Faker

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

fake = Faker()

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        "🤖 Bot Online!\n\n"
        "/fake - Generate fake user\n"
        "/ping - Check bot status"
    )

@dp.message(Command("ping"))
async def ping_cmd(message: types.Message):
    await message.answer("🏓 Pong!")

@dp.message(Command("fake"))
async def fake_cmd(message: types.Message):
    await message.answer(
        f"👤 Name: {fake.name()}\n"
        f"📧 Email: {fake.email()}\n"
        f"📍 Address: {fake.address()}"
    )

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
