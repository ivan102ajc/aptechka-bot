"""
Телеграм-бот «Аптечка»: напоминания о лекарствах, сроки годности,
аптеки рядом и аналоги.

Запуск:  дважды щёлкните start.bat (токен бота — в файле token.txt)
"""

import asyncio
import calendar
import html
import logging
import math
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote_plus

from tg import (Bot, CallbackQuery, Command, CommandStart, DefaultBotProperties, Dispatcher, F,
                FSMContext, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
                ParseMode, ReplyKeyboardMarkup, Router, State, StatesGroup, TelegramAPIError,
                fetch_json)

import analogs
import symptoms as sym

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_VERSION = "1.5.1"   # рассылка «Бот обновлён» — только при смене первых двух цифр
WHATS_NEW = (
    "🆕 <b>Бот обновлён!</b>\n\n"
    "• 📍 Аптеки рядом: бот определяет ваш адрес, отмечает аптеки на вашей улице, "
    "показывает направление и время пешком. Можно просто написать адрес текстом\n"
    "• 🩺 Кнопка «Подобрать по симптому»\n"
    "• 🔄 Аналоги: в справочнике теперь больше 200 действующих веществ и 700+ названий, "
    "бот понимает названия с ошибками (например, «нурафен»)\n"
    "• 📊 История приёма сохраняется, даже если удалить лекарство из аптечки\n"
    "• 📍 Поиск аптек работает через российский сервер карт\n\n"
    "Новые кнопки уже внизу экрана 👇"
)
# На сервере (Amvera) база лежит в постоянной папке /data, на компьютере — рядом с bot.py
DATA_DIR = os.getenv("DATA_DIR") or ("/data" if os.path.isdir("/data") else BASE_DIR)
DB_PATH = os.path.join(DATA_DIR, "medbot.db")
DEFAULT_TZ_MIN = 180          # Москва, UTC+3
REMIND_WINDOW_MIN = 30        # если бот был выключен, напомнит, если опоздал не больше чем на 30 мин
SNOOZE_MIN = 15
# ---- поиск аптек и адресов (всё бесплатно, данные OpenStreetMap)
# Nominatim — основной: адрес по геолокации, адрес по тексту и все аптеки рядом одним запросом.
# Правило сервиса: не чаще 1 запроса в секунду и понятное имя программы (User-Agent).
NOMINATIM_URL = "https://nominatim.openstreetmap.org"
USER_AGENT = "MedReminderTelegramBot/1.5"
NOMINATIM_GAP_S = 1.1
NOMINATIM_TIMEOUT_S = 6
# Запасные источники — только если Nominatim не ответил.
# Overpass: из России стабильно работает лишь российский сервер VK, и он медленный.
OVERPASS_MIRRORS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
OVERPASS_TIMEOUT_S = 25
# Photon: быстрый, но ограничивает частые запросы — спрашиваем один раз.
PHOTON_URL = "https://photon.komoot.io/api/"
PHOTON_QUERIES = ["аптека"]
PHOTON_BUDGET_S = 3
FAST_WAIT_S = 6               # сколько ждём полный запасной список, прежде чем показать быстрый
MAX_WAIT_S = 15               # дольше этого пользователь ждать не будет
SEARCH_RADIUS_M = 3000
CACHE_TTL_S = 24 * 3600       # найденные аптеки района помним сутки

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("medbot")
router = Router()

# ---------------------------------------------------------------- база данных

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            tz_min  INTEGER NOT NULL DEFAULT 180
        );
        CREATE TABLE IF NOT EXISTS meds(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            dose TEXT,
            times TEXT,                 -- "08:00,20:00"
            expiry TEXT,                -- "2027-05-31"
            expiry_stage INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sent(
            med_id INTEGER, day TEXT, time TEXT,
            PRIMARY KEY(med_id, day, time)
        );
        CREATE TABLE IF NOT EXISTS snoozes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            med_id INTEGER NOT NULL,
            due_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS intakes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            med_id INTEGER NOT NULL,
            at_utc TEXT NOT NULL,
            user_id INTEGER,
            med_name TEXT
        );
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        # обновление старой базы: добавляем колонки и заполняем их из аптечки
        cols = {r["name"] for r in c.execute("PRAGMA table_info(intakes)")}
        if "user_id" not in cols:
            c.execute("ALTER TABLE intakes ADD COLUMN user_id INTEGER")
        if "med_name" not in cols:
            c.execute("ALTER TABLE intakes ADD COLUMN med_name TEXT")
        c.execute("""UPDATE intakes SET
                         user_id  = (SELECT user_id FROM meds WHERE meds.id = intakes.med_id),
                         med_name = (SELECT name    FROM meds WHERE meds.id = intakes.med_id)
                     WHERE user_id IS NULL""")


def ensure_user(user_id: int) -> None:
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id, tz_min) VALUES(?, ?)",
                  (user_id, DEFAULT_TZ_MIN))


def user_tz(user_id: int) -> int:
    with db() as c:
        row = c.execute("SELECT tz_min FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row["tz_min"] if row else DEFAULT_TZ_MIN


def add_med(user_id, name, dose, times, expiry) -> int:
    with db() as c:
        cur = c.execute("INSERT INTO meds(user_id,name,dose,times,expiry) VALUES(?,?,?,?,?)",
                        (user_id, name, dose, ",".join(times) if times else None,
                         expiry.isoformat() if expiry else None))
        return cur.lastrowid


def get_meds(user_id: int):
    with db() as c:
        return c.execute("SELECT * FROM meds WHERE user_id=? ORDER BY name", (user_id,)).fetchall()


def get_med(med_id: int, user_id: int | None = None):
    with db() as c:
        if user_id is None:
            return c.execute("SELECT * FROM meds WHERE id=?", (med_id,)).fetchone()
        return c.execute("SELECT * FROM meds WHERE id=? AND user_id=?", (med_id, user_id)).fetchone()


def record_intake(med_id: int) -> None:
    """Отметка «принял». Название сохраняется отдельно, чтобы история не пропала после удаления."""
    m = get_med(med_id)
    if not m:
        return
    with db() as c:
        c.execute("INSERT INTO intakes(med_id, at_utc, user_id, med_name) VALUES(?, ?, ?, ?)",
                  (med_id, datetime.now(timezone.utc).isoformat(), m["user_id"], m["name"]))


def delete_med(med_id: int, user_id: int) -> None:
    with db() as c:
        c.execute("DELETE FROM meds WHERE id=? AND user_id=?", (med_id, user_id))
        c.execute("DELETE FROM snoozes WHERE med_id=?", (med_id,))

# ---------------------------------------------------------------- разбор ввода

def parse_times(text: str) -> list[str] | None:
    """'8:00, 20.30' -> ['08:00', '20:30']; None если формат неверный."""
    found = re.findall(r"(\d{1,2})[:.](\d{2})", text)
    if not found:
        return None
    res = []
    for h, m in found:
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        res.append(f"{h:02d}:{m:02d}")
    return sorted(set(res))


def parse_expiry(text: str) -> date | None:
    """'31.05.2027' -> 2027-05-31; '05.2027' -> 2027-05-31 (конец месяца)."""
    text = text.strip()
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", text)
    if m:
        d, mo, y = map(int, m.groups())
        y += 2000 if y < 100 else 0
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})[./](\d{2,4})", text)
    if m:
        mo, y = map(int, m.groups())
        y += 2000 if y < 100 else 0
        if 1 <= mo <= 12:
            return date(y, mo, calendar.monthrange(y, mo)[1])
    return None


