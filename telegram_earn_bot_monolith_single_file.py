# ============================================================
# TELEGRAM EARN BOT (MONOLITH SINGLE FILE)
# Stack: Python 3.11 + aiogram 3 + SQLite
# ============================================================
# FEATURES:
# - Registration
# - Referral system
# - Tasks system
# - Manual moderation
# - Withdrawals
# - Admin panel
# - Anti spam/flood
# - CAPTCHA
# - SQLite
# - Fully configurable via .env
# ============================================================
# INSTALL:
# pip install aiogram aiosqlite python-dotenv
# ============================================================
# RUN:
# python bot.py
# ============================================================
# .env EXAMPLE:
# BOT_TOKEN=TOKEN
# BOT_USERNAME=my_bot
# ADMINS=123456789,987654321
# ADMIN_GROUP_ID=-1001234567890
# SUPPORT_USERNAME=@support
# REFERRAL_REWARD=5
# MIN_WITHDRAW=50
# FLOOD_LIMIT=1
# CAPTCHA_ENABLED=true
# MAX_REFERRALS_PER_IP=5
# ENABLE_LOGS=true
# ============================================================

import asyncio
import html
import logging
import os
import random
import sqlite3
import string
import time
from contextlib import suppress
from datetime import datetime
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ============================================================
# LOAD ENV
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "my_bot")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "0"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support")
REFERRAL_REWARD = float(os.getenv("REFERRAL_REWARD", "5"))
MIN_WITHDRAW = float(os.getenv("MIN_WITHDRAW", "50"))
FLOOD_LIMIT = int(os.getenv("FLOOD_LIMIT", "1"))
CAPTCHA_ENABLED = os.getenv("CAPTCHA_ENABLED", "true").lower() == "true"
ENABLE_LOGS = os.getenv("ENABLE_LOGS", "true").lower() == "true"
ADMINS = [int(x) for x in os.getenv("ADMINS", "").split(",") if x]
DB_NAME = "database.db"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in .env")

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# ============================================================
# BOT INIT
# ============================================================

bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())

# ============================================================
# MEMORY CACHE
# ============================================================

user_last_message = {}
user_captcha = {}
user_sessions = {}

# ============================================================
# STATES
# ============================================================

class WithdrawalState(StatesGroup):
    amount = State()
    username = State()


class AdminTaskState(StatesGroup):
    title = State()
    description = State()
    link = State()
    reward = State()
    type = State()


class ManualSubmissionState(StatesGroup):
    content = State()

# ============================================================
# DATABASE
# ============================================================

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    balance REAL DEFAULT 0,
    referrals INTEGER DEFAULT 0,
    completed_tasks INTEGER DEFAULT 0,
    reg_date TEXT,
    status TEXT DEFAULT 'active',
    invited_by INTEGER DEFAULT NULL,
    captcha_passed INTEGER DEFAULT 0,
    last_ip TEXT DEFAULT '',
    is_banned INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    description TEXT,
    link TEXT,
    reward REAL,
    type TEXT,
    status TEXT DEFAULT 'active',
    limit_count INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inviter_id INTEGER,
    invited_id INTEGER,
    reward REAL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS task_completions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    task_id INTEGER,
    proof TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    username TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    type TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS admin_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER,
    action TEXT,
    created_at TEXT
);
"""


async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.executescript(CREATE_TABLES_SQL)
        await db.commit()

# ============================================================
# HELPERS
# ============================================================


def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


async def log_admin(admin_id: int, action: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO admin_logs(admin_id, action, created_at) VALUES (?, ?, ?)",
            (admin_id, action, datetime.now().isoformat())
        )
        await db.commit()


async def create_user(message: Message, inviter_id: Optional[int] = None):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (message.from_user.id,)
        )
        exists = await cursor.fetchone()

        if exists:
            return

        await db.execute(
            """
            INSERT INTO users(
                user_id,
                username,
                first_name,
                reg_date,
                invited_by
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                message.from_user.id,
                message.from_user.username,
                message.from_user.first_name,
                datetime.now().isoformat(),
                inviter_id
            )
        )

        await db.commit()

        if inviter_id and inviter_id != message.from_user.id:
            await process_referral(inviter_id, message.from_user.id)


