import asyncio
import os
from datetime import datetime, timezone
import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

# ================== ENV ==================
load_dotenv()

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN or ":" not in BOT_TOKEN or " " in BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN пустой или в неверном формате. Проверь .env")

CHANNEL_USERNAME = (os.getenv("CHANNEL_USERNAME") or "").strip()  # без @
ORGANIZER_LINK = os.getenv("ORGANIZER_LINK", "https://t.me/").strip()
try:
    ORGANIZER_ADMIN_ID = int((os.getenv("ORGANIZER_ADMIN_ID") or "0").strip())
except ValueError:
    raise RuntimeError("ORGANIZER_ADMIN_ID должен быть числом без лишних символов")

GIVEAWAY_CODE = os.getenv("GIVEAWAY_CODE", "632").strip()
PRIZE_COUNT = int(os.getenv("PRIZE_COUNT", "3").strip())
PRIZE_2_NAME = os.getenv("PRIZE_2_NAME", "Приз").strip()

# Новые поля: даты начала/конца из .env (человекочитаемые)
GIVEAWAY_START_RAW = (os.getenv("GIVEAWAY_START") or "").strip()
GIVEAWAY_END_RAW = (os.getenv("GIVEAWAY_END") or "").strip()

DB_PATH = "giveaway.db"

# ================== BOT/DP ==================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ================== ВСПОМОГАТЕЛЬНЫЕ ==================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_human_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
        return dt.astimezone().astimezone(timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

GIVEAWAY_START = parse_human_dt(GIVEAWAY_START_RAW)
GIVEAWAY_END = parse_human_dt(GIVEAWAY_END_RAW)

def fmt_dt(iso_or_dt) -> str:
    if not iso_or_dt:
        return "—"
    try:
        if isinstance(iso_or_dt, str):
            dt = datetime.fromisoformat(iso_or_dt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = iso_or_dt
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(iso_or_dt)

def calc_status_by_time(gw_status_db: str) -> str:
    if gw_status_db == "finished":
        return "завершен"
    now = datetime.now(timezone.utc)
    if GIVEAWAY_START and now < GIVEAWAY_START:
        return "ожидается"
    if GIVEAWAY_END and now > GIVEAWAY_END:
        return "завершен"
    return "активен"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            organizer_link TEXT,
            prize_count INTEGER,
            created_at TEXT,
            results_at TEXT,
            status TEXT,           -- 'open' | 'finished'
            prize_2 TEXT
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            giveaway_code TEXT,
            joined_at TEXT
        )
        """)
        cur = await db.execute("SELECT code FROM giveaways WHERE code = ?", (GIVEAWAY_CODE,))
        row = await cur.fetchone()
        if not row:
            await db.execute("""
                INSERT INTO giveaways(code, organizer_link, prize_count, created_at, status, prize_2)
                VALUES (?, ?, ?, ?, 'open', ?)
            """, (GIVEAWAY_CODE, ORGANIZER_LINK, PRIZE_COUNT, now_iso(), PRIZE_2_NAME))
        await db.commit()

async def get_giveaway():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT code, organizer_link, prize_count, created_at, results_at, status, prize_2
            FROM giveaways WHERE code = ?
        """, (GIVEAWAY_CODE,))
        r = await cur.fetchone()
        if not r:
            return None
        return {
            "code": r[0], "organizer_link": r[1], "prize_count": r[2],
            "created_at": r[3], "results_at": r[4], "status": r[5], "prize_2": r[6]
        }

async def set_giveaway_finished():
    """
    Итоги = GIVEAWAY_END (если задан), иначе: MAX(joined_at) или текущее время.
    """
    if GIVEAWAY_END is not None:
        results_dt = GIVEAWAY_END
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT MAX(joined_at) FROM participants WHERE giveaway_code = ?",
                (GIVEAWAY_CODE,)
            )
            max_join, = await cur.fetchone()
            if max_join:
                results_at_iso = max_join
                await db.execute(
                    "UPDATE giveaways SET status='finished', results_at=? WHERE code=?",
                    (results_at_iso, GIVEAWAY_CODE)
                )
                await db.commit()
                return results_at_iso
            else:
                results_dt = datetime.now(timezone.utc)

    results_at_iso = results_dt.isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE giveaways SET status='finished', results_at=? WHERE code=?",
            (results_at_iso, GIVEAWAY_CODE)
        )
        await db.commit()
    return results_at_iso

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=f"@{CHANNEL_USERNAME}", user_id=user_id)
        status = getattr(member, "status", None)
        if hasattr(status, "value"):
            status = status.value
        return status in ("member", "administrator", "creator")
    except Exception:
        return False