def parse_tz(text: str) -> int | None:
    """'+3' / 'UTC+5' / '-1' -> смещение в минутах."""
    m = re.search(r"([+-]?\d{1,2})(?::(\d{2}))?", text)
    if not m:
        return None
    h = int(m.group(1))
    mins = int(m.group(2) or 0)
    if not -12 <= h <= 14:
        return None
    return h * 60 + (mins if h >= 0 else -mins)


def expiry_stage(expiry: date, today: date) -> int:
    """0 — всё ок, 1 — меньше месяца, 2 — меньше недели, 3 — просрочено."""
    left = (expiry - today).days
    if left < 0:
        return 3
    if left <= 7:
        return 2
    if left <= 30:
        return 1
    return 0


def distance_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def search_link(drug: str) -> str:
    return "https://yandex.ru/search/?text=" + quote_plus(f"{drug} купить в аптеке цена")

# ---------------------------------------------------------------- клавиатуры

BTN_ADD = "➕ Добавить лекарство"
BTN_LIST = "💊 Моя аптечка"
BTN_PHARM = "📍 Аптеки рядом"
BTN_ANALOG = "🔄 Найти аналог"
BTN_HISTORY = "📊 История приёма"
BTN_SYMPTOM = "🩺 Подобрать по симптому"
BTN_CANCEL = "❌ Отмена"
MENU = {BTN_ADD, BTN_LIST, BTN_PHARM, BTN_ANALOG, BTN_HISTORY, BTN_SYMPTOM, BTN_CANCEL}
TEXT = F.text & ~F.text.in_(MENU)   # обычный текст, а не кнопка меню

MAIN_KB = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
    [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_LIST)],
    [KeyboardButton(text=BTN_PHARM), KeyboardButton(text=BTN_ANALOG)],
    [KeyboardButton(text=BTN_SYMPTOM), KeyboardButton(text=BTN_HISTORY)],
])
CANCEL_KB = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[KeyboardButton(text=BTN_CANCEL)]])
LOCATION_KB = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
    [KeyboardButton(text="📍 Отправить мою геолокацию", request_location=True)],
    [KeyboardButton(text=BTN_CANCEL)],
])


def reminder_kb(med_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принял", callback_data=f"take:{med_id}"),
        InlineKeyboardButton(text=f"⏰ Через {SNOOZE_MIN} мин", callback_data=f"snooze:{med_id}"),
    ]])


def med_kb(med_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Аналоги", callback_data=f"an:{med_id}"),
        InlineKeyboardButton(text="🛒 Где купить", callback_data=f"buy:{med_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{med_id}"),
    ]])

# ---------------------------------------------------------------- состояния диалога

class AddMed(StatesGroup):
    name = State()
    dose = State()
    times = State()
    expiry = State()


class AskAnalog(StatesGroup):
    query = State()


class AskSymptom(StatesGroup):
    text = State()


class AskPharm(StatesGroup):
    location = State()

# ---------------------------------------------------------------- общие команды

HELP = (
    "Я помогаю следить за лекарствами:\n\n"
    "⏰ напоминаю, когда пора принять лекарство\n"
    "📅 предупреждаю, когда заканчивается срок годности\n"
    "📍 показываю ближайшие аптеки\n"
    "🔄 подсказываю аналоги с тем же действующим веществом\n"
    "🩺 подсказываю, что обычно помогает при лёгких недомоганиях\n\n"
    "Часовой пояс по умолчанию — Москва. Изменить: /tz +5\n\n"
    "<i>Я не врач. Перед заменой лекарства посоветуйтесь с врачом или фармацевтом.</i>"
)


@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    ensure_user(msg.from_user.id)
    await msg.answer(f"Привет, {html.escape(msg.from_user.first_name or '')}! 👋\n\n{HELP}\n\n"
                     f"<i>Версия бота: {BOT_VERSION}</i>", reply_markup=MAIN_KB)


@router.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(HELP, reply_markup=MAIN_KB)


@router.message(F.text == BTN_CANCEL)
async def cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Отменено.", reply_markup=MAIN_KB)


@router.message(Command("tz"))
async def cmd_tz(msg: Message):
    ensure_user(msg.from_user.id)
    arg = msg.text.partition(" ")[2]
    tz = parse_tz(arg) if arg else None
    if tz is None:
        cur = user_tz(msg.from_user.id)
        await msg.answer(f"Сейчас у вас UTC{cur // 60:+d}. Чтобы изменить, напишите, например: <code>/tz +5</code>")
        return
    with db() as c:
        c.execute("UPDATE users SET tz_min=? WHERE user_id=?", (tz, msg.from_user.id))
    await msg.answer(f"Готово, часовой пояс: UTC{tz // 60:+d}", reply_markup=MAIN_KB)

# ---------------------------------------------------------------- добавление лекарства

@router.message(F.text == BTN_ADD)
async def add_start(msg: Message, state: FSMContext):
    ensure_user(msg.from_user.id)
    await state.set_state(AddMed.name)
    await msg.answer("Как называется лекарство?", reply_markup=CANCEL_KB)


@router.message(AddMed.name, TEXT)
async def add_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text.strip()[:100])
    await state.set_state(AddMed.dose)
    await msg.answer("Как принимать? Например: <i>1 таблетка после еды</i>.\n"
                     "Если не нужно — отправьте «-».")


@router.message(AddMed.dose, TEXT)
async def add_dose(msg: Message, state: FSMContext):
    dose = None if msg.text.strip() == "-" else msg.text.strip()[:100]
    await state.update_data(dose=dose)
    await state.set_state(AddMed.times)
    await msg.answer("Во сколько напоминать? Можно несколько раз в день:\n"
                     "<code>08:00, 14:00, 21:00</code>\n\n"
                     "Если напоминания не нужны (лекарство просто лежит в аптечке) — отправьте «-».")