async def process_referral(inviter_id: int, invited_id: int):
    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute(
            "SELECT id FROM referrals WHERE invited_id = ?",
            (invited_id,)
        )
        already = await cursor.fetchone()

        if already:
            return

        await db.execute(
            "UPDATE users SET balance = balance + ?, referrals = referrals + 1 WHERE user_id = ?",
            (REFERRAL_REWARD, inviter_id)
        )

        await db.execute(
            "INSERT INTO referrals(inviter_id, invited_id, reward, created_at) VALUES (?, ?, ?, ?)",
            (inviter_id, invited_id, REFERRAL_REWARD, datetime.now().isoformat())
        )

        await db.execute(
            "INSERT INTO transactions(user_id, amount, type, created_at) VALUES (?, ?, ?, ?)",
            (inviter_id, REFERRAL_REWARD, 'referral', datetime.now().isoformat())
        )

        await db.commit()

        with suppress(Exception):
            await bot.send_message(
                inviter_id,
                f"🎉 Вам начислено <b>{REFERRAL_REWARD}</b> за нового реферала"
            )


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,)
        )
        return await cursor.fetchone()


async def update_balance(user_id: int, amount: float):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id)
        )

        await db.execute(
            "INSERT INTO transactions(user_id, amount, type, created_at) VALUES (?, ?, ?, ?)",
            (user_id, amount, 'task_reward', datetime.now().isoformat())
        )

        await db.commit()


async def anti_flood(user_id: int) -> bool:
    now = time.time()
    last = user_last_message.get(user_id, 0)

    if now - last < FLOOD_LIMIT:
        return False

    user_last_message[user_id] = now
    return True


async def check_captcha(user_id: int):
    emojis = ["🍎", "🚀", "🐱", "🔥"]
    correct = random.choice(emojis)
    random.shuffle(emojis)

    kb = InlineKeyboardBuilder()

    for e in emojis:
        kb.add(
            InlineKeyboardButton(
                text=e,
                callback_data=f"captcha:{e}"
            )
        )

    user_captcha[user_id] = correct

    return correct, kb.adjust(2).as_markup()


async def is_banned(user_id: int):
    user = await get_user(user_id)
    if not user:
        return False
    return bool(user[10])


async def admin_notify(text: str):
    if ADMIN_GROUP_ID:
        with suppress(Exception):
            await bot.send_message(ADMIN_GROUP_ID, text)

# ============================================================
# MENUS
# ============================================================


def main_menu():
    kb = InlineKeyboardBuilder()

    kb.row(
        InlineKeyboardButton(text="👤 Профиль", callback_data="profile")
    )

    kb.row(
        InlineKeyboardButton(text="📋 Задания", callback_data="tasks")
    )

    kb.row(
        InlineKeyboardButton(text="📞 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME.replace('@', '')}")
    )

    return kb.as_markup()


def admin_menu():
    kb = InlineKeyboardBuilder()

    buttons = [
        ("📊 Статистика", "admin_stats"),
        ("👥 Пользователи", "admin_users"),
        ("📋 Задания", "admin_tasks"),
        ("💸 Выплаты", "admin_withdrawals"),
        ("📢 Рассылка", "admin_broadcast"),
        ("⚙ Настройки", "admin_settings"),
    ]

    for text, data in buttons:
        kb.row(InlineKeyboardButton(text=text, callback_data=data))

    return kb.as_markup()

# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message):

    if not await anti_flood(message.from_user.id):
        return

    args = message.text.split()
    inviter_id = None

    if len(args) > 1:
        try:
            inviter_id = int(args[1])
        except:
            inviter_id = None

    if inviter_id == message.from_user.id:
        inviter_id = None

    await create_user(message, inviter_id)

    if CAPTCHA_ENABLED:
        correct, markup = await check_captcha(message.from_user.id)

        await message.answer(
            f"🛡 Нажмите на эмодзи: {correct}",
            reply_markup=markup
        )
        return

    await message.answer(
        "🎉 Добро пожаловать в бот заработка",
        reply_markup=main_menu()
    )