def subscribe_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔔 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME}"))
    kb.row(InlineKeyboardButton(text="♻ Проверить подписку", callback_data="check_sub"))
    return kb.as_markup()

# === ReplyKeyboard (как в DaVinci, отправляет текст, не команды) ===
def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎁 Создать розыгрыш")],
            [KeyboardButton(text="📝 Мои розыгрыши")],
            [KeyboardButton(text="📢 Мои каналы")],
        ],
        resize_keyboard=True,
        is_persistent=True,      # клавиатура остаётся
        input_field_placeholder="Выберите действие…"
    )

def build_giveaway_text(*, gw: dict, finished_view: bool = False, user_tg_id: int | None = None) -> str:
    code = gw["code"]
    status_word = calc_status_by_time(gw["status"])

    if not finished_view and status_word != "завершен":
        header = f"🎁 Теперь вы участник розыгрыша [#{code}, ваш ID: {user_tg_id}]\n\n"
    else:
        header = f"❌ Розыгрыш [#{code}] завершен.\n\n"

    start_txt = fmt_dt(GIVEAWAY_START) if GIVEAWAY_START else "—"
    end_txt = fmt_dt(GIVEAWAY_END) if GIVEAWAY_END else "—"

    body = (
        f"👑 Организатор розыгрыша: {gw['organizer_link']}\n"
        f"🏟️ Количество призовых мест: {gw['prize_count']}\n"
        f"🟢 Начало: {start_txt}\n"
        f"🔚 Конец: {end_txt}\n"
        f"⌚️ Создан: {fmt_dt(gw['created_at'])}\n"
        f"⌚️ Итоги: {fmt_dt(gw['results_at'])}\n\n"
        f"✅ Статус: {status_word}"
    )
    return header + body

