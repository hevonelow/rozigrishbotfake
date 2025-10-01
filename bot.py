import os
from dotenv import load_dotenv
load_dotenv()

# Secure handling of secrets: BOT_TOKEN must be provided via environment variable.
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set. Create a .env file or set the BOT_TOKEN environment variable.")

import asyncio
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

# ============== КОНФИГ ИЗ ОКРУЖЕНИЯ ==============
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "snckintr")  # без @
ORGANIZER_LINK = os.getenv("ORGANIZER_LINK", "https://t.me/soqys")
ORGANIZER_ADMIN_ID = int(os.getenv("ORGANIZER_ADMIN_ID", "7738555379"))
GIVEAWAY_CODE = os.getenv("GIVEAWAY_CODE", "632")
PRIZE_COUNT = int(os.getenv("PRIZE_COUNT", "3"))
PRIZE_2_NAME = os.getenv("PRIZE_2_NAME", "100 долларов")
DB_PATH = os.getenv("DB_PATH", "giveaway.db")

# Новые переменные времени из .env (используются при старте)
GIVEAWAY_START = os.getenv("GIVEAWAY_START")  # "YYYY-MM-DD HH:MM" или "DD.MM.YYYY HH:MM" или ISO
GIVEAWAY_END   = os.getenv("GIVEAWAY_END")
# ================================================

# ================== BOT/DP ==================
if (not BOT_TOKEN) or (":" not in BOT_TOKEN) or (" " in BOT_TOKEN):
    raise RuntimeError("BOT_TOKEN пустой или в неверном формате")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ===== Простой «стейт» для админской рассылки (без FSM) =====
ADMIN_BROADCAST_WAIT = False   # ждём текст рассылки после /admin или /broadcast

# ================== УТИЛИТЫ ==================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_iso() -> str:
    return now_utc().isoformat()

def parse_human_dt_to_utc(s: str) -> datetime | None:
    """
    Парсит и приводит к UTC:
    - 'YYYY-MM-DD HH:MM'
    - 'DD.MM.YYYY HH:MM'
    - ISO 'YYYY-MM-DDTHH:MM(:SS)(+TZ)'
    """
    if not s:
        return None
    s = s.strip().replace("\u00A0", " ")
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
        return dt.astimezone().astimezone(timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.strptime(s, "%d.%m.%Y %H:%M")
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

def fmt_dt_local(iso_or_dt) -> str:
    """ISO-строку или datetime → 'YYYY-MM-DD HH:MM' (локальная зона)."""
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

async def safe_delete(msg: Message):
    try:
        await msg.delete()
    except Exception:
        pass

# ================== DB ==================
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
            prize_2 TEXT,
            start_at TEXT,
            end_at TEXT
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
                INSERT INTO giveaways(code, organizer_link, prize_count, created_at, status, prize_2, start_at, end_at)
                VALUES (?, ?, ?, ?, 'open', ?, NULL, NULL)
            """, (GIVEAWAY_CODE, ORGANIZER_LINK, PRIZE_COUNT, now_iso(), PRIZE_2_NAME))
            await db.commit()

async def get_giveaway():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT code, organizer_link, prize_count, created_at, results_at, status, prize_2, start_at, end_at
            FROM giveaways WHERE code = ?
        """, (GIVEAWAY_CODE,))
        r = await cur.fetchone()
        if not r:
            return None
        return {
            "code": r[0], "organizer_link": r[1], "prize_count": r[2],
            "created_at": r[3], "results_at": r[4], "status": r[5], "prize_2": r[6],
            "start_at": r[7], "end_at": r[8]
        }