# ============================================================
# CAPTCHA
# ============================================================

@dp.callback_query(F.data.startswith("captcha:"))
async def captcha_handler(callback: CallbackQuery):
    selected = callback.data.split(":")[1]
    correct = user_captcha.get(callback.from_user.id)

    if selected == correct:

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "UPDATE users SET captcha_passed = 1 WHERE user_id = ?",
                (callback.from_user.id,)
            )
            await db.commit()

        await callback.message.edit_text(
            "✅ CAPTCHA пройдена",
            reply_markup=None
        )

        await callback.message.answer(
            "🏠 Главное меню",
            reply_markup=main_menu()
        )

    else:
        correct, markup = await check_captcha(callback.from_user.id)

        await callback.message.edit_text(
            f"❌ Неверно. Нажмите: {correct}",
            reply_markup=markup
        )

# ============================================================
# PROFILE
# ============================================================

@dp.callback_query(F.data == "profile")
async def profile_handler(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)

    referral_link = f"https://t.me/{BOT_USERNAME}?start={callback.from_user.id}"

    text = f"""
👤 <b>Профиль</b>

🆔 ID: <code>{user[0]}</code>
💰 Баланс: <b>{user[3]}</b>
👥 Рефералы: <b>{user[4]}</b>
📋 Выполнено: <b>{user[5]}</b>
📅 Регистрация: <b>{user[6][:10]}</b>

🔗 Реферальная ссылка:
<code>{referral_link}</code>
"""

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="💸 Вывод", callback_data="withdraw"))

    await callback.message.edit_text(text, reply_markup=kb.as_markup())

# ============================================================
# TASKS
# ============================================================

@dp.callback_query(F.data == "tasks")
async def tasks_handler(callback: CallbackQuery):

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id, title, reward, type FROM tasks WHERE status = 'active'"
        )
        tasks = await cursor.fetchall()

    if not tasks:
        await callback.answer("Нет заданий", show_alert=True)
        return

    kb = InlineKeyboardBuilder()

    for task in tasks:
        kb.row(
            InlineKeyboardButton(
                text=f"💰 {task[2]} | {task[1]}",
                callback_data=f"task:{task[0]}"
            )
        )

    await callback.message.edit_text(
        "📋 Список заданий",
        reply_markup=kb.as_markup()
    )


@dp.callback_query(F.data.startswith("task:"))
async def task_view(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (task_id,)
        )
        task = await cursor.fetchone()

    if not task:
        await callback.answer("Задание не найдено")
        return

    text = f"""
📋 <b>{html.escape(task[1])}</b>

📝 {html.escape(task[2])}

💰 Награда: <b>{task[4]}</b>
🔗 Ссылка: {task[3]}
📂 Тип: {task[5]}
"""

    kb = InlineKeyboardBuilder()

    kb.row(
        InlineKeyboardButton(
            text="✅ Выполнить",
            callback_data=f"complete:{task_id}"
        )
    )

    await callback.message.edit_text(text, reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("complete:"))
async def complete_task(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])

    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute(
            "SELECT id FROM task_completions WHERE user_id = ? AND task_id = ?",
            (callback.from_user.id, task_id)
        )

        already = await cursor.fetchone()

        if already:
            await callback.answer("Вы уже выполняли это задание", show_alert=True)
            return

        cursor = await db.execute(
            "SELECT reward, type FROM tasks WHERE id = ?",
            (task_id,)
        )

        task = await cursor.fetchone()

        if not task:
            return

        reward = task[0]
        task_type = task[1]

        if task_type in ["manual", "photo", "screenshot"]:
            await db.execute(
                "INSERT INTO task_completions(user_id, task_id, status, created_at) VALUES (?, ?, ?, ?)",
                (
                    callback.from_user.id,
                    task_id,
                    'pending',
                    datetime.now().isoformat()
                )
            )
            await db.commit()

            await callback.message.answer(
                "📤 Отправьте доказательство выполнения"
            )
            return

        await db.execute(
            "INSERT INTO task_completions(user_id, task_id, status, created_at) VALUES (?, ?, ?, ?)",
            (
                callback.from_user.id,
                task_id,
                'approved',
                datetime.now().isoformat()
            )
        )

        await db.execute(
            "UPDATE users SET balance = balance + ?, completed_tasks = completed_tasks + 1 WHERE user_id = ?",
            (reward, callback.from_user.id)
        )

        await db.commit()

    await callback.answer("✅ Награда начислена", show_alert=True)

