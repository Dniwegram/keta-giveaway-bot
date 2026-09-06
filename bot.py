"""
Telegram-бот для розыгрыша с проверкой подписки на канал и загрузкой фото-заявки.

Сценарий:
1. Пользователь жмёт /start -> видит кнопку "Участвовать".
2. Бот проверяет подписку на канал (get_chat_member).
   - Не подписан -> кнопки "Подписаться" + "Я подписался, проверить".
   - Подписан -> просит прислать фото.
3. Пользователь присылает фото -> бот просит номер телефона -> заявка сохраняется в БД
   (одна заявка на пользователя) и пересылается в приватный админ-чат/канал живой лентой.
4. Админ-команды (доступны только ADMIN_IDS):
   /stats        - сколько всего заявок
   /list         - список всех заявок текстом
   /delete <id>  - удалить заявку по номеру
   /export       - выгрузить все заявки в CSV
   /pick_winner  - случайно выбрать победителя из заявок

Технологии: aiogram 3.x, aiosqlite (SQLite), python-dotenv.
"""

import asyncio
import csv
import io
import logging
import os
import random
import re
import sqlite3
from datetime import datetime

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "")  # @username канала или -100xxxxxxxxxx
CHANNEL_URL = os.getenv("CHANNEL_URL", "")  # ссылка для кнопки "Подписаться", напр. https://t.me/mychannel
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID", "")  # чат/канал, куда падает лента заявок
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
DB_PATH = os.getenv("DB_PATH", "giveaway.db")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан. Проверь файл .env")
if not CHANNEL_ID_RAW:
    raise SystemExit("CHANNEL_ID не задан. Проверь файл .env")
if not ADMIN_CHAT_ID_RAW:
    raise SystemExit("ADMIN_CHAT_ID не задан. Проверь файл .env")


def _parse_chat_id(raw: str):
    """@username оставляем строкой, числовой id приводим к int."""
    raw = raw.strip()
    if raw.startswith("@"):
        return raw
    try:
        return int(raw)
    except ValueError:
        return raw


CHANNEL_ID = _parse_chat_id(CHANNEL_ID_RAW)
ADMIN_CHAT_ID = _parse_chat_id(ADMIN_CHAT_ID_RAW)