# ================== УВЕДОМЛЕНИЕ УЧАСТНИКОВ ==================
async def notify_all_participants(results_time_iso: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, tg_id FROM participants
            WHERE giveaway_code = ? AND joined_at <= ?
            ORDER BY id ASC
        """, (GIVEAWAY_CODE, results_time_iso))
        rows = await cur.fetchall()

    sent = 0
    for pid, uid in rows:
        try:
            await bot.send_message(uid, "🎲 Бот подводит итоги...")
            await asyncio.sleep(1)
            text = (
                f"🎁 Вы выиграли в розыгрыше [#{GIVEAWAY_CODE}, ваш ID: {uid}]\n\n"
                f"🎖️ Призовое место: 2 ({PRIZE_2_NAME})\n\n"
                f"Для получения приза свяжитесь с организатором розыгрыша: {ORGANIZER_LINK}"
            )
            await bot.send_message(uid, text, disable_web_page_preview=True)
            sent += 1
        except Exception:
            pass
    return sent

# ================== АВТО-ЗАВЕРШЕНИЕ ==================
async def finish_if_due():
    gw = await get_giveaway()
    if not gw:
        return False
    if gw["status"] == "finished":
        return False
    if GIVEAWAY_END and datetime.now(timezone.utc) >= GIVEAWAY_END:
        results_time = await set_giveaway_finished()
        sent = await notify_all_participants(results_time)
        print(f"[AutoFinish] Итоги зафиксированы: {fmt_dt(results_time)}; уведомлено: {sent}")
        return True
    return False

async def auto_watcher():
    try:
        await finish_if_due()
    except Exception as e:
        print("[AutoFinish@startup] Ошибка:", e)
    while True:
        try:
            await asyncio.sleep(20)
            await finish_if_due()
        except Exception as e:
            print("[AutoFinish@loop] Ошибка:", e)

# ================== ХЕНДЛЕРЫ ==================
@dp.message(Command("start"))
async def cmd_start(m: Message):
    gw = await get_giveaway()
    if not gw:
        await m.answer("Ошибка: розыгрыш не найден.", reply_markup=main_reply_keyboard())
        return

    status_word = calc_status_by_time(gw["status"])
    if status_word == "завершен":
        await m.answer(
            build_giveaway_text(gw=gw, finished_view=True),
            disable_web_page_preview=True,
            reply_markup=main_reply_keyboard()
        )
        return

    if status_word == "ожидается":
        await m.answer(
            "⏳ Розыгрыш ещё не начался.\n\n" +
            build_giveaway_text(gw=gw, finished_view=False, user_tg_id=m.from_user.id),
            reply_markup=main_reply_keyboard(),
            disable_web_page_preview=True
        )
        return

    text = (
        "🛑 Для участия в розыгрыше вам необходимо подписаться на канал организатора.\n\n"
        "После подписки нажмите «♻ Проверить подписку»."
    )
    # В стартовом сообщении показываем ReplyKeyboard + inline-кнопки подписки
    await m.answer(
        text,
        reply_markup=main_reply_keyboard()
    )
    # Отдельным сообщением — inline-кнопки (чтобы не смешивать две разметки)
    await m.answer(
        "Выберите действие:",
        reply_markup=subscribe_keyboard()
    )

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(c: CallbackQuery):
    gw = await get_giveaway()
    if not gw:
        await c.answer("Ошибка: розыгрыш не найден.", show_alert=True)
        return

    status_word = calc_status_by_time(gw["status"])
    if status_word == "завершен":
        try:
            await c.message.edit_text(build_giveaway_text(gw=gw, finished_view=True), disable_web_page_preview=True)
        except Exception:
            await c.message.answer(build_giveaway_text(gw=gw, finished_view=True), disable_web_page_preview=True)
        await c.answer()
        return

    ok = await is_subscribed(c.from_user.id)
    if not ok:
        await c.answer("Сначала подпишитесь на канал.", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id FROM participants WHERE tg_id = ? AND giveaway_code = ?
        """, (c.from_user.id, GIVEAWAY_CODE))
        row = await cur.fetchone()
        if not row:
            await db.execute("""
                INSERT INTO participants(tg_id, giveaway_code, joined_at)
                VALUES (?, ?, ?)
            """, (c.from_user.id, GIVEAWAY_CODE, now_iso()))
            await db.commit()

    text = build_giveaway_text(gw=gw, finished_view=False, user_tg_id=c.from_user.id)
    try:
        await c.message.edit_text(text, disable_web_page_preview=True)
    except Exception:
        await c.message.answer(text, disable_web_page_preview=True)
    await c.answer("Участие подтверждено!")

@dp.message(Command("end"))
async def cmd_end(m: Message):
    if m.from_user.id != ORGANIZER_ADMIN_ID:
        await m.answer("Только организатор может завершить розыгрыш.", reply_markup=main_reply_keyboard())
        return

    gw = await get_giveaway()
    if not gw:
        await m.answer("Ошибка: розыгрыш не найден.", reply_markup=main_reply_keyboard())
        return

    if gw["status"] == "finished":
        await m.answer("Розыгрыш уже завершён.", reply_markup=main_reply_keyboard())
        return

    results_time = await set_giveaway_finished()
    sent = await notify_all_participants(results_time)

    gw2 = await get_giveaway()
    await m.answer(
        f"Розыгрыш #{GIVEAWAY_CODE} завершён.\n"
        f"Итоги зафиксированы: {fmt_dt(gw2['results_at'])}\n"
        f"Уведомлено участников: {sent}",
        reply_markup=main_reply_keyboard()
    )

# ================== ЗАПУСК ==================
async def main():
    await init_db()
    print("Bot started.")

    # (необязательно) можно также задать команды-меню, но они не нужны для клавиатуры
    await bot.set_my_commands([
        BotCommand(command="create", description="🎁 Создать розыгрыш"),
        BotCommand(command="my_giveaways", description="📝 Мои розыгрыши"),
        BotCommand(command="my_channels", description="📢 Мои каналы"),
    ])

    asyncio.create_task(auto_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