# ============================================================
# WITHDRAW
# ============================================================

@dp.callback_query(F.data == "withdraw")
async def withdraw_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WithdrawalState.amount)

    await callback.message.answer(
        f"💸 Введите сумму для вывода\nМинимум: {MIN_WITHDRAW}"
    )


@dp.message(WithdrawalState.amount)
async def withdraw_amount(message: Message, state: FSMContext):

    try:
        amount = float(message.text)
    except:
        await message.answer("Введите число")
        return

    user = await get_user(message.from_user.id)

    if amount < MIN_WITHDRAW:
        await message.answer("Слишком маленькая сумма")
        return

    if amount > user[3]:
        await message.answer("Недостаточно средств")
        return

    await state.update_data(amount=amount)
    await state.set_state(WithdrawalState.username)

    await message.answer("Введите username BOR_CASINO")


@dp.message(WithdrawalState.username)
async def withdraw_username(message: Message, state: FSMContext):

    data = await state.get_data()
    amount = data["amount"]

    async with aiosqlite.connect(DB_NAME) as db:

        await db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?",
            (amount, message.from_user.id)
        )

        cursor = await db.execute(
            "INSERT INTO withdrawals(user_id, amount, username, created_at) VALUES (?, ?, ?, ?)",
            (
                message.from_user.id,
                amount,
                message.text,
                datetime.now().isoformat()
            )
        )

        withdrawal_id = cursor.lastrowid

        await db.commit()

    kb = InlineKeyboardBuilder()

    kb.row(
        InlineKeyboardButton(
            text="✅ Выплачено",
            callback_data=f"wd_ok:{withdrawal_id}"
        ),
        InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"wd_no:{withdrawal_id}"
        )
    )

    text = f"""
💸 Новая заявка на вывод

👤 Пользователь: {message.from_user.id}
💰 Сумма: {amount}
🎰 Username: {message.text}
"""

    await admin_notify(text)

    if ADMIN_GROUP_ID:
        await bot.send_message(
            ADMIN_GROUP_ID,
            text,
            reply_markup=kb.as_markup()
        )

    await message.answer("✅ Заявка отправлена")

    await state.clear()

# ============================================================
# ADMIN
# ============================================================

@dp.message(Command("admin"))
async def admin_handler(message: Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer(
        "⚙ Админ панель",
        reply_markup=admin_menu()
    )


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):

    async with aiosqlite.connect(DB_NAME) as db:

        users = await (await db.execute(
            "SELECT COUNT(*) FROM users"
        )).fetchone()

        tasks = await (await db.execute(
            "SELECT COUNT(*) FROM tasks"
        )).fetchone()

        withdrawals = await (await db.execute(
            "SELECT COUNT(*) FROM withdrawals WHERE status='pending'"
        )).fetchone()

    text = f"""
📊 Статистика

👥 Пользователи: {users[0]}
📋 Задания: {tasks[0]}
💸 Выплаты: {withdrawals[0]}
"""

    await callback.message.edit_text(text)


@dp.callback_query(F.data == "admin_tasks")
async def admin_tasks(callback: CallbackQuery):
    kb = InlineKeyboardBuilder()

    kb.row(
        InlineKeyboardButton(
            text="➕ Создать",
            callback_data="create_task"
        )
    )

    await callback.message.edit_text(
        "📋 Управление заданиями",
        reply_markup=kb.as_markup()
    )