@router.message(AddMed.times, TEXT)
async def add_times(msg: Message, state: FSMContext):
    if msg.text.strip() == "-":
        times = []
    else:
        times = parse_times(msg.text)
        if times is None:
            await msg.answer("Не понял время 🤔 Напишите так: <code>08:00, 20:00</code>")
            return
    await state.update_data(times=times)
    await state.set_state(AddMed.expiry)
    await msg.answer("Срок годности с упаковки? Например <code>31.05.2027</code> или <code>05.2027</code>.\n"
                     "Если не знаете — отправьте «-».")


@router.message(AddMed.expiry, TEXT)
async def add_expiry(msg: Message, state: FSMContext):
    if msg.text.strip() == "-":
        expiry = None
    else:
        expiry = parse_expiry(msg.text)
        if expiry is None:
            await msg.answer("Не понял дату 🤔 Напишите так: <code>31.05.2027</code> или <code>05.2027</code>")
            return
    data = await state.get_data()
    await state.clear()
    add_med(msg.from_user.id, data["name"], data.get("dose"), data["times"], expiry)

    lines = [f"✅ Добавил <b>{html.escape(data['name'])}</b>"]
    if data.get("dose"):
        lines.append(f"Как принимать: {html.escape(data['dose'])}")
    lines.append("Напоминания: " + (", ".join(data["times"]) if data["times"] else "нет"))
    if expiry:
        lines.append(f"Годен до: {expiry:%d.%m.%Y}")
        if expiry_stage(expiry, date.today()) == 3:
            lines.append("⚠️ Срок годности уже истёк — такое лекарство лучше не принимать.")
    await msg.answer("\n".join(lines), reply_markup=MAIN_KB)

# ---------------------------------------------------------------- список лекарств

def med_text(m) -> str:
    parts = [f"💊 <b>{html.escape(m['name'])}</b>"]
    if m["dose"]:
        parts.append(f"   {html.escape(m['dose'])}")
    parts.append("   ⏰ " + (m["times"].replace(",", ", ") if m["times"] else "без напоминаний"))
    if m["expiry"]:
        exp = date.fromisoformat(m["expiry"])
        mark = {0: "", 1: " ⚠️ меньше месяца", 2: " ⚠️ меньше недели", 3: " ❌ просрочено"}
        parts.append(f"   📅 до {exp:%d.%m.%Y}{mark[expiry_stage(exp, date.today())]}")
    return "\n".join(parts)


@router.message(F.text == BTN_LIST)
async def list_meds(msg: Message, state: FSMContext):
    await state.clear()
    meds = get_meds(msg.from_user.id)
    if not meds:
        await msg.answer("Аптечка пуста. Нажмите «➕ Добавить лекарство».", reply_markup=MAIN_KB)
        return
    for m in meds:
        await msg.answer(med_text(m), reply_markup=med_kb(m["id"]))


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(cb: CallbackQuery):
    med_id = int(cb.data.split(":")[1])
    m = get_med(med_id, cb.from_user.id)
    if m:
        delete_med(med_id, cb.from_user.id)
        await cb.message.edit_text(f"🗑 {html.escape(m['name'])} удалено из аптечки.")
    await cb.answer()


@router.callback_query(F.data.startswith("an:"))
async def cb_analogs(cb: CallbackQuery):
    m = get_med(int(cb.data.split(":")[1]), cb.from_user.id)
    if m:
        text, kb = analog_answer(m["name"])
        await cb.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await cb.answer()


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery, state: FSMContext):
    m = get_med(int(cb.data.split(":")[1]), cb.from_user.id)
    if m:
        await state.set_state(AskPharm.location)
        await state.update_data(drug=m["name"])
        await cb.message.answer(
            f"{ASK_PLACE}\n\nПокажу ближайшие аптеки.\n"
            f'Цены и наличие «{html.escape(m["name"])}»: <a href="{search_link(m["name"])}">поиск по аптекам</a>',
            reply_markup=LOCATION_KB, disable_web_page_preview=True)
    await cb.answer()

# ---------------------------------------------------------------- аналоги

def _entry_card(e: dict) -> list[str]:
    group, rx = analogs.group_info(e)
    lines = [f"Действующее вещество: <b>{html.escape(e['inn'])}</b>",
             f"Группа: {html.escape(group)}\n",
             "<b>Аналоги — препараты с тем же веществом:</b>"]
    for b in e["brands"]:
        lines.append(f'• {html.escape(b)} — <a href="{search_link(b)}">цены</a>')
    others = analogs.same_group(e)
    if others:
        lines.append("\n<b>Похожие по действию</b> (другое вещество — это <u>не прямая замена</u>):")
        for o in others:
            lines.append(f"• {html.escape(', '.join(o['brands'][:3]))} <i>({html.escape(o['inn'])})</i>")
    if rx:
        lines.append("\n💊 Препараты этой группы обычно отпускаются <b>по рецепту</b>. "
                     "Менять их можно только по согласованию с врачом.")
    lines.append("\n⚠️ <i>Сверяйте дозировку и форму выпуска (таблетки, сироп, спрей) — "
                 "они могут отличаться. Если лекарство назначил врач, замену обсудите с ним "
                 "или с фармацевтом в аптеке. Я бот, а не врач.</i>")
    return lines