OK_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("giveaway-bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


# --------------------------------------------------------------------------- #
# FSM состояния
# --------------------------------------------------------------------------- #


class Entry(StatesGroup):
    waiting_photo = State()
    waiting_phone = State()


# --------------------------------------------------------------------------- #
# База данных
# --------------------------------------------------------------------------- #

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE NOT NULL,
    username TEXT,
    full_name TEXT,
    photo_file_id TEXT NOT NULL,
    phone TEXT,
    created_at TEXT NOT NULL
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        # Миграция для баз, созданных до появления номера телефона.
        try:
            await db.execute("ALTER TABLE entries ADD COLUMN phone TEXT")
        except sqlite3.OperationalError:
            pass  # столбец уже есть
        await db.commit()


async def get_entry(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM entries WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone()


async def get_entry_by_id(entry_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)) as cur:
            return await cur.fetchone()


async def delete_entry(entry_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        await db.commit()
        return cursor.rowcount > 0


async def add_entry(user_id: int, username: str, full_name: str, photo_file_id: str, phone: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO entries (user_id, username, full_name, photo_file_id, phone, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                user_id,
                username,
                full_name,
                photo_file_id,
                phone,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def count_entries() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM entries") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def all_entries():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM entries ORDER BY id") as cur:
            return await cur.fetchall()


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #


def join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🎉 Участвую", callback_data="join")]]
    )


def subscribe_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    if CHANNEL_URL:
        buttons.append([InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL)])
    buttons.append([InlineKeyboardButton(text="✅ Я подписался, проверить", callback_data="recheck")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось проверить подписку user_id=%s: %s", user_id, e)
        return False
    return member.status in OK_STATUSES


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# --------------------------------------------------------------------------- #
# Хендлеры: участники
# --------------------------------------------------------------------------- #


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🎉 Привет! Здесь проходит розыгрыш iPhone!\n\n"
        "<b>Чтобы участвовать, нужно:</b>\n"
        f'• быть подписанным на канал <a href="{CHANNEL_URL}">@ketamedia</a>\n'
        "• снять видео до 8 мин. о поездке по Хабаровскому краю и отправить его на сайт "
        "путешественникдв.рф\n"
        "• отправить в бота скриншот отправленной заявки — так мы сможем отследить, что ты её присылал\n"
        "• оставить номер телефона для связи, если выиграешь\n"
        "• нажать на кнопку «Участвую»\n\n"
        "Победителя определим на фестивале «Амур. Живая линия», который пройдёт на площади "
        "им. Ленина 12 и 13 сентября.",
        reply_markup=join_keyboard(),
    )


async def _handle_join_flow(user_id: int, message_to_edit: Message, state: FSMContext) -> None:
    existing = await get_entry(user_id)
    if existing:
        await message_to_edit.answer(
            f"Вы уже участвуете в розыгрыше 🎉\nНомер вашей заявки: <b>#{existing['id']}</b>"
        )
        return

    if await is_subscribed(user_id):
        await state.set_state(Entry.waiting_photo)
        await message_to_edit.answer(
            "Отлично, подписка подтверждена ✅\n\n"
            "Теперь пришлите скриншот отправленной заявки на сайте путешественникдв.рф — "
            "это подтвердит ваше участие в розыгрыше."
        )
    else:
        await message_to_edit.answer(
            "Чтобы участвовать, сначала подпишитесь на канал, затем нажмите «Я подписался».",
            reply_markup=subscribe_keyboard(),
        )


@dp.callback_query(F.data == "join")
async def cb_join(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _handle_join_flow(callback.from_user.id, callback.message, state)


@dp.callback_query(F.data == "recheck")
async def cb_recheck(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if await is_subscribed(callback.from_user.id):
        await _handle_join_flow(callback.from_user.id, callback.message, state)
    else:
        await callback.answer("Подписка пока не найдена. Проверь, что подписался, и попробуй снова.", show_alert=True)


@dp.message(Entry.waiting_photo, F.photo)
async def handle_photo(message: Message, state: FSMContext) -> None:
    user = message.from_user

    # На всякий случай перепроверяем подписку прямо перед тем, как двигаться дальше.
    if not await is_subscribed(user.id):
        await state.clear()
        await message.answer(
            "Подписка на канал не найдена — заявка не принята. Подпишитесь и начните заново: /start",
            reply_markup=subscribe_keyboard(),
        )
        return

    if await get_entry(user.id):
        await state.clear()
        await message.answer("Заявка от вас уже есть, повторно участвовать нельзя 🙂")
        return

    photo_file_id = message.photo[-1].file_id  # самое большое разрешение
    await state.update_data(photo_file_id=photo_file_id)
    await state.set_state(Entry.waiting_phone)
    await message.answer(
        "Отлично, скриншот получен ✅\n\n"
        "Теперь пришлите номер телефона для связи (если выиграете), например: <code>+79141234567</code>"
    )


@dp.message(Entry.waiting_photo)
async def handle_wrong_content(message: Message) -> None:
    await message.answer(
        "Нужно прислать именно скриншот заявки как фото (не файлом и не текстом) — попробуйте ещё раз."
    )


PHONE_DIGITS_RE = re.compile(r"\D")


@dp.message(Entry.waiting_phone, F.text)
async def handle_phone(message: Message, state: FSMContext) -> None:
    user = message.from_user
    phone_raw = message.text.strip()
    digits_only = PHONE_DIGITS_RE.sub("", phone_raw)

    if not (10 <= len(digits_only) <= 12):
        await message.answer(
            "Похоже, это не номер телефона. Пришлите номер в формате, например: <code>+79141234567</code>"
        )
        return

    data = await state.get_data()
    photo_file_id = data.get("photo_file_id")
    if not photo_file_id:
        # Данные потерялись (например, бот перезапускался между шагами) — просим начать заново.
        await state.clear()
        await message.answer("Что-то пошло не так, начните заново: /start")
        return

    # Ещё раз перепроверяем подписку и отсутствие дубля перед финальным сохранением.
    if not await is_subscribed(user.id):
        await state.clear()
        await message.answer(
            "Подписка на канал не найдена — заявка не принята. Подпишитесь и начните заново: /start",
            reply_markup=subscribe_keyboard(),
        )
        return

    if await get_entry(user.id):
        await state.clear()
        await message.answer("Заявка от вас уже есть, повторно участвовать нельзя 🙂")
        return

    entry_id = await add_entry(
        user_id=user.id,
        username=user.username or "",
        full_name=user.full_name,
        photo_file_id=photo_file_id,
        phone=phone_raw,
    )
    await state.clear()

    await message.answer(
        f"Заявка принята! 🎉 Ваш номер: <b>#{entry_id}</b>\n"
        "Победителя определим на фестивале «Амур. Живая линия» (12–13 сентября, площадь им. Ленина)."
    )

    username_line = f"Username: @{user.username}" if user.username else "Username: —"
    caption = (
        f"📝 Новая заявка <b>#{entry_id}</b>\n"
        f"Имя: {user.full_name}\n"
        f"{username_line}\n"
        f"Телефон: <code>{phone_raw}</code>\n"
        f"ID: <code>{user.id}</code>\n"
        f"Время: {datetime.utcnow().isoformat(timespec='seconds')} UTC"
    )

    try:
        await bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=photo_file_id, caption=caption)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.error("Не удалось отправить заявку в админ-чат: %s", e)


@dp.message(Entry.waiting_phone)
async def handle_phone_wrong_content(message: Message) -> None:
    await message.answer("Пришлите номер телефона текстом, например: <code>+79141234567</code>")


# --------------------------------------------------------------------------- #
# Хендлеры: админка
# --------------------------------------------------------------------------- #


@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    total = await count_entries()
    await message.answer(f"Всего заявок: <b>{total}</b>")


@dp.message(Command("list"))
async def cmd_list(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    rows = await all_entries()
    if not rows:
        await message.answer("Пока нет ни одной заявки.")
        return

    lines = []
    for r in rows:
        username_part = f"@{r['username']}" if r["username"] else "—"
        phone_part = r["phone"] or "—"
        lines.append(
            f"#{r['id']} — {r['full_name']} ({username_part}) id:{r['user_id']} "
            f"тел: {phone_part} — {r['created_at']} UTC"
        )

    # Telegram режет сообщения по ~4096 символов — на всякий случай бьём список на части.
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk)

    await message.answer("Чтобы удалить заявку: /delete <номер>, например /delete 12")


@dp.message(Command("delete"))
async def cmd_delete(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return

    if not command.args or not command.args.strip().isdigit():
        await message.answer("Использование: /delete <номер_заявки>\nНапример: /delete 12")
        return

    entry_id = int(command.args.strip())
    entry = await get_entry_by_id(entry_id)
    if not entry:
        await message.answer(f"Заявка #{entry_id} не найдена.")
        return

    await delete_entry(entry_id)
    await message.answer(f"Заявка #{entry_id} ({entry['full_name']}) удалена.")

    try:
        await bot.send_message(
            entry["user_id"],
            "Ваша заявка на розыгрыш была отклонена модератором (не подошёл присланный скриншот). "
            "Вы можете отправить новую заявку — напишите /start и пройдите шаги заново.",
        )
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось уведомить пользователя %s об удалении заявки: %s", entry["user_id"], e)


@dp.message(Command("export"))
async def cmd_export(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    rows = await all_entries()
    if not rows:
        await message.answer("Пока нет ни одной заявки.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "user_id", "username", "full_name", "phone", "photo_file_id", "created_at_utc"])
    for r in rows:
        writer.writerow(
            [r["id"], r["user_id"], r["username"], r["full_name"], r["phone"], r["photo_file_id"], r["created_at"]]
        )

    data = buf.getvalue().encode("utf-8-sig")  # BOM, чтобы Excel не ломал кириллицу
    filename = f"entries_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    await message.answer_document(BufferedInputFile(data, filename=filename))


@dp.message(Command("pick_winner"))
async def cmd_pick_winner(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    rows = await all_entries()
    if not rows:
        await message.answer("Заявок ещё нет — выбирать не из чего.")
        return

    winner = random.choice(rows)
    username_line = f"Username: @{winner['username']}" if winner["username"] else "Username: —"
    caption = (
        f"🏆 Победитель: заявка <b>#{winner['id']}</b>\n"
        f"Имя: {winner['full_name']}\n"
        f"{username_line}\n"
        f"Телефон: <code>{winner['phone'] or '—'}</code>\n"
        f"ID: <code>{winner['user_id']}</code>"
    )
    await message.answer_photo(photo=winner["photo_file_id"], caption=caption)


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #


async def main() -> None:
    await init_db()
    logger.info("Бот запущен, начинаю polling...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
