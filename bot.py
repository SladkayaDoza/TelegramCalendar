# -*- coding: utf-8 -*-
"""
Telegram-бот «Календар конференцій».
- Дозволяє створити подію: конкретна дата АБО правило "кожна N-ша <день тижня> місяця".
- Зберігає події у SQLite.
- Нагадує за 1 день до початку, а потім надсилає посилання на конференцію
  за N хвилин до початку.

Усі часові розрахунки — за локальним часом сервера.
"""

import asyncio
import calendar
import datetime
import logging
import os
import sqlite3

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "СЮДИ_ВСТАВТЕ_ТОКЕН")
# за скільки хвилин до початку надсилати посилання
REMIND_MINUTES = int(os.getenv("REMIND_MINUTES", "15"))
DB_PATH = "events.db"

WEEKDAYS_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
MONTHS_UA = ["Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
             "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"]

logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------- база даних

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                conference_url TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                kind TEXT NOT NULL,             -- 'once' | 'nth'
                event_date TEXT,                -- для kind='once', YYYY-MM-DD
                nth INTEGER,                    -- для kind='nth': 1..4 або -1 (останній)
                weekday INTEGER,                -- 0=Пн .. 6=Нд
                event_time TEXT NOT NULL,       -- HH:MM
                warn_text TEXT,                 -- текст попередження за 1 день
                soon_text TEXT,                 -- текст повідомлення з посиланням
                notified_day TEXT,              -- дата події, для якої надіслано нагадування за день
                notified_soon TEXT              -- дата події, для якої надіслано посилання
            )
        """)
        # міграція старих баз: додаємо колонки, якщо їх ще немає
        cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
        for col in ("warn_text", "soon_text", "notified_day", "notified_soon"):
            if col not in cols:
                conn.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")


# ------------------------------------------------- обчислення дат (правила)

def nth_weekday_date(year, month, nth, weekday):
    """Дата N-го <weekday> місяця. nth=-1 означає останній."""
    days = [d for d in range(1, calendar.monthrange(year, month)[1] + 1)
            if datetime.date(year, month, d).weekday() == weekday]
    return datetime.date(year, month, days[nth - 1 if nth > 0 else nth])


def nth_rule_label(nth, weekday):
    ordinals = {1: "1-ша", 2: "2-га", 3: "3-тя", 4: "4-та", -1: "остання"}
    return f"кожна {ordinals[nth]} {WEEKDAYS_UA[weekday]} місяця"


def rule_label(ev):
    return ev["event_date"] if ev["kind"] == "once" \
        else nth_rule_label(ev["nth"], ev["weekday"])


def next_occurrence(ev, from_dt: datetime.datetime) -> datetime.datetime | None:
    """Найближчий початок події починаючи з from_dt."""
    h, m = map(int, ev["event_time"].split(":"))
    if ev["kind"] == "once":
        d = datetime.date.fromisoformat(ev["event_date"])
        return datetime.datetime.combine(d, datetime.time(h, m))
    # kind == 'nth': шукаємо по місяцях уперед
    y, mo = from_dt.year, from_dt.month
    for _ in range(14):  # вистачить більш ніж на рік
        d = nth_weekday_date(y, mo, ev["nth"], ev["weekday"])
        dt = datetime.datetime.combine(d, datetime.time(h, m))
        if dt >= from_dt:
            return dt
        mo += 1
        if mo > 12:
            mo, y = 1, y + 1
    return None


def fill_template(text: str, ev, url=True) -> str:
    """Підставляє плейсхолдери {час}, {правило}, {посилання} у текст."""
    return (text
            .replace("{час}", ev["event_time"])
            .replace("{правило}", rule_label(ev))
            .replace("{посилання}", ev["conference_url"] if url else ""))


# ---------------------------------------------------------------- FSM-стани

class CreateEvent(StatesGroup):
    choosing_kind = State()      # одноразова / повторювана
    choosing_date = State()      # календар (одноразова)
    choosing_nth = State()       # номер тижня (повторювана)
    choosing_weekday = State()
    choosing_time = State()
    asking_url = State()
    asking_warn_text = State()
    asking_soon_text = State()
    asking_group = State()


# ---------------------------------------------------------------- клавіатури

def kind_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Одноразова (обрати дату)", callback_data="kind:once")],
        [InlineKeyboardButton(text="🔁 Повторювана (напр., кожна 3-тя середа)",
                              callback_data="kind:nth")],
    ])


def nth_kb():
    rows = []
    for n, label in [(1, "1-ша"), (2, "2-га"), (3, "3-тя"), (4, "4-та"), (-1, "остання")]:
        rows.append([InlineKeyboardButton(text=label, callback_data=f"nth:{n}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def weekday_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"wd:{i}")
         for i, name in enumerate(WEEKDAYS_UA)],
    ])


def calendar_kb(year: int, month: int):
    """Простий inline-календар із перемиканням місяців."""
    cal = calendar.Calendar(firstweekday=0)
    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)
    rows = [[
        InlineKeyboardButton(text="‹", callback_data=f"cal:{prev_y}:{prev_m}"),
        InlineKeyboardButton(text=f"{MONTHS_UA[month - 1]} {year}", callback_data="cal:noop"),
        InlineKeyboardButton(text="›", callback_data=f"cal:{next_y}:{next_m}"),
    ]]
    rows.append([InlineKeyboardButton(text=d, callback_data="cal:noop")
                 for d in WEEKDAYS_UA])
    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="cal:noop"))
            else:
                d = datetime.date(year, month, day)
                if d < datetime.date.today():
                    row.append(InlineKeyboardButton(text="·", callback_data="cal:noop"))
                else:
                    row.append(InlineKeyboardButton(
                        text=str(day), callback_data=f"date:{d.isoformat()}"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------- хендлери

dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "👋 Бот-календар конференцій.\n\n"
        "/new — створити подію\n"
        "/list — список подій\n"
        "/delete — видалити подію\n\n"
        f"⚠️ Попередження надсилається за 1 день, а посилання на конференцію — "
        f"за {REMIND_MINUTES} хв до початку."
    )


@dp.message(Command("new"))
async def cmd_new(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(CreateEvent.choosing_kind)
    await m.answer("Подія одноразова чи повторювана?", reply_markup=kind_kb())


@dp.callback_query(CreateEvent.choosing_kind, F.data.startswith("kind:"))
async def choose_kind(c: CallbackQuery, state: FSMContext):
    kind = c.data.split(":")[1]
    await state.update_data(kind=kind)
    if kind == "once":
        await state.set_state(CreateEvent.choosing_date)
        t = datetime.date.today()
        await c.message.answer("Оберіть дату:", reply_markup=calendar_kb(t.year, t.month))
    else:
        await state.set_state(CreateEvent.choosing_nth)
        await c.message.answer("Яка за рахунком тиждень місяця?", reply_markup=nth_kb())
    await c.answer()


@dp.callback_query(F.data.startswith("cal:"))
async def cal_nav(c: CallbackQuery, state: FSMContext):
    if c.data == "cal:noop":
        return await c.answer()
    if await state.get_state() != CreateEvent.choosing_date:
        return await c.answer()
    _, y, m = c.data.split(":")
    await c.message.edit_reply_markup(reply_markup=calendar_kb(int(y), int(m)))
    await c.answer()


@dp.callback_query(CreateEvent.choosing_date, F.data.startswith("date:"))
async def choose_date(c: CallbackQuery, state: FSMContext):
    d = c.data.split(":")[1]
    await state.update_data(event_date=d)
    await state.set_state(CreateEvent.choosing_time)
    await c.message.edit_text(f"Дата: {d}. Введіть час у форматі ГГ:ХХ (наприклад 18:30):")
    await c.answer()


@dp.callback_query(CreateEvent.choosing_nth, F.data.startswith("nth:"))
async def choose_nth(c: CallbackQuery, state: FSMContext):
    await state.update_data(nth=int(c.data.split(":")[1]))
    await state.set_state(CreateEvent.choosing_weekday)
    await c.message.edit_text("Який день тижня?", reply_markup=weekday_kb())
    await c.answer()


@dp.callback_query(CreateEvent.choosing_weekday, F.data.startswith("wd:"))
async def choose_weekday(c: CallbackQuery, state: FSMContext):
    await state.update_data(weekday=int(c.data.split(":")[1]))
    await state.set_state(CreateEvent.choosing_time)
    await c.answer()
    await c.message.edit_text("Введіть час у форматі ГГ:ХХ (наприклад 18:30):")


@dp.message(CreateEvent.choosing_time)
async def choose_time(m: Message, state: FSMContext):
    try:
        h, mi = map(int, m.text.strip().split(":"))
        assert 0 <= h < 24 and 0 <= mi < 60
    except Exception:
        return await m.answer("Невірний формат. Введіть час як ГГ:ХХ, наприклад 18:30")
    await state.update_data(event_time=f"{h:02d}:{mi:02d}")
    await state.set_state(CreateEvent.asking_url)
    await m.answer("Надішліть посилання на конференцію (Zoom / Google Meet / тощо):")


@dp.message(CreateEvent.asking_url)
async def ask_url(m: Message, state: FSMContext):
    if not m.text.startswith("http"):
        return await m.answer("Це не схоже на посилання. Надішліть посилання, починаючи з http")
    await state.update_data(conference_url=m.text.strip())
    await state.set_state(CreateEvent.asking_warn_text)
    await m.answer(
        "✍️ Тепер напишіть текст ПОПЕРЕДЖЕННЯ (прийде за 1 день до конференції).\n\n"
        "Можна використовувати плейсхолдери:\n"
        "{час} — час події (напр. 18:30)\n"
        "{правило} — правило події (напр. «кожна 3-тя Ср місяця»)\n\n"
        "Приклад: «📢 Нагадуємо: завтра о {час} — конференція!»\n"
        "Або надішліть /skip щоб використати типовий текст."
    )


@dp.message(CreateEvent.asking_warn_text)
async def ask_warn_text(m: Message, state: FSMContext):
    text = None if m.text == "/skip" else m.text
    await state.update_data(warn_text=text)
    await state.set_state(CreateEvent.asking_soon_text)
    await m.answer(
        "✍️ Тепер напишіть текст ПОВІДОМЛЕННЯ З ПОСИЛАННЯМ "
        f"(прийде за {REMIND_MINUTES} хв до початку).\n\n"
        "Плейсхолдери: {час}, {правило}, {посилання} — посилання на конференцію.\n\n"
        "Приклад: «🔔 Конференція о {час} починається за 15 хв! "
        "Приєднуйтеся: {посилання}»\n"
        "Або надішліть /skip щоб використати типовий текст."
    )


@dp.message(CreateEvent.asking_soon_text)
async def ask_soon_text(m: Message, state: FSMContext):
    text = None if m.text == "/skip" else m.text
    await state.update_data(soon_text=text)
    await state.set_state(CreateEvent.asking_group)
    await m.answer(
        "Тепер додайте бота до потрібної групи та перешліть сюди будь-яке "
        "повідомлення з цієї групи (або її числовий ID, якщо знаєте).\n\n"
        "⚠️ У групі бот має бути адміністратором, щоб писати повідомлення."
    )


@dp.message(CreateEvent.asking_group, F.forward_from_chat)
async def group_from_forward(m: Message, state: FSMContext):
    await save_group(m, state, m.forward_from_chat.id)


@dp.message(CreateEvent.asking_group)
async def group_from_text(m: Message, state: FSMContext):
    try:
        chat_id = int(m.text.strip())
    except ValueError:
        return await m.answer("Не зрозумів. Перешліть повідомлення з групи або надішліть її ID числом.")
    await save_group(m, state, chat_id)


async def save_group(m: Message, state: FSMContext, group_id: int):
    data = await state.get_data()
    with db() as conn:
        conn.execute(
            """INSERT INTO events (title, conference_url, group_id, kind,
               event_date, nth, weekday, event_time, warn_text, soon_text)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("Конференція", data["conference_url"], group_id, data["kind"],
             data.get("event_date"), data.get("nth"), data.get("weekday"),
             data["event_time"], data.get("warn_text"), data.get("soon_text")),
        )
        ev = conn.execute("SELECT * FROM events WHERE id=last_insert_rowid()").fetchone()
    rule = (data.get("event_date") if data["kind"] == "once"
            else nth_rule_label(data["nth"], data["weekday"]))
    nxt = next_occurrence(ev, datetime.datetime.now())
    try:
        await m.bot.send_message(
            group_id, "✅ Бота підключено до цієї групи. Він нагадуватиме "
                      "про конференції: за день та перед початком.")
    except Exception as e:
        logging.error("Не зміг написати до групи %s: %s", group_id, e)
    await state.clear()
    await m.answer(f"✅ Подію створено!\n"
                   f"Правило: {rule}\nЧас: {data['event_time']}\n"
                   f"Наступна: {nxt:%d.%m.%Y %H:%M}\n"
                   f"Нагадування: за 1 день та за {REMIND_MINUTES} хв до початку.")