def analog_answer(query: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст ответа и (если нужно) кнопки «возможно, вы имели в виду»."""
    r = analogs.search(query)
    q = html.escape(query)
    if r["status"] == "found":
        lines = []
        matched = r["matched"][:1].upper() + r["matched"][1:]
        if r["corrected"]:
            lines.append(f"Вы написали «{q}» — похоже, это <b>{html.escape(matched)}</b>.\n"
                         "Если я ошибся, напишите название точнее.\n")
        else:
            lines.append(f"🔎 <b>{html.escape(matched)}</b>\n")
        return "\n".join(lines + _entry_card(r["entry"])), None

    if r["status"] == "suggest":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"ans:{analogs.NAMES.index(name)}")]
            for name in r["options"]])
        return (f"Точно «{q}» не нашёл 🤔 Возможно, вы имели в виду одно из этих лекарств? "
                "Нажмите на подходящее.\n\n"
                f'Если нужного нет — <a href="{search_link(query + " аналоги")}">поиск в интернете</a>.'), kb

    return (f"Не нашёл «{q}» в своём справочнике.\n\n"
            "Попробуйте написать название по-другому или действующее вещество "
            "(оно указано на упаковке мелким шрифтом).\n\n"
            f'• <a href="{search_link(query + " аналоги")}">Поискать аналоги в интернете</a>\n'
            '• <a href="https://grls.rosminzdrav.ru/">Госреестр лекарств</a> — '
            "там можно узнать действующее вещество\n\n"
            "<i>Аналог — это препарат с тем же действующим веществом и дозировкой. "
            "Перед заменой посоветуйтесь с врачом или фармацевтом.</i>"), None


def analog_text(query: str) -> str:
    return analog_answer(query)[0]


@router.callback_query(F.data.startswith("ans:"))
async def cb_analog_suggest(cb: CallbackQuery):
    i = int(cb.data.split(":")[1])
    if 0 <= i < len(analogs.NAMES):
        text, kb = analog_answer(analogs.NAMES[i])
        await cb.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await cb.answer()


@router.message(F.text == BTN_ANALOG)
async def analog_start(msg: Message, state: FSMContext):
    await state.set_state(AskAnalog.query)
    await msg.answer("Напишите название лекарства, например <i>Нурофен</i>.", reply_markup=CANCEL_KB)


@router.message(AskAnalog.query, TEXT)
async def analog_query(msg: Message, state: FSMContext):
    await state.clear()
    text, kb = analog_answer(msg.text.strip())
    await msg.answer(text, reply_markup=kb or MAIN_KB, disable_web_page_preview=True)
    if kb:   # вернуть основное меню, если показывали кнопки-подсказки
        await msg.answer("Или воспользуйтесь меню 👇", reply_markup=MAIN_KB)

# ---------------------------------------------------------------- аптеки рядом

_pharm_cache: dict[tuple, tuple[float, list[dict]]] = {}   # район -> (время, аптеки)
_pharm_pending: dict[tuple, asyncio.Task] = {}             # район -> идущий запрос к Overpass
_addr_cache: dict[tuple, dict | None] = {}                 # точка -> адрес
_nomi_lock = asyncio.Lock()
_nomi_last = 0.0


def _cell(lat: float, lon: float) -> tuple:
    """Район ~1 км: соседние запросы берут аптеки из памяти."""
    return round(lat, 2), round(lon, 2)


def _bbox(lat: float, lon: float, r: float) -> tuple[float, float, float, float]:
    dlat = r / 111000
    dlon = r / (111000 * max(math.cos(math.radians(lat)), 0.01))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon        # юг, запад, север, восток


def _pharmacy(name, street, house, hours, lat, lon, **extra) -> dict:
    addr = ", ".join(x for x in (street, house) if x)
    return {"name": name or "Аптека", "street": street or "", "house": house or "",
            "addr": addr, "hours": hours or "", "lat": float(lat), "lon": float(lon), **extra}


def parse_nominatim(js: list) -> list[dict]:
    res = []
    for x in js or []:
        a, t = x.get("address") or {}, x.get("extratags") or {}
        name = x.get("name") or t.get("brand") or a.get("amenity")
        res.append(_pharmacy(name, a.get("road") or a.get("pedestrian"), a.get("house_number"),
                             t.get("opening_hours"), x["lat"], x["lon"]))
    return res


def parse_pharmacies(js: dict) -> list[dict]:
    """Ответ Overpass."""
    res = []
    for el in js.get("elements", []):
        plat = el.get("lat") or el.get("center", {}).get("lat")
        plon = el.get("lon") or el.get("center", {}).get("lon")
        if plat is None or plon is None:
            continue
        tags = el.get("tags", {})
        res.append(_pharmacy(tags.get("name") or tags.get("brand"), tags.get("addr:street"),
                             tags.get("addr:housenumber"), tags.get("opening_hours"), plat, plon))
    return res


def parse_photon(js: dict) -> list[dict]:
    res = []
    for f in js.get("features", []):
        p = f.get("properties", {})
        coords = f.get("geometry", {}).get("coordinates") or [None, None]
        if coords[1] is None:
            continue
        res.append(_pharmacy(p.get("name"), p.get("street"), p.get("housenumber"), "",
                             coords[1], coords[0], id=f"{p.get('osm_type')}{p.get('osm_id')}"))
    return res

# ---- сравнение адресов

_STREET_TYPES = {"улица", "ул", "проспект", "пр", "пр-т", "просп", "переулок", "пер", "шоссе", "ш",
                 "бульвар", "б-р", "бул", "площадь", "пл", "набережная", "наб", "проезд", "пр-д",
                 "тупик", "аллея", "микрорайон", "мкр", "квартал", "кв-л", "линия", "тракт", "дорога"}


def norm_street(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    words = [w.strip("-") for w in re.split(r"[\s.,]+", s)]
    return " ".join(w for w in words if w and w not in _STREET_TYPES)


def norm_house(h: str) -> str:
    # «49А» -> 49а; «7с2», «49 к1» -> 7 / 49 (строение и корпус не важны)
    m = re.match(r"\s*(\d+)(?:\s*([а-яa-z])(?![\dа-яa-z]))?", (h or "").lower())
    return (m.group(1) + (m.group(2) or "")) if m else ""


def address_relation(user: dict | None, p: dict) -> str:
    """'house' — в вашем доме, 'street' — на вашей улице, '' — иначе."""
    if not user or not user.get("street") or not p.get("street"):
        return ""
    if norm_street(user["street"]) != norm_street(p["street"]):
        return ""
    uh, ph = norm_house(user.get("house")), norm_house(p.get("house"))
    return "house" if uh and uh == ph else "street"


_DIRS = [("⬆️", "на север"), ("↗️", "на северо-восток"), ("➡️", "на восток"), ("↘️", "на юго-восток"),
         ("⬇️", "на юг"), ("↙️", "на юго-запад"), ("⬅️", "на запад"), ("↖️", "на северо-запад")]


def direction(lat1, lon1, lat2, lon2) -> str:
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lon2 - lon1)))
    bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
    arrow, word = _DIRS[int((bearing + 22.5) // 45) % 8]
    return f"{arrow} {word}"


def walk_minutes(dist_m: float) -> int:
    # по прямой короче, чем по улицам: +30 % к расстоянию, скорость ~75 м/мин
    return max(1, round(dist_m * 1.3 / 75))


def nearest(items: list[dict], lat: float, lon: float, n: int = 7, user: dict | None = None) -> list[dict]:
    out, seen = [], set()
    for p in items:
        key = (round(p["lat"], 4), round(p["lon"], 4))
        if key in seen:
            continue
        seen.add(key)
        out.append({**p, "dist": distance_m(lat, lon, p["lat"], p["lon"]),
                    "rel": address_relation(user, p)})
    return sorted(out, key=lambda x: x["dist"])[:n]

# ---- запросы к сервисам

async def nominatim(path: str, params: dict) -> object:
    """Запрос к Nominatim с соблюдением правила «не чаще 1 раза в секунду»."""
    global _nomi_last
    async with _nomi_lock:
        wait = _nomi_last + NOMINATIM_GAP_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _nomi_last = time.monotonic()
    full = {**params, "format": "jsonv2", "accept-language": "ru"}
    status, js = await fetch_json("GET", NOMINATIM_URL + path, params=full,
                                  headers={"User-Agent": USER_AGENT}, timeout=NOMINATIM_TIMEOUT_S)
    if status != 200:
        raise RuntimeError(f"Nominatim HTTP {status}")
    return js


def _addr_from_nominatim(a: dict) -> dict:
    return {"street": a.get("road") or a.get("pedestrian") or a.get("square") or "",
            "house": a.get("house_number") or "",
            "area": a.get("suburb") or a.get("city_district") or a.get("quarter") or "",
            "city": a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or ""}


def format_address(a: dict | None) -> str:
    if not a:
        return ""
    main = ", ".join(x for x in (a.get("street"), a.get("house")) if x)
    rest = ", ".join(x for x in (a.get("area"), a.get("city")) if x and x not in main)
    if main and rest:
        return f"{main} ({rest})"
    return main or rest


async def reverse_address(lat: float, lon: float) -> dict | None:
    """Адрес по координатам. None — если не удалось."""
    key = (round(lat, 4), round(lon, 4))
    if key in _addr_cache:
        return _addr_cache[key]
    try:
        js = await nominatim("/reverse", {"lat": f"{lat}", "lon": f"{lon}", "zoom": "18",
                                          "addressdetails": "1"})
        addr = _addr_from_nominatim(js.get("address") or {}) if isinstance(js, dict) else None
    except Exception as e:
        log.warning("адрес по геолокации не определён: %r", e)
        return None
    if addr and (addr["street"] or addr["city"]):
        _addr_cache[key] = addr
        return addr
    return None


async def geocode(text: str) -> tuple[float, float, dict] | None:
    """Координаты по адресу, написанному текстом."""
    js = await nominatim("/search", {"q": text, "limit": "1", "addressdetails": "1"})
    if not js:
        return None
    x = js[0]
    return float(x["lat"]), float(x["lon"]), _addr_from_nominatim(x.get("address") or {})


async def nominatim_pharmacies(lat: float, lon: float) -> list[dict]:
    """Все аптеки рядом (тег amenity=pharmacy — любое название, включая сети)."""
    for r in (1000, SEARCH_RADIUS_M):
        s_, w, n, e = _bbox(lat, lon, r)
        js = await nominatim("/search", {"amenity": "pharmacy", "bounded": "1", "limit": "50",
                                         "viewbox": f"{w:.5f},{n:.5f},{e:.5f},{s_:.5f}",
                                         "addressdetails": "1", "extratags": "1"})
        items = parse_nominatim(js)
        if len(items) >= 3 or r == SEARCH_RADIUS_M:
            return items
    return []


async def overpass_pharmacies(lat: float, lon: float) -> list[dict]:
    """Спрашивает все серверы Overpass одновременно, берёт первый удачный ответ."""
    s_, w, n, e = _bbox(lat, lon, SEARCH_RADIUS_M)
    box = f"{s_:.5f},{w:.5f},{n:.5f},{e:.5f}"
    query = (f'[out:json][timeout:{OVERPASS_TIMEOUT_S - 5}];'
             f'(node["amenity"="pharmacy"]({box});way["amenity"="pharmacy"]({box}););out center 150;')

    async def one(url):
        status, js = await fetch_json("POST", url, form={"data": query},
                                      headers={"User-Agent": USER_AGENT}, timeout=OVERPASS_TIMEOUT_S)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        return parse_pharmacies(js)

    tasks = [asyncio.create_task(one(u)) for u in OVERPASS_MIRRORS]
    errors = []
    try:
        for fut in asyncio.as_completed(tasks):
            try:
                return await fut
            except Exception as e:
                errors.append(repr(e))
    finally:
        for t in tasks:
            t.cancel()
    raise RuntimeError("Overpass недоступен: " + "; ".join(errors))


async def photon_pharmacies(lat: float, lon: float) -> list[dict]:
    s_, w, n, e = _bbox(lat, lon, SEARCH_RADIUS_M)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PHOTON_BUDGET_S
    found: dict[str, dict] = {}
    ok = False
    for q in PHOTON_QUERIES:
        left = deadline - loop.time()
        if left <= 0.2:
            break
        params = {"q": q, "lat": f"{lat}", "lon": f"{lon}", "limit": "50",
                  "bbox": f"{w:.5f},{s_:.5f},{e:.5f},{n:.5f}", "osm_tag": "amenity:pharmacy"}
        try:
            status, js = await asyncio.wait_for(
                fetch_json("GET", PHOTON_URL, params=params, headers={"User-Agent": USER_AGENT},
                           timeout=left), left)
            if status != 200:
                continue
            for p in parse_photon(js):
                found.setdefault(p["id"], p)
            ok = True
        except Exception as e:
            log.info("Photon «%s»: %r", q, e)
    if not ok:
        raise RuntimeError("Photon недоступен")
    return list(found.values())


def _remember(cell: tuple):
    def done(task: asyncio.Task):
        _pharm_pending.pop(cell, None)
        if not task.cancelled() and task.exception() is None:
            _pharm_cache[cell] = (time.monotonic(), task.result())
        elif not task.cancelled():
            log.warning("Запасной список аптек не получен: %s", task.exception())
    return done


async def find_pharmacies(lat: float, lon: float, user: dict | None = None) -> tuple[list[dict], bool]:
    """
    Возвращает (аптеки, полный_ли_список). Бросает исключение, если не ответил ни один сервис.
    Порядок: память -> Nominatim -> запасные (Overpass и Photon одновременно).
    """
    t0 = time.monotonic()
    cell = _cell(lat, lon)
    cached = _pharm_cache.get(cell)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_S and cached[1]:
        return nearest(cached[1], lat, lon, user=user), True

    try:
        res = await nominatim_pharmacies(lat, lon)
        log.info("Аптеки: Nominatim, %d шт. за %.1f с", len(res), time.monotonic() - t0)
        if res:
            _pharm_cache[cell] = (time.monotonic(), res)
        return nearest(res, lat, lon, user=user), True
    except Exception as e:
        log.warning("Nominatim не ответил: %r — пробую запасные источники", e)

    full = _pharm_pending.get(cell)
    if full is None:
        full = asyncio.create_task(overpass_pharmacies(lat, lon))
        full.add_done_callback(_remember(cell))
        _pharm_pending[cell] = full
    fast = asyncio.create_task(photon_pharmacies(lat, lon))
    try:
        res = await asyncio.wait_for(asyncio.shield(full), FAST_WAIT_S)
        if res:
            fast.cancel()
            return nearest(res, lat, lon, user=user), True
    except Exception:
        pass
    try:
        res = await fast
        if res:
            return nearest(res, lat, lon, user=user), False
    except Exception as e:
        log.warning("Photon не помог: %s", e)
    left = max(1.0, MAX_WAIT_S - (time.monotonic() - t0))
    res = await asyncio.wait_for(asyncio.shield(full), left)
    return nearest(res, lat, lon, user=user), True


def yandex_maps_search(lat: float, lon: float) -> str:
    return f"https://yandex.ru/maps/?ll={lon},{lat}&z=15&text={quote_plus('аптека')}"


def yandex_point(lat: float, lon: float) -> str:
    return f"https://yandex.ru/maps/?pt={lon},{lat}&z=17&l=map"


def maps_kb(lat: float, lon: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Аптеки на Яндекс Картах", url=yandex_maps_search(lat, lon))],
        [InlineKeyboardButton(text="🗺 Аптеки в 2ГИС",
                              url=f"https://2gis.ru/search/{quote_plus('аптека')}?m={lon},{lat}/15")],
    ])


ASK_PLACE = ("Нажмите кнопку ниже, чтобы отправить геолокацию (на телефоне),\n"
             "или <b>напишите адрес</b>, например: <i>Москва, Тверская 7</i>")


@router.message(F.text == BTN_PHARM)
async def pharm_start(msg: Message, state: FSMContext):
    await state.set_state(AskPharm.location)
    await msg.answer(ASK_PLACE, reply_markup=LOCATION_KB)


def format_dist(d: float) -> str:
    return f"{d:.0f} м" if d < 1000 else f"{d / 1000:.1f} км"


def pharmacies_text(items: list[dict] | None, complete: bool, user: dict | None,
                    lat: float, lon: float, typed: bool = False) -> str:
    lines = []
    where = format_address(user)
    if where:
        lines.append(f"📍 <b>{'Адрес' if typed else 'Ваш адрес'}:</b> {html.escape(where)}")
        if not typed:
            lines.append("<i>Если адрес неверный — нажмите «📍 Аптеки рядом» и напишите его текстом.</i>")
        lines.append("")
    if items is None:
        lines.append("Не получилось загрузить список аптек, но их можно сразу открыть на карте — кнопки ниже 👇")
        return "\n".join(lines)
    if not items:
        lines.append("Поблизости аптек в базе не нашёл. Посмотрите на карте — кнопки ниже 👇")
        return "\n".join(lines)

    same_house = [p for p in items if p["rel"] == "house"]
    same_street = [p for p in items if p["rel"] == "street"]
    if same_house:
        lines.append("🏠 <b>Аптека прямо в вашем доме!</b>")
    elif same_street:
        n = len(same_street)
        word = "аптека" if n == 1 else "аптеки" if n < 5 else "аптек"
        lines.append(f"🛣 <b>На вашей улице: {n} {word}</b> (отмечены значком 🛣)")
    elif user and user.get("street"):
        lines.append("На вашей улице аптек не нашёл, вот ближайшие:")

    lines.append("<b>Ближайшие аптеки:</b>\n")
    for i, p in enumerate(items, 1):
        link = yandex_point(p["lat"], p["lon"])
        lines.append(f'{i}. <a href="{link}">{html.escape(p["name"])}</a> — {format_dist(p["dist"])}, '
                     f'~{walk_minutes(p["dist"])} мин пешком {direction(lat, lon, p["lat"], p["lon"])}')
        extra = []
        if p["addr"]:
            extra.append(html.escape(p["addr"]))
        if p["rel"] == "house":
            extra.append("🏠 в вашем доме")
        elif p["rel"] == "street":
            extra.append("🛣 на вашей улице")
        if extra:
            lines.append("    " + " · ".join(extra))
        if p["hours"]:
            lines.append(f"    🕒 {html.escape(p['hours'])}")
    if not complete:
        lines.append("\n<i>Список может быть неполным — все аптеки на карте 👇</i>")
    return "\n".join(lines)


async def send_pharmacies(msg: Message, lat: float, lon: float, drug: str | None,
                          user: dict | None = None, typed: bool = False) -> None:
    waiting = await msg.answer("Ищу аптеки рядом… 🔎", reply_markup=MAIN_KB)
    if user is None:
        # адрес и аптеки ищем одновременно
        addr_task = asyncio.create_task(reverse_address(lat, lon))
    try:
        items, complete = await find_pharmacies(lat, lon)
    except Exception as e:
        log.warning("поиск аптек не удался: %r", e)
        items, complete = None, True
    if user is None:
        user = await addr_task
    if items:
        items = [{**p, "rel": address_relation(user, p)} for p in items]
    text = pharmacies_text(items, complete, user, lat, lon, typed)
    if drug:
        text += (f'\n\nНаличие и цены «{html.escape(drug)}»: <a href="{search_link(drug)}">поиск по аптекам</a>\n'
                 "<i>Проверить наличие напрямую бот не может: аптеки не дают открытого доступа к своим остаткам.</i>")
    await msg.answer(text, disable_web_page_preview=True, reply_markup=maps_kb(lat, lon))
    try:
        await waiting.delete()
    except Exception:
        pass


@router.message(F.location)
async def got_location(msg: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await send_pharmacies(msg, msg.location.latitude, msg.location.longitude, data.get("drug"))


@router.message(AskPharm.location, TEXT)
async def got_address_text(msg: Message, state: FSMContext):
    text = msg.text.strip()[:200]
    try:
        found = await geocode(text)
    except Exception as e:
        log.warning("поиск адреса не удался: %r", e)
        await msg.answer("Сервис адресов сейчас не отвечает 😔 Попробуйте ещё раз через минуту "
                         "или отправьте геолокацию кнопкой ниже.", reply_markup=LOCATION_KB)
        return
    if not found:
        await msg.answer("Не нашёл такой адрес 🤔 Напишите так: <i>Город, улица, дом</i> — "
                         "например, <i>Казань, Баумана 20</i>.", reply_markup=LOCATION_KB)
        return
    lat, lon, addr = found
    data = await state.get_data()
    await state.clear()
    await send_pharmacies(msg, lat, lon, data.get("drug"), user=addr, typed=True)

# ---------------------------------------------------------------- напоминания: кнопки

@router.callback_query(F.data.startswith("take:"))
async def cb_take(cb: CallbackQuery):
    med_id = int(cb.data.split(":")[1])
    if get_med(med_id, cb.from_user.id):
        record_intake(med_id)
        await cb.message.edit_text(cb.message.html_text + "\n\n✅ <b>Принято</b>")
    await cb.answer("Отлично!")


@router.callback_query(F.data.startswith("snooze:"))
async def cb_snooze(cb: CallbackQuery):
    med_id = int(cb.data.split(":")[1])
    if get_med(med_id, cb.from_user.id):
        due = datetime.now(timezone.utc) + timedelta(minutes=SNOOZE_MIN)
        with db() as c:
            c.execute("INSERT INTO snoozes(med_id, due_utc) VALUES(?, ?)", (med_id, due.isoformat()))
        await cb.message.edit_text(cb.message.html_text + f"\n\n⏰ Напомню через {SNOOZE_MIN} минут")
    await cb.answer()


@router.message(F.text == BTN_HISTORY)
async def history(msg: Message, state: FSMContext):
    await state.clear()
    tz = timedelta(minutes=user_tz(msg.from_user.id))
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with db() as c:
        rows = c.execute("""SELECT intakes.med_name, intakes.at_utc, meds.id AS still_here
                            FROM intakes LEFT JOIN meds ON meds.id = intakes.med_id
                            WHERE intakes.user_id=? AND intakes.at_utc>=?
                            ORDER BY intakes.at_utc DESC LIMIT 40""",
                         (msg.from_user.id, since)).fetchall()
    if not rows:
        await msg.answer("За последние 30 дней отметок о приёме нет.", reply_markup=MAIN_KB)
        return
    lines = ["<b>Приём за 30 дней:</b>"]
    for r in rows:
        t = datetime.fromisoformat(r["at_utc"]) + tz
        gone = "" if r["still_here"] else " <i>(удалено из аптечки)</i>"
        lines.append(f"✅ {t:%d.%m %H:%M} — {html.escape(r['med_name'] or '?')}{gone}")
    await msg.answer("\n".join(lines), reply_markup=MAIN_KB)

# ---------------------------------------------------------------- подбор по симптому

def symptoms_kb() -> InlineKeyboardMarkup:
    items = list(sym.SYMPTOMS.items())
    rows = [[InlineKeyboardButton(text=s["title"], callback_data=f"sym:{k}") for k, s in items[i:i + 2]]
            for i in range(0, len(items), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def answer_symptom_text(msg: Message, text: str) -> bool:
    """Отвечает по тексту с симптомами. Возвращает False, если ничего не распознал."""
    danger = sym.check_danger(text)
    if danger == "crisis":
        await msg.answer(sym.CRISIS_TEXT, reply_markup=MAIN_KB)
        return True
    if danger == "emergency":
        await msg.answer(sym.EMERGENCY_TEXT, reply_markup=MAIN_KB)
        return True
    keys = sym.match_symptoms(text)
    if not keys:
        return False
    child = sym.mentions_child(text)
    for k in keys[:3]:
        await msg.answer(sym.symptom_text(k, child), reply_markup=MAIN_KB)
    return True


@router.message(F.text == BTN_SYMPTOM)
async def symptom_start(msg: Message, state: FSMContext):
    await state.set_state(AskSymptom.text)
    await msg.answer(sym.DISCLAIMER, reply_markup=CANCEL_KB)
    await msg.answer("Выберите, что беспокоит, или опишите своими словами "
                     "(например: <i>болит голова и температура</i>).", reply_markup=symptoms_kb())


@router.callback_query(F.data.startswith("sym:"))
async def cb_symptom(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":", 1)[1]
    if key in sym.SYMPTOMS:
        await state.clear()
        await cb.message.answer(sym.symptom_text(key), reply_markup=MAIN_KB)
    await cb.answer()


@router.message(AskSymptom.text, TEXT)
async def symptom_text_input(msg: Message, state: FSMContext):
    if await answer_symptom_text(msg, msg.text):
        await state.clear()
        return
    await msg.answer("Не смог распознать симптом 🤔 Выберите из списка или опишите иначе.\n\n"
                     "Если симптомы сильные или необычные — лучше сразу обратиться к врачу, "
                     "а в экстренном случае звонить <b>103</b> или <b>112</b>.",
                     reply_markup=symptoms_kb())

# ---------------------------------------------------------------- непонятные сообщения
# Этот обработчик должен стоять ПОСЛЕДНИМ: он ловит всё, что не поймали остальные.

STATE_HINTS = {
    AddMed.name.state: "Напишите название лекарства текстом.",
    AddMed.dose.state: "Напишите, как принимать, или отправьте «-».",
    AddMed.times.state: "Напишите время, например <code>08:00, 20:00</code>, или «-».",
    AddMed.expiry.state: "Напишите срок годности, например <code>05.2027</code>, или «-».",
    AskAnalog.query.state: "Напишите название лекарства текстом.",
    AskPharm.location.state: "Нажмите кнопку «📍 Отправить мою геолокацию» внизу экрана или напишите адрес.",
    AskSymptom.text.state: "Опишите симптом текстом или выберите из списка выше.",
}


@router.message()
async def fallback(msg: Message, state: FSMContext):
    current = await state.get_state()
    if current in STATE_HINTS:
        await msg.answer(f"{STATE_HINTS[current]}\nИли нажмите «{BTN_CANCEL}».")
        return
    text = msg.text or ""
    if text.startswith("/"):
        await msg.answer("Такой команды я не знаю 🤷\n\n"
                         "Доступные команды:\n/start — начать заново\n/help — что я умею\n"
                         "/tz — сменить часовой пояс\n\nИли пользуйтесь кнопками внизу 👇",
                         reply_markup=MAIN_KB)
        return
    if text and await answer_symptom_text(msg, text):
        return
    found = analogs.search(text) if text and len(text) <= 60 else {"status": "none"}
    if found["status"] == "found" and found["score"] >= 0.9:   # только почти точные названия
        answer, _ = analog_answer(text)
        await msg.answer(answer, reply_markup=MAIN_KB, disable_web_page_preview=True)
        return
    await msg.answer("Я не понял сообщение 🙂 Пожалуйста, воспользуйтесь кнопками внизу экрана "
                     "или напишите /help, чтобы узнать, что я умею.", reply_markup=MAIN_KB)

# ---------------------------------------------------------------- фоновая проверка

def due_reminders(now_utc: datetime) -> list[tuple]:
    """Какие напоминания пора отправить. Возвращает [(med_row, day, time)]."""
    out = []
    with db() as c:
        rows = c.execute("""SELECT meds.*, users.tz_min FROM meds
                            JOIN users ON users.user_id = meds.user_id
                            WHERE meds.times IS NOT NULL AND meds.times != ''""").fetchall()
        for m in rows:
            local = now_utc + timedelta(minutes=m["tz_min"])
            day = local.date().isoformat()
            now_min = local.hour * 60 + local.minute
            for t in m["times"].split(","):
                h, mi = map(int, t.split(":"))
                late = now_min - (h * 60 + mi)
                if 0 <= late <= REMIND_WINDOW_MIN:
                    if not c.execute("SELECT 1 FROM sent WHERE med_id=? AND day=? AND time=?",
                                     (m["id"], day, t)).fetchone():
                        out.append((m, day, t))
    return out


def reminder_text(m) -> str:
    text = f"⏰ Пора принять <b>{html.escape(m['name'])}</b>"
    if m["dose"]:
        text += f"\n{html.escape(m['dose'])}"
    return text


async def check_reminders(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    for m, day, t in due_reminders(now):
        try:
            await bot.send_message(m["user_id"], reminder_text(m), reply_markup=reminder_kb(m["id"]))
        except TelegramAPIError as e:
            log.warning("не смог отправить %s: %s", m["user_id"], e)
        with db() as c:
            c.execute("INSERT OR IGNORE INTO sent(med_id, day, time) VALUES(?,?,?)", (m["id"], day, t))

    with db() as c:
        snz = c.execute("SELECT * FROM snoozes WHERE due_utc<=?", (now.isoformat(),)).fetchall()
        c.executemany("DELETE FROM snoozes WHERE id=?", [(s["id"],) for s in snz])
    for s in snz:
        m = get_med(s["med_id"])
        if m:
            try:
                await bot.send_message(m["user_id"], reminder_text(m) + " (повтор)",
                                       reply_markup=reminder_kb(m["id"]))
            except TelegramAPIError as e:
                log.warning("не смог отправить %s: %s", m["user_id"], e)


async def check_expiry(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    with db() as c:
        rows = c.execute("""SELECT meds.*, users.tz_min FROM meds
                            JOIN users ON users.user_id = meds.user_id
                            WHERE meds.expiry IS NOT NULL""").fetchall()
    for m in rows:
        local = now + timedelta(minutes=m["tz_min"])
        if not 10 <= local.hour < 21:          # не беспокоим ночью
            continue
        exp = date.fromisoformat(m["expiry"])
        stage = expiry_stage(exp, local.date())
        if stage <= m["expiry_stage"]:
            continue
        name = html.escape(m["name"])
        msg = {1: f"📅 У <b>{name}</b> срок годности заканчивается {exp:%d.%m.%Y} — меньше чем через месяц.",
               2: f"⚠️ У <b>{name}</b> срок годности заканчивается {exp:%d.%m.%Y} — меньше недели!",
               3: f"❌ У <b>{name}</b> истёк срок годности ({exp:%d.%m.%Y}). Такое лекарство лучше не принимать."}[stage]
        try:
            await bot.send_message(m["user_id"], msg)
        except TelegramAPIError as e:
            log.warning("не смог отправить %s: %s", m["user_id"], e)
        with db() as c:
            c.execute("UPDATE meds SET expiry_stage=? WHERE id=?", (stage, m["id"]))


async def scheduler(bot: Bot) -> None:
    while True:
        try:
            await check_reminders(bot)
            await check_expiry(bot)
        except Exception:
            log.exception("ошибка в планировщике")
        await asyncio.sleep(20)

# ---------------------------------------------------------------- запуск

def load_token() -> str:
    """Токен берётся из файла token.txt рядом с bot.py (или из переменной BOT_TOKEN)."""
    token = os.getenv("BOT_TOKEN", "")
    path = os.path.join(BASE_DIR, "token.txt")
    if not token and os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if ":" in line and not line.startswith("#"):
                    token = line
                    break
    if not re.fullmatch(r"\d+:[\w-]{30,}", token):
        raise SystemExit("Не найден токен бота. Откройте файл token.txt и вставьте токен от @BotFather.")
    return token


async def announce_update(bot: Bot) -> None:
    """Если версия бота изменилась — рассказать всем о новинках и прислать новое меню.
    Без этого Telegram продолжает показывать пользователям старые кнопки."""
    with db() as c:
        row = c.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        users = [r["user_id"] for r in c.execute("SELECT user_id FROM users")]
    major = lambda v: ".".join(v.split(".")[:2])
    if row and major(row["value"]) == major(BOT_VERSION):
        if row["value"] != BOT_VERSION:          # пишем в базу, только если версия правда изменилась
            with db() as c:
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('version', ?)", (BOT_VERSION,))
        return
    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid, WHATS_NEW, reply_markup=MAIN_KB)
            sent += 1
        except TelegramAPIError as e:
            log.warning("не смог отправить новость %s: %s", uid, e)
        await asyncio.sleep(0.05)       # чтобы Telegram не ограничил частоту
    with db() as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('version', ?)", (BOT_VERSION,))
    log.info("Разослал новость об обновлении %d пользователям", sent)


async def main() -> None:
    init_db()
    bot = Bot(load_token(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(scheduler(bot))
    me = await bot.get_me()
    # если бот уже переехал в облако (Telegram шлёт сообщения туда), второй экземпляр не нужен
    info = await bot.get_webhook_info()
    if info.url:
        log.warning("Бот @%s сейчас подключён к облаку (%s).", me.username, info.url)
        print("\nЕсли облачная версия работает — просто закройте это окно.")
        print("Если облако не отвечает и бота нужно вернуть на этот компьютер, введите Y и нажмите Enter.")
        if sys.stdin is None or not sys.stdin.isatty():
            answer = "y"          # сервер: спросить некого, забираем бота себе
            log.info("Запуск на сервере — отключаю облачный вебхук автоматически.")
        else:
            answer = await asyncio.to_thread(input, "Вернуть бота на компьютер? [Y/N]: ")
        if answer.strip().lower() not in ("y", "д", "да", "yes"):
            await bot.session.close()
            return
        await bot.delete_webhook()
        log.info("Готово: бот снова работает на этом компьютере. "
                 "Не включайте таймер в облаке, пока работает это окно.")
    log.info("Бот @%s запущен (версия %s). Чтобы остановить — нажмите Ctrl+C.", me.username, BOT_VERSION)
    log.info("ВАЖНО: держите открытым только ОДНО такое окно, иначе бот будет работать неправильно.")
    await announce_update(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