async def set_times_in_db(start_dt_utc: datetime | None = None, end_dt_utc: datetime | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if start_dt_utc is not None and end_dt_utc is not None:
            await db.execute("UPDATE giveaways SET start_at=?, end_at=? WHERE code=?",
                             (start_dt_utc.isoformat(), end_dt_utc.isoformat(), GIVEAWAY_CODE))
        elif start_dt_utc is not None:
            await db.execute("UPDATE giveaways SET start_at=? WHERE code=?",
                             (start_dt_utc.isoformat(), GIVEAWAY_CODE))
        elif end_dt_utc is not None:
            await db.execute("UPDATE giveaways SET end_at=? WHERE code=?",
                             (end_dt_utc.isoformat(), GIVEAWAY_CODE))
        await db.commit()

async def set_giveaway_finished_with_results(results_dt_utc: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE giveaways SET status='finished', results_at=? WHERE code=?
        """, (results_dt_utc.isoformat(), GIVEAWAY_CODE))
        await db.commit()

# ================== ЛОГИКА СТАТУСА ==================
def calc_status(gw: dict) -> str:
    """'ожидается' | 'активен' | 'завершен' (учитывает start_at/end_at + статус из БД)."""
    if gw["status"] == "finished":
        return "завершен"
    now = now_utc()
    start = datetime.fromisoformat(gw["start_at"]).astimezone(timezone.utc) if gw["start_at"] else None
    end = datetime.fromisoformat(gw["end_at"]).astimezone(timezone.utc) if gw["end_at"] else None
    if start and now < start:
        return "ожидается"
    if end and now > end:
        return "завершен"
    return "активен"

# ================== ПОДПИСКА ==================
async def is_subscribed(user_id: int) -> bool:
    """Для приватных каналов get_chat_member может не сработать."""
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

# === ReplyKeyboard (как «меню») ===
def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎁 Создать розыгрыш")],
            [KeyboardButton(text="📝 Мои розыгрыши")],
            [KeyboardButton(text="📢 Мои каналы")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие…"
    )

# === Админ-меню (inline только для организатора) ===
def admin_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📣 Отправить сообщение всем", callback_data="admin_broadcast"))
    kb.row(InlineKeyboardButton(text="🕒 Показать времена", callback_data="admin_showtimes"))
    return kb.as_markup()

# ================== ТЕКСТЫ ==================
def build_giveaway_text(*, gw: dict, finished_view: bool = False, user_tg_id: int | None = None) -> str:
    code = gw["code"]
    status_word = calc_status(gw)
    header = (f"🎁 Теперь вы участник розыгрыша [#{code}, ваш ID: {user_tg_id}]\n\n"
              if (not finished_view and status_word != "завершен") else
              f"❌ Розыгрыш [#{code}] завершен.\n\n")
    start_txt = fmt_dt_local(gw["start_at"]) if gw["start_at"] else "—"
    end_txt = fmt_dt_local(gw["end_at"]) if gw["end_at"] else "—"
    body = (
        f"👑 Организатор розыгрыша: {gw['organizer_link']}\n"
        f"🏟️ Количество призовых мест: {gw['prize_count']}\n"
        f"🟢 Начало: {start_txt}\n"
        f"🔚 Конец: {end_txt}\n"
        f"⌚️ Создан: {fmt_dt_local(gw['created_at'])}\n"
        f"⌚️ Итоги: {fmt_dt_local(gw['results_at'])}\n\n"
        f"✅ Статус: {status_word}"
    )
    return header + body

# ================== УВЕДОМЛЕНИЯ ==================
async def notify_all_participants(results_time_iso: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT DISTINCT tg_id FROM participants
            WHERE giveaway_code = ? AND joined_at <= ?
            ORDER BY tg_id ASC
        """, (GIVEAWAY_CODE, results_time_iso))
        rows = await cur.fetchall()

    sent = 0
    for (uid,) in rows:
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

# ======= Админская рассылка всем участникам =======
async def broadcast_to_all(text_for_user: str) -> tuple[int, int]:
    """
    Отправляет сообщение всем участникам данного розыгрыша.
    Возвращает (успешно, всего_адресатов)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT DISTINCT tg_id FROM participants
            WHERE giveaway_code = ?
            ORDER BY tg_id ASC
        """, (GIVEAWAY_CODE,))
        users = [row[0] for row in (await cur.fetchall())]

    ok = 0
    total = len(users)

    for uid in users:
        # Попытаемся узнать username
        username_label = "пользователь"
        try:
            chat = await bot.get_chat(uid)
            if getattr(chat, "username", None):
                username_label = chat.username
        except Exception:
            pass

        formatted = (
            f"✉️ Сообщение от организатора для {username_label} [id {uid}]\n\n"
            f"{text_for_user}"
        )
        try:
            await bot.send_message(uid, formatted, disable_web_page_preview=True)
            ok += 1
            await asyncio.sleep(0.05)  # лёгкий троттлинг
        except Exception:
            # пользователь мог не запускать бота/заблокировал/ошибка доставки
            pass

    return ok, total

# ================== ЗАВЕРШЕНИЕ ==================
async def finish_if_due():
    """Если наступил end_at и розыгрыш ещё не завершён — завершает и рассылает сообщения."""
    gw = await get_giveaway()
    if not gw or gw["status"] == "finished":
        return False
    end = datetime.fromisoformat(gw["end_at"]).astimezone(timezone.utc) if gw["end_at"] else None
    if end and now_utc() >= end:
        await set_giveaway_finished_with_results(end)  # Итоги = конец
        await notify_all_participants(end.isoformat())
        return True
    return False

async def auto_watcher():
    try:
        await finish_if_due()   # проверка при старте
    except Exception as e:
        print("[AutoFinish@startup] Ошибка:", e)
    while True:
        try:
            await asyncio.sleep(20)
            await finish_if_due()
        except Exception as e:
            print("[AutoFinish@loop] Ошибка:", e)

