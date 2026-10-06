# -*- coding: utf-8 -*-
# Бот продажи доступа — v3.
# Вход через мини-приложение: /start открывает приложение-витрину.
# В приложении клиент выбирает тариф и жмёт "Оплатить" -> бот получает выбор,
# показывает реквизиты + сумму и ждёт чек -> по кнопке "Выдать" ключ уходит клиенту.

import asyncio
import json
import random
import string
import datetime as dt

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo,
)

import config

BOT_TOKEN  = config.BOT_TOKEN
ADMIN_ID   = int(getattr(config, "ADMIN_ID", 0))
CARD       = getattr(config, "CARD",      "0000 0000 0000 0000")
RECIPIENT  = getattr(config, "RECIPIENT", "ПОЛУЧАТЕЛЬ · БАНК")
BRAND      = getattr(config, "BRAND",     "Bez_granits")
SUPPORT    = getattr(config, "SUPPORT",   "@your_support")
DB_PATH    = getattr(config, "DB_PATH",   "bot.db")
WEBAPP_URL = getattr(config, "WEBAPP_URL", "")   # https-ссылка на мини-приложение

# Тарифы: код -> (название, дней, цена ₽)
TARIFFS = {
    "m1": ("1 месяц",   30,  150),
    "m3": ("3 месяца",  90,  400),
    "m6": ("6 месяцев", 180, 750),
}

CONNECT = (
    "📲 <b>Как подключить</b>\n\n"
    "1. Установи приложение: <b>iPhone — Incy</b>, <b>Android и ПК — Happ</b>.\n"
    "2. Открой приложение → «+» → вставь полученный ключ из буфера.\n"
    "3. Нажми «Подключить» — готово ✅\n\n"
    "Одна подписка работает до 2 устройств одновременно."
)

try:
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
except TypeError:
    bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


class Buy(StatesGroup):
    waiting_receipt = State()