@dp.message(Command("list"))
async def cmd_list(m: Message):
    with db() as conn:
        rows = conn.execute("SELECT * FROM events").fetchall()
    if not rows:
        return await m.answer("Подій поки немає. Створіть через /new")
    lines = []
    for r in rows:
        nxt = next_occurrence(r, datetime.datetime.now())
        lines.append(
            f"#{r['id']} — {rule_label(r)} о {r['event_time']} "
            f"(наступна: {nxt:%d.%m.%Y %H:%M}, група {r['group_id']})")
    await m.answer("\n".join(lines))


@dp.message(Command("delete"))
async def cmd_delete(m: Message):
    with db() as conn:
        rows = conn.execute("SELECT id FROM events").fetchall()
    if not rows:
        return await m.answer("Немає що видаляти.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Видалити #{r['id']}", callback_data=f"del:{r['id']}")]
        for r in rows])
    await m.answer("Яку подію видалити?", reply_markup=kb)


@dp.callback_query(F.data.startswith("del:"))
async def do_delete(c: CallbackQuery):
    eid = int(c.data.split(":")[1])
    with db() as conn:
        conn.execute("DELETE FROM events WHERE id=?", (eid,))
    await c.message.edit_text(f"Подію #{eid} видалено.")
    await c.answer()