@dp.callback_query(F.data == "create_task")
async def create_task_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminTaskState.title)
    await callback.message.answer("Введите название задания")


@dp.message(AdminTaskState.title)
async def create_task_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(AdminTaskState.description)
    await message.answer("Введите описание")


@dp.message(AdminTaskState.description)
async def create_task_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(AdminTaskState.link)
    await message.answer("Введите ссылку")


@dp.message(AdminTaskState.link)
async def create_task_link(message: Message, state: FSMContext):
    await state.update_data(link=message.text)
    await state.set_state(AdminTaskState.reward)
    await message.answer("Введите награду")


@dp.message(AdminTaskState.reward)
async def create_task_reward(message: Message, state: FSMContext):

    try:
        reward = float(message.text)
    except:
        await message.answer("Введите число")
        return

    await state.update_data(reward=reward)
    await state.set_state(AdminTaskState.type)

    await message.answer(
        "Введите тип задания:\nchannel/group/site/manual/photo/screenshot"
    )


@dp.message(AdminTaskState.type)
async def create_task_type(message: Message, state: FSMContext):

    data = await state.get_data()

    async with aiosqlite.connect(DB_NAME) as db:

        await db.execute(
            """
            INSERT INTO tasks(
                title,
                description,
                link,
                reward,
                type,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                data['title'],
                data['description'],
                data['link'],
                data['reward'],
                message.text,
                datetime.now().isoformat()
            )
        )

        await db.commit()

    await message.answer("✅ Задание создано")
    await state.clear()

# ============================================================
# WITHDRAW MODERATION
# ============================================================

@dp.callback_query(F.data.startswith("wd_ok:"))
async def withdrawal_ok(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):
        return

    wd_id = int(callback.data.split(":")[1])

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE withdrawals SET status = 'paid' WHERE id = ?",
            (wd_id,)
        )
        await db.commit()

    await callback.message.edit_text(
        callback.message.text + "\n\n✅ Выплачено"
    )


@dp.callback_query(F.data.startswith("wd_no:"))
async def withdrawal_no(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):
        return

    wd_id = int(callback.data.split(":")[1])

    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute(
            "SELECT user_id, amount FROM withdrawals WHERE id = ?",
            (wd_id,)
        )

        wd = await cursor.fetchone()

        if wd:
            await db.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (wd[1], wd[0])
            )

        await db.execute(
            "UPDATE withdrawals SET status = 'rejected' WHERE id = ?",
            (wd_id,)
        )

        await db.commit()

    await callback.message.edit_text(
        callback.message.text + "\n\n❌ Отклонено"
    )

# ============================================================
# BAN SYSTEM
# ============================================================

@dp.message(Command("ban"))
async def ban_user(message: Message):

    if not is_admin(message.from_user.id):
        return

    args = message.text.split()

    if len(args) < 2:
        await message.answer("/ban USER_ID")
        return

    user_id = int(args[1])

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET is_banned = 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

    await message.answer("✅ Пользователь забанен")


@dp.message(Command("unban"))
async def unban_user(message: Message):

    if not is_admin(message.from_user.id):
        return

    args = message.text.split()

    if len(args) < 2:
        await message.answer("/unban USER_ID")
        return

    user_id = int(args[1])

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET is_banned = 0 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

    await message.answer("✅ Пользователь разбанен")

# ============================================================
# ERROR HANDLER
# ============================================================

@dp.errors()
async def error_handler(update, exception):
    logger.error(f"ERROR: {exception}")

    with suppress(Exception):
        await admin_notify(f"❌ ERROR\n{exception}")

    return True

# ============================================================
# STARTUP
# ============================================================

async def on_startup():
    await init_db()

    logger.info("DATABASE INITIALIZED")

    with suppress(Exception):
        await admin_notify("🚀 Бот запущен")


async def main():

    await on_startup()

    logger.info("BOT STARTED")

    while True:
        try:
            await dp.start_polling(bot)
        except Exception as e:
            logger.error(f"POLLING ERROR: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("BOT STOPPED")