# ================== ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЕЙ ==================
@dp.message(Command("start"))
async def cmd_start(m: Message):
    gw = await get_giveaway()
    if not gw:
        await m.answer("Ошибка: розыгрыш не найден.", reply_markup=main_reply_keyboard())
        return

    status_word = calc_status(gw)
    if status_word == "завершен":
        await m.answer(build_giveaway_text(gw=gw, finished_view=True),
                       disable_web_page_preview=True, reply_markup=main_reply_keyboard())
        return

    if status_word == "ожидается":
        await m.answer("⏳ Розыгрыш ещё не начался.\n\n" +
                       build_giveaway_text(gw=gw, finished_view=False, user_tg_id=m.from_user.id),
                       reply_markup=main_reply_keyboard(), disable_web_page_preview=True)
        return

    text = ("🛑 Для участия в розыгрыше вам необходимо подписаться на канал организатора.\n\n"
            "После подписки нажмите «♻ Проверить подписку».")
    await m.answer(text, reply_markup=main_reply_keyboard())
    await m.answer("Выберите действие:", reply_markup=subscribe_keyboard())

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(c: CallbackQuery):
    gw = await get_giveaway()
    if not gw:
        await c.answer("Ошибка: розыгрыш не найден.", show_alert=True)
        return

    status_word = calc_status(gw)
    if status_word == "завершен":
        try:
            await c.message.edit_text(build_giveaway_text(gw=gw, finished_view=True),
                                      disable_web_page_preview=True)
        except Exception:
            await c.message.answer(build_giveaway_text(gw=gw, finished_view=True),
                                   disable_web_page_preview=True)
        await c.answer()
        return

    ok = await is_subscribed(c.from_user.id)
    if not ok:
        await c.answer("Сначала подпишитесь на канал.", show_alert=True)
        return

    # регистрируем участника (если ещё нет)
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

# ================== ХЕНДЛЕРЫ АДМИНА (меню, время, рассылка) ==================
def admin_only(m: Message) -> bool:
    return m.from_user and (m.from_user.id == ORGANIZER_ADMIN_ID)

@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not admin_only(m):
        await m.answer("❌ Команда доступна только организатору.")
        return
    # ВСЕГДА шлём какой-то текст + кнопки
    txt = (
        "🔧 Админ-меню.\n\n"
        "• Нажмите «📣 Отправить сообщение всем» — затем пришлите текст одним сообщением, и я разошлю его всем участникам.\n"
        "• Или используйте команду: /broadcast <текст>\n"
        "• Посмотреть времена — кнопка «🕒 Показать времена»."
    )
    try:
        await m.answer(txt, reply_markup=admin_menu_keyboard())
    except Exception:
        # На всякий случай, если inline-кнопки не отрисуются
        await m.answer(txt + "\n\n(Кнопки не отобразились? Используйте /broadcast <текст>)")

@dp.callback_query(F.data == "admin_showtimes")
async def cb_admin_showtimes(c: CallbackQuery):
    if c.from_user.id != ORGANIZER_ADMIN_ID:
        await c.answer()
        return
    gw = await get_giveaway()
    if not gw:
        await c.message.answer("Розыгрыш не найден.")
        await c.answer()
        return
    start_txt = fmt_dt_local(gw["start_at"]) if gw["start_at"] else "—"
    end_txt = fmt_dt_local(gw["end_at"]) if gw["end_at"] else "—"
    await c.message.answer(f"🕒 Текущие времена:\n🟢 Начало: {start_txt}\n🔚 Конец: {end_txt}")
    await c.answer("")

@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(c: CallbackQuery):
    global ADMIN_BROADCAST_WAIT
    if c.from_user.id != ORGANIZER_ADMIN_ID:
        await c.answer()
        return
    ADMIN_BROADCAST_WAIT = True
    await c.message.answer(
        "✍️ Отправь текст для рассылки одним сообщением.\n\n"
        "Он будет отправлен всем участникам в формате:\n"
        "✉️ Сообщение от организатора для <username> [id <id>]\n\n"
        "<твой_текст>"
    )
    await c.answer("Жду текст 👍")

@dp.message(Command("broadcast"))
async def cmd_broadcast(m: Message):
    """Запасной вариант без кнопки: /broadcast <текст>"""
    global ADMIN_BROADCAST_WAIT
    if not admin_only(m):
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) == 1:
        ADMIN_BROADCAST_WAIT = True
        await m.answer("✍️ Пришлите текст для рассылки одним сообщением (или используйте: /broadcast <текст>).")
        return
    text_for_all = parts[1].strip()
    await m.answer("📤 Рассылаю…")
    ok, total = await broadcast_to_all(text_for_all)
    await m.answer(f"✅ Готово. Доставлено: {ok} из {total}.")