# ============== БАЗА ==============
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS keys(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            value TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            used_by INTEGER,
            used_at TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS orders(
            code TEXT PRIMARY KEY,
            user_id INTEGER,
            username TEXT,
            plan TEXT,
            amount INTEGER,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT,
            key_value TEXT
        )""")
        cur = await db.execute("PRAGMA table_info(orders)")
        cols = [r[1] for r in await cur.fetchall()]
        if "plan" not in cols:
            await db.execute("ALTER TABLE orders ADD COLUMN plan TEXT")
        await db.commit()


async def keys_free_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM keys WHERE used=0")
        (n,) = await cur.fetchone()
        return n


async def add_keys(values) -> int:
    added = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for v in values:
            v = v.strip()
            if not v:
                continue
            await db.execute("INSERT INTO keys(value) VALUES(?)", (v,))
            added += 1
        await db.commit()
    return added


async def take_key(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, value FROM keys WHERE used=0 ORDER BY id LIMIT 1")
        row = await cur.fetchone()
        if not row:
            return None
        kid, val = row
        await db.execute(
            "UPDATE keys SET used=1, used_by=?, used_at=? WHERE id=?",
            (user_id, dt.datetime.now().isoformat(timespec="seconds"), kid),
        )
        await db.commit()
        return val


async def order_create(code, user_id, username, plan, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO orders(code,user_id,username,plan,amount,status,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (code, user_id, username, plan, amount, "wait_receipt",
             dt.datetime.now().isoformat(timespec="seconds")),
        )
        await db.commit()


async def order_get(code):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT code,user_id,username,plan,amount,status,key_value FROM orders WHERE code=?",
            (code,))
        return await cur.fetchone()


async def order_set(code, status, key_value=None):
    async with aiosqlite.connect(DB_PATH) as db:
        if key_value is None:
            await db.execute("UPDATE orders SET status=? WHERE code=?", (status, code))
        else:
            await db.execute("UPDATE orders SET status=?, key_value=? WHERE code=?",
                             (status, key_value, code))
        await db.commit()


async def user_keys(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT value, used_at FROM keys WHERE used_by=? ORDER BY used_at DESC", (user_id,))
        return await cur.fetchall()


async def stats():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM orders WHERE status='paid'")
        sold, revenue = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM keys WHERE used=0")
        (free,) = await cur.fetchone()
        return sold, revenue, free


def new_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=5))


# ============== КЛАВИАТУРЫ ==============
def start_kb():
    # Если приложение размещено (есть https-ссылка) — кнопка открытия мини-аппа.
    if WEBAPP_URL.startswith("https"):
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=f"🚀 Открыть {BRAND}",
                                      web_app=WebAppInfo(url=WEBAPP_URL))]],
            resize_keyboard=True,
        )
    return None


def tariffs_kb() -> InlineKeyboardMarkup:
    rows = []
    for code, (title, days, price) in TARIFFS.items():
        rows.append([InlineKeyboardButton(text=f"{title} — {price} ₽",
                                          callback_data=f"plan:{code}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pay_kb(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{code}")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"cancel:{code}")],
    ])


def admin_kb(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выдать ключ", callback_data=f"issue:{code}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{code}")],
    ])


# ============== ОБЩЕЕ: показать реквизиты оплаты ==============
async def show_payment(target: Message, user_id, username, plan, state: FSMContext):
    if plan not in TARIFFS:
        await target.answer("Тариф не найден, открой приложение заново.")
        return
    title, days, price = TARIFFS[plan]
    code = new_code()
    await order_create(code, user_id, username or "—", plan, price)
    await state.set_state(Buy.waiting_receipt)
    await state.update_data(code=code)
    await target.answer(
        f"💳 <b>Оплата — {title}</b>\n\n"
        f"Сумма: <b>{price} ₽</b>\n"
        f"Срок: <b>{days} дней</b>\n"
        f"🔑 Доступ на <b>до 2 устройств</b>\n\n"
        f"Переведи точную сумму на карту:\n"
        f"<code>{CARD}</code>\n"
        f"Получатель: {RECIPIENT}\n\n"
        f"🔖 Код заказа: <b>{code}</b>\n\n"
        f"⚡️ После перевода нажми «✅ Я оплатил» и пришли чек — выдадим сразу после проверки.",
        reply_markup=pay_kb(code),
    )


# ============== ПОЛЬЗОВАТЕЛЬ ==============
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    kb = start_kb()
    if kb:
        await m.answer(
            f"👋 <b>{BRAND}</b>\n\n"
            f"🚀 Быстрый доступ в интернет без ограничений.\n"
            f"🔒 Защищённое соединение · до 2 устройств.\n\n"
            f"Нажми кнопку ниже, чтобы открыть приложение 👇",
            reply_markup=kb,
        )
    else:
        # приложение ещё не размещено — показываем тарифы прямо тут
        await m.answer(
            f"👋 <b>{BRAND}</b>\n\nВыбери тариф:",
            reply_markup=tariffs_kb(),
        )


# Данные из мини-приложения (нажатие "Оплатить")
@router.message(F.web_app_data)
async def from_webapp(m: Message, state: FSMContext):
    try:
        data = json.loads(m.web_app_data.data)
    except Exception:
        await m.answer("Не понял выбор, открой приложение заново.")
        return
    if data.get("action") == "buy":
        await show_payment(m, m.from_user.id, m.from_user.username, data.get("plan"), state)


# Запасной путь выбора тарифа (если приложение ещё не подключено)
@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(c: CallbackQuery, state: FSMContext):
    plan = c.data.split(":", 1)[1]
    await show_payment(c.message, c.from_user.id, c.from_user.username, plan, state)
    await c.answer()


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await c.message.answer("Заказ отменён. Открой приложение, чтобы оформить заново.")
    await c.answer()


@router.callback_query(F.data.startswith("paid:"))
async def cb_paid(c: CallbackQuery, state: FSMContext):
    code = c.data.split(":", 1)[1]
    await state.set_state(Buy.waiting_receipt)
    await state.update_data(code=code)
    await c.message.answer(
        "📎 Пришли <b>чек об оплате</b> — фото или файлом (скрин/PDF).\n"
        "Как только проверим перевод, ключ придёт сюда автоматически ⚡️"
    )
    await c.answer()


@router.message(Buy.waiting_receipt, F.photo | F.document)
async def got_receipt(m: Message, state: FSMContext):
    data = await state.get_data()
    code = data.get("code")
    if not code:
        await m.answer("Сначала выбери тариф в приложении.")
        return
    order = await order_get(code)
    plan = order[3] if order else "-"
    amount = order[4] if order else 0
    await order_set(code, "review")
    await state.clear()
    uname = ("@" + m.from_user.username) if m.from_user.username else "без username"
    title = TARIFFS.get(plan, ("?",))[0]
    caption = (
        f"💸 <b>Новая оплата — проверь чек</b>\n"
        f"Пользователь: {uname}\n"
        f"ID: <code>{m.from_user.id}</code>\n"
        f"Тариф: {title}\n"
        f"Сумма: <b>{amount} ₽</b>\n"
        f"Код заказа: <code>{code}</code>"
    )
    if m.photo:
        await bot.send_photo(ADMIN_ID, m.photo[-1].file_id, caption=caption,
                             reply_markup=admin_kb(code))
    else:
        await bot.send_document(ADMIN_ID, m.document.file_id, caption=caption,
                                reply_markup=admin_kb(code))
    await m.answer("✅ Чек получен! Проверяем оплату, ключ придёт сюда автоматически 🙌")


@router.message(Buy.waiting_receipt)
async def waiting_receipt_other(m: Message):
    await m.answer("Жду именно чек — пришли его фото или файлом 📎")


@router.message(Command("mykeys"))
async def mykeys(m: Message):
    rows = await user_keys(m.from_user.id)
    if not rows:
        await m.answer("У тебя пока нет ключей.")
        return
    text = "🔑 <b>Твои ключи:</b>\n\n" + "\n\n".join(
        f"<code>{v}</code>\n<i>выдан: {t}</i>" for v, t in rows
    )
    await m.answer(text)


@router.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(CONNECT)


# ============== АДМИН ==============
@router.callback_query(F.data.startswith("issue:"))
async def cb_issue(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Недоступно", show_alert=True)
        return
    code = c.data.split(":", 1)[1]
    order = await order_get(code)
    if not order:
        await c.answer("Заказ не найден", show_alert=True)
        return
    _, user_id, username, plan, amount, status, key_value = order
    if status == "paid":
        await c.answer("Уже выдан", show_alert=True)
        return
    key = await take_key(user_id)
    if not key:
        await c.answer("Пул пуст! Добавь ключи через /addkey и нажми снова.", show_alert=True)
        return
    await order_set(code, "paid", key_value=key)
    title = TARIFFS.get(plan, ("доступ",))[0]
    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"Тариф: <b>{title}</b>\n"
            f"Твой ключ (до 2 устройств):\n<code>{key}</code>\n\n"
            f"Подключение: iPhone — <b>Incy</b>, Android и ПК — <b>Happ</b>. "
            f"Открой приложение → «+» → вставь ключ.\n"
            f"Спасибо за покупку 🙌",
        )
    except Exception:
        pass
    await c.message.edit_caption(
        caption=(c.message.caption or "") + f"\n\n✅ ВЫДАНО: <code>{key}</code>"
    )
    await c.answer("Ключ выдан клиенту")


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Недоступно", show_alert=True)
        return
    code = c.data.split(":", 1)[1]
    order = await order_get(code)
    if not order:
        await c.answer("Заказ не найден", show_alert=True)
        return
    user_id = order[1]
    await order_set(code, "rejected")
    try:
        await bot.send_message(
            user_id,
            "❌ Оплату найти не удалось. Проверь перевод и пришли корректный чек, "
            f"либо напиши в поддержку {SUPPORT}.",
        )
    except Exception:
        pass
    await c.message.edit_caption(caption=(c.message.caption or "") + "\n\n❌ ОТКЛОНЕНО")
    await c.answer("Клиенту отправлен отказ")


@router.message(Command("addkey"))
async def addkey(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    if "\n" in m.text:
        lines = m.text.split("\n")[1:]
    else:
        parts = m.text.split(maxsplit=1)
        lines = [parts[1]] if len(parts) > 1 else []
    if not lines:
        await m.answer("Пришли так:\n<code>/addkey</code>\nКЛЮЧ1\nКЛЮЧ2\nКЛЮЧ3")
        return
    n = await add_keys(lines)
    free = await keys_free_count()
    await m.answer(f"Добавлено ключей: {n}\nСвободно в пуле: {free}")


@router.message(Command("stock"))
async def stock(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer(f"Свободных ключей в пуле: {await keys_free_count()}")


@router.message(Command("stats"))
async def stats_cmd(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    sold, revenue, free = await stats()
    await m.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"Продано: <b>{sold}</b>\n"
        f"Выручка: <b>{revenue} ₽</b>\n"
        f"Ключей в пуле: <b>{free}</b>"
    )


async def main():
    await db_init()
    print("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
