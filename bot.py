"""
NFC Subscription Bot
ЮKassa + рекурренты + напоминания за 2 дня до списания
"""

import asyncio
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode

from yookassa import Configuration as YKConfig, Payment as YKPayment

# ═══════════════════════════════════════════════════════════
# ⚙️  КОНФИГ
# ═══════════════════════════════════════════════════════════
load_dotenv()

BOT_TOKEN           = os.getenv("BOT_TOKEN")
ADMIN_ID            = int(os.getenv("ADMIN_ID", "0"))
YOOKASSA_SHOP_ID    = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
RETURN_URL          = os.getenv("RETURN_URL", "https://t.me/your_bot")

YKConfig.account_id = YOOKASSA_SHOP_ID
YKConfig.secret_key = YOOKASSA_SECRET_KEY

DB_PATH       = "bot.db"
REMINDER_DAYS = 2

PLANS = {
    "test": {
        "name": "🧪 Тестовая",
        "price": 2500,
        "is_recurrent": False,  # Разовый платеж
        "desc": (
            "✅ Единоразовое обновление сайта\n"
            "✅ Настройка воронки отзывов\n"
            "✅ Интеграция системы аналитики «Метрика»\n"
            "✅ Создание кастомного сайта для NFC\n\n"
            "<i>Внимание: это разовый платеж за 1 месяц. Без автопродления.</i>"
        )
    },
    "standard": {
        "name": "🟣 Стандарт",
        "price": 5000,
        "is_recurrent": True,
        "desc": (
            "✅ Два ежемесячных обновления сайта (акции, контент)\n"
            "✅ Настройка воронки отзывов\n"
            "✅ Интеграция «Метрика» (расширенная аналитика)\n"
            "✅ Создание кастомного сайта для NFC\n"
            "✅ Мониторинг отзывов"
        )
    },
    "extended": {
        "name": "🟡 Расширенная",
        "price": 7000,
        "is_recurrent": True,
        "desc": (
            "✅ <b>Неограниченное</b> количество обновлений сайта\n"
            "✅ Настройка воронки отзывов\n"
            "✅ Интеграция «Метрика» (расширенная аналитика)\n"
            "✅ Создание кастомного сайта для NFC\n"
            "✅ Мониторинг отзывов\n"
            "✅ <b>Приоритетное обслуживание</b> (менеджеры по запросам)"
        )
    },
    "network": {
        "name": "🏢 Сеть (от 3 точек)",
        "price_per_point": 5000,
        "is_recurrent": True,
        "desc": "Единая подписка на все ваши заведения. Скидка за объем."
    }
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)

router = Router()
class NetworkStates(StatesGroup):
    waiting_for_points = State()
from aiogram.fsm.state import State, StatesGroup