@dp.message(F.text)
async def admin_broadcast_catcher(m: Message):
    """
    Ловим текст сразу после нажатия кнопки «📣 Отправить сообщение всем»
    или после команды /broadcast без текста.
    """
    global ADMIN_BROADCAST_WAIT
    if not ADMIN_BROADCAST_WAIT:
        return
    if not admin_only(m):
        return

    ADMIN_BROADCAST_WAIT = False
    text_for_all = m.text.strip()
    if not text_for_all:
        await m.answer("Пустой текст. Нажми /admin и выбери «📣 Отправить сообщение всем» ещё раз.")
        return

    await m.answer("📤 Рассылаю… Это может занять немного времени.")
    ok, total = await broadcast_to_all(text_for_all)
    await m.answer(f"✅ Готово. Доставлено: {ok} из {total}.")

@dp.message(Command("set_start"))
async def cmd_set_start(m: Message):
    if not admin_only(m):
        return
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        await m.answer("Укажи время начала: /set_start YYYY-MM-DD HH:MM\nили: /set_start DD.MM.YYYY HH:MM")
        return
    dt = parse_human_dt_to_utc(args[1])
    if not dt:
        await m.answer("Не понял дату. Пример: 2025-10-05 21:00")
        return
    await set_times_in_db(start_dt_utc=dt)
    await m.answer(f"✅ Начало установлено: {fmt_dt_local(dt)}")

@dp.message(Command("set_end"))
async def cmd_set_end(m: Message):
    if not admin_only(m):
        return
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        await m.answer("Укажи время конца: /set_end YYYY-MM-DD HH:MM\nили: /set_end DD.MM.YYYY HH:MM")
        return
    dt = parse_human_dt_to_utc(args[1])
    if not dt:
        await m.answer("Не понял дату. Пример: 2025-10-05 21:00")
        return
    await set_times_in_db(end_dt_utc=dt)
    await m.answer(f"✅ Конец установлен: {fmt_dt_local(dt)}")

@dp.message(Command("show_times"))
async def cmd_show_times(m: Message):
    if not admin_only(m):
        return
    gw = await get_giveaway()
    if not gw:
        await m.answer("Розыгрыш не найден.")
        return
    start_txt = fmt_dt_local(gw["start_at"]) if gw["start_at"] else "—"
    end_txt = fmt_dt_local(gw["end_at"]) if gw["end_at"] else "—"
    await m.answer(f"🕒 Текущие времена:\n🟢 Начало: {start_txt}\n🔚 Конец: {end_txt}")

@dp.message(Command("end"))
async def cmd_end(m: Message):
    if not admin_only(m):
        await m.answer("Только организатор может завершить розыгрыш.", reply_markup=main_reply_keyboard())
        return

    gw = await get_giveaway()
    if not gw:
        await m.answer("Ошибка: розыгрыш не найден.", reply_markup=main_reply_keyboard())
        return
    if gw["status"] == "finished":
        await m.answer("Розыгрыш уже завершён.", reply_markup=main_reply_keyboard())
        return

    end_dt = datetime.fromisoformat(gw["end_at"]).astimezone(timezone.utc) if gw["end_at"] else now_utc()
    await set_giveaway_finished_with_results(end_dt)
    await notify_all_participants(end_dt.isoformat())

    gw2 = await get_giveaway()
    await m.answer(
        f"Розыгрыш #{GIVEAWAY_CODE} завершён.\n"
        f"Итоги зафиксированы: {fmt_dt_local(gw2['results_at'])}",
        reply_markup=main_reply_keyboard()
    )

# ================== ЗАПУСК ==================
async def main():
    await init_db()
    print("Bot started.")

    # Автоустановка дат из .env (если заданы)
    start_dt = parse_human_dt_to_utc(GIVEAWAY_START)
    end_dt   = parse_human_dt_to_utc(GIVEAWAY_END)
    if start_dt or end_dt:
        await set_times_in_db(start_dt_utc=start_dt, end_dt_utc=end_dt)

    # На всякий случай: если когда-то ставил webhook — выключим перед polling
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    # Команды можно показывать или скрыть по желанию
    await bot.set_my_commands([
        BotCommand(command="admin", description="Админ-меню"),
        BotCommand(command="set_start", description="Установить начало"),
        BotCommand(command="set_end", description="Установить конец"),
        BotCommand(command="show_times", description="Показать времена"),
        BotCommand(command="broadcast", description="Рассылка всем"),
    ])

    asyncio.create_task(auto_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