# ---------------------------------------------------------------- планувальник

scheduler = AsyncIOScheduler()


async def check_and_notify(bot: Bot):
    now = datetime.datetime.now()
    with db() as conn:
        rows = conn.execute("SELECT * FROM events").fetchall()
    for ev in rows:
        nxt = next_occurrence(ev, now)
        if nxt is None:
            continue
        key = nxt.strftime("%Y-%m-%d %H:%M")
        rule = rule_label(ev)
        # 1) попередження за 1 день
        day_due = nxt - datetime.timedelta(days=1)
        if now >= day_due and ev["notified_day"] != key:
            text = ev["warn_text"] or (
                f"📅 Попередження: завтра о {ev['event_time']} — конференція.\n"
                f"Правило: {rule}. Посилання надійде за {REMIND_MINUTES} хв до початку.")
            try:
                await bot.send_message(ev["group_id"], fill_template(text, ev, url=False))
                with db() as conn:
                    conn.execute("UPDATE events SET notified_day=? WHERE id=?",
                                 (key, ev["id"]))
                ev = dict(ev)
                ev["notified_day"] = key
            except Exception as e:
                logging.error("Не вдалося надіслати до групи %s: %s", ev["group_id"], e)
        # 2) посилання за N хвилин до початку
        soon_due = nxt - datetime.timedelta(minutes=REMIND_MINUTES)
        if now >= soon_due and ev["notified_soon"] != key:
            text = ev["soon_text"] or (
                f"🔔 Через {REMIND_MINUTES} хв — {rule} о {ev['event_time']}\n"
                f"Приєднуйтеся: {ev['conference_url']}")
            try:
                await bot.send_message(ev["group_id"], fill_template(text, ev))
                with db() as conn:
                    conn.execute("UPDATE events SET notified_soon=? WHERE id=?",
                                 (key, ev["id"]))
            except Exception as e:
                logging.error("Не вдалося надіслати до групи %s: %s", ev["group_id"], e)


# ---------------------------------------------------------------- запуск

async def main():
    init_db()
    bot = Bot(BOT_TOKEN)
    scheduler.add_job(check_and_notify, "interval", seconds=30, args=(bot,))
    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