# ═══════════════════════════════════════════════════════════
# 💾 БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════
def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    c = db()
    cur = c.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            full_name   TEXT,
            created_at  TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           INTEGER,
            plan              TEXT,
            status            TEXT,
            payment_method_id TEXT,
            next_payment      TEXT,
            notified_2d       INTEGER DEFAULT 0,
            created_at        TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER,
            payment_id      TEXT UNIQUE,
            subscription_id INTEGER,
            amount          REAL,
            status          TEXT,
            type            TEXT,
            created_at      TEXT
        )
    """)
    c.commit()
    c.close()
    log.info("DB initialized")


def save_user(user):
    c = db()
    c.execute(
        "INSERT OR IGNORE INTO users "
        "(user_id, username, full_name, created_at) VALUES (?,?,?,?)",
        (user.id, user.username or "", user.full_name,
         datetime.now().isoformat())
    )
    c.commit()
    c.close()


def get_subscription(user_id):
    c = db()
    row = c.execute(
        "SELECT id, plan, status, payment_method_id, next_payment "
        "FROM subscriptions WHERE user_id = ? AND status = 'active' "
        "ORDER BY id DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    c.close()
    if not row:
        return None
    return {
        "id": row[0], "plan": row[1], "status": row[2],
        "payment_method_id": row[3], "next_payment": row[4]
    }


def create_subscription(user_id, plan, pm_id):
    next_payment = (datetime.now() + timedelta(days=30)).isoformat()
    c = db()
    cur = c.cursor()
    cur.execute(
        "INSERT INTO subscriptions "
        "(user_id, plan, status, payment_method_id, next_payment, "
        "notified_2d, created_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, plan, "active", pm_id, next_payment, 0,
         datetime.now().isoformat())
    )
    sub_id = cur.lastrowid
    c.commit()
    c.close()
    return sub_id


def cancel_subscription(sub_id):
    c = db()
    c.execute(
        "UPDATE subscriptions SET status = 'cancelled' WHERE id = ?",
        (sub_id,)
    )
    c.commit()
    c.close()


def get_due_subscriptions():
    c = db()
    rows = c.execute(
        "SELECT id, user_id, plan, payment_method_id FROM subscriptions "
        "WHERE status = 'active' AND next_payment <= ?",
        (datetime.now().isoformat(),)
    ).fetchall()
    c.close()
    return [
        {"id": r[0], "user_id": r[1], "plan": r[2], "payment_method_id": r[3]}
        for r in rows
    ]


def get_due_soon():
    now = datetime.now().isoformat()
    threshold = (datetime.now() + timedelta(days=REMINDER_DAYS)).isoformat()
    c = db()
    rows = c.execute(
        "SELECT id, user_id, plan, next_payment FROM subscriptions "
        "WHERE status = 'active' AND notified_2d = 0 "
        "AND next_payment > ? AND next_payment <= ?",
        (now, threshold)
    ).fetchall()
    c.close()
    return [
        {"id": r[0], "user_id": r[1], "plan": r[2], "next_payment": r[3]}
        for r in rows
    ]


def mark_notified(sub_id):
    c = db()
    c.execute(
        "UPDATE subscriptions SET notified_2d = 1 WHERE id = ?",
        (sub_id,)
    )
    c.commit()
    c.close()


def update_next_payment(sub_id):
    next_payment = (datetime.now() + timedelta(days=30)).isoformat()
    c = db()
    c.execute(
        "UPDATE subscriptions SET next_payment = ?, notified_2d = 0 "
        "WHERE id = ?",
        (next_payment, sub_id)
    )
    c.commit()
    c.close()


def save_payment(user_id, payment_id, sub_id, amount, status, ptype):
    c = db()
    c.execute(
        "INSERT OR REPLACE INTO payments "
        "(user_id, payment_id, subscription_id, amount, status, type, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, payment_id, sub_id, amount, status, ptype,
         datetime.now().isoformat())
    )
    c.commit()
    c.close()


def set_payment_status(payment_id, status):
    c = db()
    c.execute(
        "UPDATE payments SET status = ? WHERE payment_id = ?",
        (status, payment_id)
    )
    c.commit()
    c.close()


def get_stats():
    c = db()
    active = c.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE status = 'active'"
    ).fetchone()[0]
    
    revenue = c.execute(
        "SELECT COALESCE(SUM(amount),0) FROM payments WHERE status = 'succeeded'"
    ).fetchone()[0]
    
    # Считаем MRR (ежемесячный доход) в Python, чтобы учесть сети
    subs = c.execute("SELECT plan FROM subscriptions WHERE status = 'active'").fetchall()
    mrr = 0
    for row in subs:
        plan = row[0]
        if plan.startswith("network_"):
            points = int(plan.split("_")[1])
            mrr += points * PLANS["network"]["price_per_point"]
        elif plan in PLANS:
            mrr += PLANS[plan]["price"]
            
    c.close()
    return {"active": active, "revenue": revenue, "mrr": mrr}


# ═══════════════════════════════════════════════════════════
# 💳 ЮKASSA
# ═══════════════════════════════════════════════════════════
def create_initial_payment(user_id, plan_key):
    plan = PLANS[plan_key]
    payment = YKPayment.create({
        "amount": {"value": f"{plan['price']:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": RETURN_URL},
        "save_payment_method": False,  # 👈 ВРЕМЕННО FALSE (ждем разблокировки от ЮKassa)
        "description": f"Оплата тарифа {plan['name']}",
        "metadata": {"user_id": user_id, "plan": plan_key}
    }, str(uuid.uuid4()))
    save_payment(user_id, payment.id, None, plan["price"], payment.status, "initial")
    return payment

def create_custom_payment(user_id, plan_key, amount, points):
    plan = PLANS[plan_key]
    payment = YKPayment.create({
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": RETURN_URL},
        "save_payment_method": False,  # 👈 ВРЕМЕННО FALSE
        "description": f"Подписка Сеть ({points} точек)",
        "metadata": {"user_id": user_id, "plan": f"{plan_key}_{points}"}
    }, str(uuid.uuid4()))
    save_payment(user_id, payment.id, None, amount, payment.status, "initial")
    return payment

def create_recurring_payment(user_id, sub_id, plan_key, pm_id):
    plan = PLANS[plan_key]
    payment = YKPayment.create({
        "amount": {"value": f"{plan['price']:.2f}", "currency": "RUB"},
        "capture": True,
        "payment_method_id": pm_id,
        "description": f"Подписка {plan['name']} — продление",
        "metadata": {"user_id": user_id, "subscription_id": sub_id}
    }, str(uuid.uuid4()))
    save_payment(user_id, payment.id, sub_id, plan["price"],
                 payment.status, "recurring")
    return payment


async def poll_payment(payment_id, timeout_sec=600):
    for _ in range(timeout_sec // 3):
        await asyncio.sleep(3)
        try:
            p = YKPayment.find_one(payment_id)
            if p.status in ("succeeded", "canceled"):
                return p
        except Exception as e:
            log.warning(f"poll error: {e}")
    return None


# ═══════════════════════════════════════════════════════════
# 🎛 КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Тарифы и подписка", callback_data="plans")],
        
        [InlineKeyboardButton(
            text="📊 Моя подписка", 
            web_app=WebAppInfo(url="https://telegram-web-app-tik.netlify.app/tma.html")
        )],
        
        [InlineKeyboardButton(text="ℹ️ Что входит", callback_data="info")],
        [InlineKeyboardButton(text="📄 Оферта", callback_data="offer")],
    ])


def kb_plans():
    rows = []
    for k, p in PLANS.items():
        if k == "network":
            continue
        rows.append([InlineKeyboardButton(
            text=f"{p['name']} — {p['price']} ₽/мес",
            callback_data=f"plan:{k}"
        )])
    
    network_plan = PLANS["network"]
    rows.append([
        InlineKeyboardButton(
            text=f"🏢 {network_plan['name']} — {network_plan['price_per_point']} ₽/точка", 
            callback_data="plan:network"
        )
    ])
    
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_plan_details(key):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оформить подписку",
                              callback_data=f"pay:{key}")],
        [InlineKeyboardButton(text="⬅️ К тарифам", callback_data="plans")],
    ])


def kb_cancel_sub(sub_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить подписку",
                              callback_data=f"cancel:{sub_id}")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back")],
    ])


def kb_reminder(sub_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Всё ок, оставить",
                              callback_data="keep")],
        [InlineKeyboardButton(text="❌ Отменить подписку",
                              callback_data=f"cancel:{sub_id}")],
    ])


def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="a_stats")],
        [InlineKeyboardButton(text="👥 Подписчики", callback_data="a_subs")],
        [InlineKeyboardButton(text="💰 Платежи", callback_data="a_pays")],
    ])


# ═══════════════════════════════════════════════════════════
# 👤 ПОЛЬЗОВАТЕЛЬСКИЕ ХЕНДЛЕРЫ
# ═══════════════════════════════════════════════════════════
@router.message(CommandStart())
async def cmd_start(m: Message):
    save_user(m.from_user)
    await m.answer(
        f"👋 Привет, <b>{m.from_user.first_name}</b>!\n\n"
        "Я бот по управлению вашей подписки.\n\n"
        "✅ Создание кастомного сайта для направления NFC\n"
        "✅ Присылаем отчёт с цифрами каждый месяц\n"
        "✅ Настраиваем воронку отзывов и мониторим их\n"
        "✅ Отмена — в один клик\n\n"
        "Выбери действие 👇",
        reply_markup=kb_main()
    )


@router.callback_query(F.data == "plans")
async def cb_plans(c: CallbackQuery):
    text = (
        "💎 <b>Тарифы подписки</b>\n\n"
        "Выберите подходящий тариф для вашего заведения:\n\n"
        + "\n\n".join(
            f"{p['name']} — <b>{p['price']} ₽/мес</b>\n{p['desc']}"
            for k, p in PLANS.items() if k != "network"
        )
        + "\n\n🏢 <b>Для сетей от 3 точек</b> — 5000 ₽/точка\n\n"
        "<i>Нажмите на тариф ниже, чтобы узнать, что входит:</i>"
    )
    await c.message.edit_text(text, reply_markup=kb_plans())
    await c.answer()
    
async def cb_plan(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 1)[1]
    
    # Если выбрали Сеть
    if key == "network":
        await c.message.edit_text(
            "🏢 <b>Тариф Сеть</b>\n\n"
            "Напишите цифрой, <b>сколько у вас точек/заведений</b>? (минимум 3)\n\n"
            "Например: <code>13</code>"
        )
        await state.set_state(NetworkStates.waiting_for_points)
        await c.answer()
        return

    p = PLANS[key]
    text = (
        f"{p['name']}\n\n{p['desc']}\n\n"
        f"💰 <b>{p['price']} ₽/мес</b>\n\n"
        "✅ Первый платёж — сейчас\n"
        "✅ Далее списание раз в 30 дней\n"
        "✅ Отменить можно в любой момент"
    )
    await c.message.edit_text(text, reply_markup=kb_plan_details(key))
    await c.answer()

@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 1)[1]
    
    # Если выбрали Сеть
    if key == "network":
        await c.message.edit_text(
            "🏢 <b>Тариф Сеть</b>\n\n"
            "Напишите цифрой, <b>сколько у вас точек/заведений</b>? (минимум 3)\n\n"
            "Например: <code>13</code>"
        )
        await state.set_state(NetworkStates.waiting_for_points)
        await c.answer()
        return

    # Для обычных тарифов
    p = PLANS[key]
    text = (
        f"{p['name']}\n\n{p['desc']}\n\n"
        f"💰 <b>{p['price']} ₽/мес</b>\n\n"
        "✅ Первый платёж — сейчас\n"
        "✅ Далее списание раз в 30 дней\n"
        "✅ Отменить можно в любой момент"
    )
@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 1)[1]
    
    # Если выбрали Сеть
    if key == "network":
        await c.message.edit_text(
            "🏢 <b>Тариф Сеть</b>\n\n"
            "Напишите цифрой, <b>сколько у вас точек/заведений</b>? (минимум 3)\n\n"
            "Например: <code>13</code>"
        )
        await state.set_state(NetworkStates.waiting_for_points)
        await c.answer()
        return

    # Для обычных тарифов
    p = PLANS[key]
    text = (
        f"{p['name']}\n\n{p['desc']}\n\n"
        f"💰 <b>{p['price']} ₽/мес</b>\n\n"
        "✅ Первый платёж — сейчас\n"
        "✅ Далее списание раз в 30 дней\n"
        "✅ Отменить можно в любой момент"
    )
    await c.message.edit_text(text, reply_markup=kb_plan_details(key))
    await c.answer()


@router.message(NetworkStates.waiting_for_points)
async def process_network_points(m: Message, state: FSMContext):
    if not m.text.isdigit():
        await m.answer("⚠️ Пожалуйста, отправьте просто число (например: 5).")
        return

    points = int(m.text)
    if points < 3:
        await m.answer("⚠️ Минимальное количество для тарифа Сеть — 3 точки.")
        return

    total_price = points * PLANS['network']['price_per_point']
    await state.update_data(points=points, total_price=total_price)
    
    text = (
        f"🏢 <b>Расчет для сети</b>\n\n"
        f"Количество точек: <b>{points}</b>\n"
        f"Цена за точку: <b>{PLANS['network']['price_per_point']} ₽</b>\n"
        f"Итого: <b>{total_price} ₽/мес</b>\n\n"
        "Нажмите кнопку ниже, чтобы перейти к оплате."
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {total_price} ₽", callback_data=f"pay_network:{points}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back")]
    ])
    
    await m.answer(text, reply_markup=kb)
    await state.clear()

@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(c: CallbackQuery):
    key = c.data.split(":", 1)[1]
    if get_subscription(c.from_user.id):
        await c.answer("У вас уже есть активная подписка", show_alert=True)
        return

    await c.answer("Создаю платёж...")
    try:
        payment = create_initial_payment(c.from_user.id, key)
    except Exception as e:
        log.exception("Payment create error")
        await c.message.edit_text(f"❌ Ошибка: {e}", reply_markup=kb_plans())
        return

    p = PLANS[key]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить",
                              url=payment.confirmation.confirmation_url)],
    ])
    await c.message.edit_text(
        f"💳 <b>Счёт на {p['price']} ₽</b>\n\n"
        f"Тариф: {p['name']}\n\n"
        "1. Нажми «Оплатить»\n"
        "2. Оплати картой или через СБП\n"
        "3. Вернись сюда — активирую автоматически",
        reply_markup=kb
    )
    asyncio.create_task(
        _watch_payment(c.bot, c.from_user.id, payment.id, key,
                       c.message.chat.id)
    )


async def _watch_payment(bot, user_id, payment_id, plan_key, chat_id):
    p = await poll_payment(payment_id)
    if not p:
        await bot.send_message(chat_id, "⏰ Время ожидания истекло.")
        return

    if p.status == "canceled":
        set_payment_status(payment_id, "canceled")
        await bot.send_message(chat_id, "❌ Платёж отменён.")
        return

    set_payment_status(payment_id, "succeeded")
    pm_id = getattr(p.payment_method, "id", None)
    sub_id = create_subscription(user_id, plan_key, pm_id)
    
    # Проверяем, сеть это или обычный тариф
    if plan_key.startswith("network_"):
        points = int(plan_key.split("_")[1])
        price = points * PLANS["network"]["price_per_point"]
        name = f"Сеть ({points} точек)"
        is_recurrent = True
    else:
        plan = PLANS[plan_key]
        price = plan["price"]
        name = plan["name"]
        is_recurrent = plan.get("is_recurrent", True)

    if is_recurrent:
        next_date = (datetime.now() + timedelta(days=30)).strftime("%d.%m.%Y")
        text = (
            f"🎉 <b>Подписка активирована!</b>\n\n"
            f"Тариф: {name}\n"
            f"Стоимость: {price} ₽/мес\n\n"
            "Отменить можно через «Моя подписка» в любой момент."
        )
    else:
        text = (
            f"🎉 <b>Тестовый доступ активирован!</b>\n\n"
            f"Тариф: {name}\n"
            f"Стоимость: {price} ₽\n"
            "Оплачен 1 месяц. <b>Автопродления не будет.</b>\n\n"
            "Спасибо, что решили попробовать!"
        )

    await bot.send_message(chat_id, text)

    try:
        await bot.send_message(
            ADMIN_ID,
            f"💳 <b>Новая оплата!</b>\n"
            f"User: <code>{user_id}</code>\n"
            f"Тариф: {name} ({price} ₽)"
        )
    except Exception:
        pass


@router.callback_query(F.data == "my_sub")
async def cb_my_sub(c: CallbackQuery):
    sub = get_subscription(c.from_user.id)
    if not sub:
        await c.message.edit_text(
            "У вас нет активной подписки.\n\nВыбери тариф:",
            reply_markup=kb_plans()
        )
        await c.answer()
        return

    plan = PLANS[sub["plan"]]
    next_pay = datetime.fromisoformat(sub["next_payment"]).strftime("%d.%m.%Y")
    reminder = (datetime.fromisoformat(sub["next_payment"])
                - timedelta(days=REMINDER_DAYS)).strftime("%d.%m.%Y")
    text = (
        f"📊 <b>Ваша подписка</b>\n\n"
        f"Тариф: {plan['name']}\n"
        f"Стоимость: {plan['price']} ₽/мес\n"
        f"Статус: ✅ Активна\n"
        f"Следующее списание: <b>{next_pay}</b>\n"
        f"Напоминание придёт: <b>{reminder}</b>"
    )
    await c.message.edit_text(text, reply_markup=kb_cancel_sub(sub["id"]))
    await c.answer()


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(c: CallbackQuery):
    sub_id = int(c.data.split(":", 1)[1])
    cancel_subscription(sub_id)
    await c.message.edit_text(
        "✅ Подписка отменена.\n\n"
        "Доступ сохранится до конца оплаченного периода. "
        "Списаний больше не будет.",
        reply_markup=kb_main()
    )
    try:
        await c.bot.send_message(
            ADMIN_ID,
            f"⚠️ Отмена подписки\nUser: <code>{c.from_user.id}</code>"
        )
    except Exception:
        pass
    await c.answer()


@router.callback_query(F.data == "keep")
async def cb_keep(c: CallbackQuery):
    await c.message.edit_text(
        "👍 Отлично! Подписка остаётся активной.\n"
        "Списание пройдёт в назначенную дату."
    )
    await c.answer()


@router.callback_query(F.data == "info")
async def cb_info(c: CallbackQuery):
    await c.message.edit_text(
        "ℹ️ <b>Что входит в подписку</b>\n\n"
        "✨ Создание кастомного сайта для направления NFC\n"
        "📝 Обновление меню и акций\n"
        "📊 Ежемесячный отчёт: тапы, клики, отзывы\n"
        "🎨 Сезонный редизайн (Расширеная)\n"
        "🔧 Приоритетная поддержка\n\n"
        f"🔔 За {REMINDER_DAYS} дня до списания — уведомление\n\n"
        "🔒 Оплата через ЮKassa (карта или СБП)",
        reply_markup=kb_main()
    )
    await c.answer()


@router.callback_query(F.data == "offer")
async def cb_offer(c: CallbackQuery):
    await c.message.edit_text(
        "📄 <b>Публичная оферта</b>\n\n"
        "1. Исполнитель: [Анисимов Иван Андреевич], самозанятый, ИНН [781637567093]\n"
        "2. Предмет: ведение NFC-страницы клиента\n"
        "3. Стоимость: по выбранному тарифу\n"
        "4. Оплата: автоматическое списание раз в 30 дней\n"
        "5. Отмена: в любой момент в боте\n"
        "6. Акцепт оферты = оплата подписки\n\n"
        "<i>Полный текст: [https://telegra.ph/Publichnaya-oferta-na-okazanie-uslug-11-05-2]</i>",
        reply_markup=kb_main()
    )
    await c.answer()


# ═══════════════════════════════════════════════════════════
# 🛡 АДМИН
# ═══════════════════════════════════════════════════════════
@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer("🛡 <b>Админ-панель</b>", reply_markup=kb_admin())


@router.callback_query(F.data == "a_stats")
async def cb_a_stats(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    s = get_stats()
    await c.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"Активных подписок: <b>{s['active']}</b>\n"
        f"MRR: <b>{s['mrr']:.0f} ₽/мес</b>\n"
        f"Всего получено: <b>{s['revenue']:.0f} ₽</b>",
        reply_markup=kb_admin()
    )
    await c.answer()


@router.callback_query(F.data == "a_subs")
async def cb_a_subs(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    conn = db()
    rows = conn.execute(
        "SELECT user_id, plan, next_payment, notified_2d "
        "FROM subscriptions WHERE status = 'active' "
        "ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()
    if not rows:
        text = "Нет активных подписок."
    else:
        text = "👥 <b>Активные подписки</b>\n\n" + "\n".join(
            f"• <code>{uid}</code> — {PLANS[plan]['name']} — "
            f"до {datetime.fromisoformat(nxt).strftime('%d.%m.%Y')} "
            f"{'🔔' if notif else ''}"
            for uid, plan, nxt, notif in rows
        )
    await c.message.edit_text(text, reply_markup=kb_admin())
    await c.answer()


@router.callback_query(F.data == "a_pays")
async def cb_a_pays(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    conn = db()
    rows = conn.execute(
        "SELECT user_id, amount, status, type, created_at FROM payments "
        "ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()
    if not rows:
        text = "Нет платежей."
    else:
        text = "💰 <b>Платежи</b>\n\n" + "\n".join(
            f"{'✅' if st == 'succeeded' else '⏳' if st == 'pending' else '❌'} "
            f"{amt:.0f}₽ | {tp} | <code>{uid}</code> | "
            f"{datetime.fromisoformat(dt).strftime('%d.%m %H:%M')}"
            for uid, amt, st, tp, dt in rows
        )
    await c.message.edit_text(text, reply_markup=kb_admin())
    await c.answer()


@router.message(Command("charge"))
async def cmd_charge(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer("🔄 Проверяю подписки...")
    n = await charge_due(m.bot)
    await m.answer(f"✅ Обработано: {n}")


@router.message(Command("remind"))
async def cmd_remind(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer("🔔 Отправляю напоминания...")
    n = await send_reminders(m.bot)
    await m.answer(f"✅ Отправлено: {n}")


# ═══════════════════════════════════════════════════════════
# 🔔 НАПОМИНАНИЯ + 🔁 СПИСАНИЯ
# ═══════════════════════════════════════════════════════════
async def send_reminders(bot: Bot):
    subs = get_due_soon()
    sent = 0
    for s in subs:
        plan = PLANS[s["plan"]]
        pay_date = datetime.fromisoformat(s["next_payment"])
        days_left = (pay_date - datetime.now()).days
        word = "день" if days_left == 1 else "дня" if 2 <= days_left <= 4 else "дней"

        try:
            await bot.send_message(
                s["user_id"],
                f"🔔 <b>Скоро списание</b>\n\n"
                f"Через <b>{days_left} {word}</b> "
                f"({pay_date.strftime('%d.%m.%Y')}) с вашего счёта "
                f"будет списано <b>{plan['price']} ₽</b> "
                f"за подписку {plan['name']}.\n\n"
                "Если не хотите продлевать — отмените кнопкой ниже. "
                "Списание не произойдёт, доступ сохранится до "
                "конца оплаченного периода.",
                reply_markup=kb_reminder(s["id"])
            )
            mark_notified(s["id"])
            sent += 1
        except Exception as e:
            log.warning(f"Reminder failed for {s['user_id']}: {e}")
    return sent


async def charge_due(bot: Bot):
    subs = get_due_subscriptions()
    count = 0
    for s in subs:
        try:
            if not PLANS[s["plan"]].get("is_recurrent", True):
                continue
                
            if not s["payment_method_id"]:
                continue
            p = create_recurring_payment(
                s["user_id"], s["id"], s["plan"], s["payment_method_id"]
            )
            result = await poll_payment(p.id, timeout_sec=120)

            if result and result.status == "succeeded":
                set_payment_status(p.id, "succeeded")
                update_next_payment(s["id"])
                count += 1
                next_date = (datetime.now() +
                             timedelta(days=30)).strftime("%d.%m.%Y")
                try:
                    await bot.send_message(
                        s["user_id"],
                        f"✅ <b>Подписка продлена</b>\n\n"
                        f"Списано: {PLANS[s['plan']]['price']} ₽\n"
                        f"Следующее списание: <b>{next_date}</b>\n"
                        f"Напоминание придёт за {REMINDER_DAYS} дня."
                    )
                except Exception:
                    pass
            else:
                set_payment_status(p.id, "canceled")
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ Не удалось списать с {s['user_id']} "
                        f"(подписка #{s['id']})"
                    )
                except Exception:
                    pass
        except Exception as e:
            log.exception(f"Charge error #{s['id']}: {e}")
    return count


# ═══════════════════════════════════════════════════════════
# 🔄 ЦИКЛЫ
# ═══════════════════════════════════════════════════════════
async def billing_loop(bot: Bot):
    while True:
        try:
            n = await charge_due(bot)
            if n:
                log.info(f"Charged {n} subs")
        except Exception as e:
            log.exception(f"billing_loop: {e}")
        await asyncio.sleep(6 * 3600)


async def reminder_loop(bot: Bot):
    while True:
        try:
            n = await send_reminders(bot)
            if n:
                log.info(f"Sent {n} reminders")
        except Exception as e:
            log.exception(f"reminder_loop: {e}")
        await asyncio.sleep(3600)

# ═══════════════════════════════════════════════════════════
# ФУНКЦИИ ДЛЯ СЕТИ И ОТМЕНЫ
# ═══════════════════════════════════════════════════════════

@router.callback_query(F.data == "back")
async def cb_back(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(
        "🏠 <b>Главное меню</b>", 
        reply_markup=kb_main()
    )
    await c.answer()


@router.callback_query(F.data.startswith("pay_network:"))
async def cb_pay_network(c: CallbackQuery, state: FSMContext):
    # Достаем количество точек прямо из кнопки
    points = int(c.data.split(":")[1])
    total_price = points * PLANS['network']['price_per_point']
    
    await c.answer("Создаю платёж...")
    
    try:
        payment = create_custom_payment(c.from_user.id, "network", total_price, points)
    except Exception as e:
        log.exception("Payment create error")
        await c.message.edit_text(f"❌ Ошибка: {e}")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=payment.confirmation.confirmation_url)],
    ])
    await c.message.edit_text(
        f"💳 <b>Счёт на {total_price} ₽</b>\n\n"
        "1. Нажми «Оплатить»\n"
        "2. Оплати картой\n"
        "3. Вернись сюда — активирую подписку.",
        reply_markup=kb
    )
    
    asyncio.create_task(_watch_payment(c.bot, c.from_user.id, payment.id, f"network_{points}", c.message.chat.id))

@router.callback_query(F.data == "back")
async def cb_back(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(
        "🏠 <b>Главное меню</b>", 
        reply_markup=kb_main()
    )
    await c.answer()


def create_custom_payment(user_id, plan_key, amount, points):
    plan = PLANS[plan_key]
    payment = YKPayment.create({
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": RETURN_URL},
        "save_payment_method": False,
        "description": f"Подписка Сеть ({points} точек)",
        "metadata": {"user_id": user_id, "plan": f"{plan_key}_{points}"}
    }, str(uuid.uuid4()))
    save_payment(user_id, payment.id, None, amount, payment.status, "initial")
    return payment

# ═══════════════════════════════════════════════════════════
# 🚀 ЗАПУСК
# ═══════════════════════════════════════════════════════════
async def main():
    init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    asyncio.create_task(billing_loop(bot))
    asyncio.create_task(reminder_loop(bot))

    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
