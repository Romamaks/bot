import asyncio
import csv
import io
import logging
import os
import sqlite3
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAIN_CHANNEL = os.getenv("MAIN_CHANNEL", "").strip()
MAIN_CHANNEL_URL = os.getenv("MAIN_CHANNEL_URL", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "movies.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in .env")
if not MAIN_CHANNEL or not MAIN_CHANNEL_URL:
    raise RuntimeError("MAIN_CHANNEL and MAIN_CHANNEL_URL are required")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row


def now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    db.executescript("""
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS movies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        year TEXT DEFAULT '',
        genre TEXT DEFAULT '',
        description TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        first_name TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        lookups INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS lookup_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        code TEXT NOT NULL,
        found INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS required_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        chat_id TEXT NOT NULL UNIQUE,
        url TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    """)
    db.commit()

    # Migrate old single-channel setup into the new required-channels table.
    if MAIN_CHANNEL:
        exists = db.execute(
            "SELECT 1 FROM required_channels WHERE chat_id=?", (MAIN_CHANNEL,)
        ).fetchone()
        if not exists:
            db.execute(
                "INSERT OR IGNORE INTO required_channels(title, chat_id, url, enabled, created_at) "
                "VALUES(?, ?, ?, 1, ?)",
                ("Основной канал", MAIN_CHANNEL, MAIN_CHANNEL_URL, now())
            )
            db.commit()


def is_admin(user_id):
    return user_id in ADMIN_IDS


def save_user(user):
    t = now()
    db.execute("""
        INSERT INTO users(user_id, username, first_name, created_at, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_seen=excluded.last_seen
    """, (user.id, user.username or "", user.first_name or "", t, t))
    db.commit()


def required_channels():
    return db.execute(
        "SELECT * FROM required_channels WHERE enabled=1 ORDER BY id"
    ).fetchall()


def all_channels():
    return db.execute(
        "SELECT * FROM required_channels ORDER BY id"
    ).fetchall()


async def channel_subscribed(user_id, chat_id):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.RESTRICTED,
        }
    except Exception:
        logging.exception("Subscription check failed: user=%s chat=%s", user_id, chat_id)
        return False


async def missing_channels(user_id):
    missing = []
    for ch in required_channels():
        if not await channel_subscribed(user_id, ch["chat_id"]):
            missing.append(ch)
    return missing


def subscription_keyboard(missing=None):
    missing = missing if missing is not None else required_channels()
    rows = []
    for ch in missing:
        rows.append([InlineKeyboardButton(
            text=f"📢 {ch['title']}", url=ch["url"]
        )])
    rows.append([InlineKeyboardButton(
        text="✅ Проверить подписки", callback_data="check_all_subs"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_keyboard(user_id):
    rows = [
        [KeyboardButton(text="🎬 Проверить код")],
        [KeyboardButton(text="📢 Каналы"), KeyboardButton(text="ℹ️ Помощь")]
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить фильм", callback_data="a_add"),
         InlineKeyboardButton(text="🗑 Удалить фильм", callback_data="a_del")],
        [InlineKeyboardButton(text="📋 Коды", callback_data="a_list"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="a_stats")],
        [InlineKeyboardButton(text="📢 Подписки / реклама", callback_data="a_channels")],
        [InlineKeyboardButton(text="📥 Импорт CSV", callback_data="a_csv"),
         InlineKeyboardButton(text="📣 Рассылка", callback_data="a_broadcast")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="a_close")]
    ])


def channels_keyboard():
    rows = [
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data="ch_add")],
        [InlineKeyboardButton(text="📋 Список каналов", callback_data="ch_list")],
        [InlineKeyboardButton(text="🗑 Удалить канал", callback_data="ch_delete")],
        [InlineKeyboardButton(text="🔄 Включить/выключить", callback_data="ch_toggle")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_admin")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


class AddMovie(StatesGroup):
    code = State()
    title = State()
    year = State()
    genre = State()
    description = State()


class DeleteMovie(StatesGroup):
    code = State()


class Broadcast(StatesGroup):
    text = State()


class ImportCSV(StatesGroup):
    file = State()


class AddChannel(StatesGroup):
    title = State()
    chat_id = State()
    url = State()


class DeleteChannel(StatesGroup):
    chat_id = State()


class ToggleChannel(StatesGroup):
    chat_id = State()


def add_movie(code, title, year="", genre="", description=""):
    db.execute("""
        INSERT INTO movies(code,title,year,genre,description,created_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(code) DO UPDATE SET
        title=excluded.title, year=excluded.year,
        genre=excluded.genre, description=excluded.description
    """, (code.upper().strip(), title.strip(), year.strip(), genre.strip(),
          description.strip(), now()))
    db.commit()


def delete_movie(code):
    cur = db.execute("DELETE FROM movies WHERE code=?", (code.upper().strip(),))
    db.commit()
    return cur.rowcount > 0


@dp.message(CommandStart())
async def start(message: Message):
    save_user(message.from_user)
    await message.answer(
        "🎬 <b>КиноКоды</b>\n\n"
        "Отправь код фильма или сериала — я покажу его название.\n\n"
        "🔐 Для доступа нужно быть подписанным на обязательные каналы.",
        reply_markup=main_keyboard(message.from_user.id)
    )
    missing = await missing_channels(message.from_user.id)
    if missing:
        await message.answer(
            "📢 <b>Подпишись на каналы ниже:</b>",
            reply_markup=subscription_keyboard(missing)
        )


@dp.callback_query(F.data == "check_all_subs")
async def check_all_subs(call: CallbackQuery):
    missing = await missing_channels(call.from_user.id)
    if not missing:
        await call.answer("✅ Все подписки подтверждены!")
        await call.message.edit_text(
            "✅ <b>Все подписки подтверждены!</b>\n\n"
            "Теперь отправь код фильма или сериала."
        )
    else:
        names = "\n".join(f"• {x['title']}" for x in missing)
        await call.answer("❌ Есть неподтверждённые подписки.", show_alert=True)
        await call.message.edit_text(
            "🔒 <b>Нужно подписаться на все каналы:</b>\n\n" + names +
            "\n\nПосле подписки нажми кнопку ниже.",
            reply_markup=subscription_keyboard(missing)
        )


@dp.message(F.text == "📢 Каналы")
async def channels(message: Message):
    rows = required_channels()
    if not rows:
        await message.answer("Список обязательных каналов пуст.")
        return
    await message.answer(
        "📢 <b>Каналы для подписки</b>\n\n"
        "Подпишись на все каналы, затем нажми «Проверить подписки».",
        reply_markup=subscription_keyboard(rows)
    )


@dp.message(F.text == "ℹ️ Помощь")
@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "ℹ️ <b>Как пользоваться</b>\n\n"
        "1️⃣ Подпишись на все обязательные каналы.\n"
        "2️⃣ Отправь код.\n"
        "3️⃣ Получи название фильма или сериала.\n\n"
        "Пример: <code>7F3K-9D2L-4Q8M</code>"
    )


@dp.message(F.text == "🎬 Проверить код")
async def ask_code(message: Message):
    await message.answer("🔑 Отправь код одним сообщением:")


@dp.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message):
    if is_admin(message.from_user.id):
        await message.answer("⚙️ <b>Панель администратора</b>", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "back_admin")
async def back_admin(call: CallbackQuery):
    if is_admin(call.from_user.id):
        await call.message.edit_text("⚙️ <b>Панель администратора</b>", reply_markup=admin_keyboard())
    await call.answer()


@dp.callback_query(F.data == "a_channels")
async def channel_admin(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.message.edit_text(
        "📢 <b>Подписки и рекламодатели</b>\n\n"
        "Здесь можно добавлять каналы рекламодателей.\n"
        "После добавления бот будет требовать подписку на них вместе с твоим каналом.",
        reply_markup=channels_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "ch_add")
async def ch_add_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AddChannel.title)
    await call.message.answer(
        "➕ <b>Добавление рекламного канала</b>\n\n"
        "1/3. Введи название, например:\n"
        "<code>Канал рекламодателя №1</code>"
    )
    await call.answer()


@dp.message(AddChannel.title)
async def ch_add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AddChannel.chat_id)
    await message.answer(
        "2/3. Введи @username канала или его numeric chat_id.\n\n"
        "Пример: <code>@reklama_channel</code>"
    )


@dp.message(AddChannel.chat_id)
async def ch_add_chat(message: Message, state: FSMContext):
    await state.update_data(chat_id=message.text.strip())
    await state.set_state(AddChannel.url)
    await message.answer(
        "3/3. Введи ссылку на канал:\n"
        "<code>https://t.me/reklama_channel</code>"
    )


@dp.message(AddChannel.url)
async def ch_add_url(message: Message, state: FSMContext):
    data = await state.get_data()
    url = message.text.strip()
    if not (url.startswith("https://t.me/") or url.startswith("http://t.me/")):
        await message.answer("❌ Нужна ссылка вида https://t.me/username")
        return

    try:
        db.execute(
            "INSERT INTO required_channels(title,chat_id,url,enabled,created_at) VALUES(?,?,?,?,?)",
            (data["title"], data["chat_id"], url, 1, now())
        )
        db.commit()
    except sqlite3.IntegrityError:
        await message.answer("❌ Такой канал уже добавлен.")
        await state.clear()
        return

    await state.clear()

    # Check bot access immediately.
    try:
        me = await bot.get_chat_member(data["chat_id"], (await bot.get_me()).id)
        bot_is_admin = me.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
    except Exception:
        bot_is_admin = False

    extra = (
        "✅ Бот видит канал как администратора."
        if bot_is_admin else
        "⚠️ Добавлено, но бот не подтвердил права администратора. "
        "Добавь бота администратором этого канала, иначе проверка подписки может не работать."
    )
    await message.answer(
        f"✅ <b>Канал добавлен</b>\n\n"
        f"📢 {data['title']}\n"
        f"🔗 {url}\n\n{extra}",
        reply_markup=channels_keyboard()
    )


@dp.callback_query(F.data == "ch_list")
async def ch_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    rows = all_channels()
    if not rows:
        text = "📋 Обязательных каналов нет."
    else:
        lines = ["📋 <b>Каналы подписки</b>\n"]
        for r in rows:
            status = "🟢 включён" if r["enabled"] else "⚪ выключен"
            lines.append(f"#{r['id']} — <b>{r['title']}</b> — {status}\n<code>{r['chat_id']}</code>")
        text = "\n".join(lines)
    await call.message.answer(text)
    await call.answer()


@dp.callback_query(F.data == "ch_delete")
async def ch_delete_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(DeleteChannel.chat_id)
    await call.message.answer(
        "🗑 Введи ID канала из списка (#ID), например <code>2</code>, "
        "или его @username."
    )
    await call.answer()


@dp.message(DeleteChannel.chat_id)
async def ch_delete_finish(message: Message, state: FSMContext):
    value = message.text.strip().lstrip("#")
    if value.isdigit():
        cur = db.execute("DELETE FROM required_channels WHERE id=?", (int(value),))
    else:
        cur = db.execute("DELETE FROM required_channels WHERE chat_id=?", (value,))
    db.commit()
    await state.clear()
    await message.answer(
        "✅ Канал удалён." if cur.rowcount else "❌ Канал не найден.",
        reply_markup=channels_keyboard()
    )


@dp.callback_query(F.data == "ch_toggle")
async def ch_toggle_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(ToggleChannel.chat_id)
    await call.message.answer("🔄 Введи ID канала из списка, например <code>2</code>:")
    await call.answer()


@dp.message(ToggleChannel.chat_id)
async def ch_toggle_finish(message: Message, state: FSMContext):
    value = message.text.strip().lstrip("#")
    if not value.isdigit():
        await message.answer("Нужен числовой ID канала из списка.")
        return
    row = db.execute("SELECT enabled FROM required_channels WHERE id=?", (int(value),)).fetchone()
    if not row:
        await message.answer("❌ Канал не найден.")
        await state.clear()
        return
    new_value = 0 if row["enabled"] else 1
    db.execute("UPDATE required_channels SET enabled=? WHERE id=?", (new_value, int(value)))
    db.commit()
    await state.clear()
    await message.answer(
        "🟢 Канал включён." if new_value else "⚪ Канал выключен.",
        reply_markup=channels_keyboard()
    )


@dp.callback_query(F.data == "a_close")
async def admin_close(call: CallbackQuery):
    if is_admin(call.from_user.id):
        await call.message.delete()


# Movie admin
@dp.callback_query(F.data == "a_add")
async def add_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AddMovie.code)
    await call.message.answer("➕ Отправь код:")
    await call.answer()


@dp.message(AddMovie.code)
async def add_code(message: Message, state: FSMContext):
    await state.update_data(code=message.text.strip())
    await state.set_state(AddMovie.title)
    await message.answer("🎬 Название:")


@dp.message(AddMovie.title)
async def add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AddMovie.year)
    await message.answer("📅 Год или «-»:")


@dp.message(AddMovie.year)
async def add_year(message: Message, state: FSMContext):
    await state.update_data(year="" if message.text.strip() == "-" else message.text.strip())
    await state.set_state(AddMovie.genre)
    await message.answer("🎭 Жанр или «-»:")


@dp.message(AddMovie.genre)
async def add_genre(message: Message, state: FSMContext):
    await state.update_data(genre="" if message.text.strip() == "-" else message.text.strip())
    await state.set_state(AddMovie.description)
    await message.answer("📝 Описание или «-»:")


@dp.message(AddMovie.description)
async def add_description(message: Message, state: FSMContext):
    d = await state.get_data()
    desc = "" if message.text.strip() == "-" else message.text.strip()
    add_movie(d["code"], d["title"], d["year"], d["genre"], desc)
    await state.clear()
    await message.answer(
        f"✅ Сохранено!\n\n🔑 <code>{d['code'].upper()}</code>\n🎬 <b>{d['title']}</b>",
        reply_markup=main_keyboard(message.from_user.id)
    )


@dp.callback_query(F.data == "a_del")
async def del_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(DeleteMovie.code)
    await call.message.answer("🗑 Отправь код фильма для удаления:")
    await call.answer()


@dp.message(DeleteMovie.code)
async def del_finish(message: Message, state: FSMContext):
    ok = delete_movie(message.text)
    await state.clear()
    await message.answer("✅ Код удалён." if ok else "❌ Код не найден.")


@dp.callback_query(F.data == "a_list")
async def movie_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    rows = db.execute("SELECT code,title,year FROM movies ORDER BY id DESC LIMIT 100").fetchall()
    text = "📋 <b>Коды</b>\n\n" + (
        "\n".join(f"<code>{r['code']}</code> — {r['title']}" for r in rows)
        if rows else "База пуста."
    )
    await call.message.answer(text)
    await call.answer()


@dp.callback_query(F.data == "a_stats")
async def stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    users = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    movies = db.execute("SELECT COUNT(*) c FROM movies").fetchone()["c"]
    lookups = db.execute("SELECT COUNT(*) c FROM lookup_log").fetchone()["c"]
    await call.message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"🎬 Кодів: <b>{movies}</b>\n"
        f"🔎 Проверок: <b>{lookups}</b>\n"
        f"📢 Обязательных каналов: <b>{len(required_channels())}</b>"
    )
    await call.answer()


@dp.callback_query(F.data == "a_csv")
async def csv_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(ImportCSV.file)
    await call.message.answer("📥 Пришли CSV с колонками code,title,year,genre,description")
    await call.answer()


@dp.message(ImportCSV.file, F.document)
async def csv_import(message: Message, state: FSMContext):
    doc = await bot.get_file(message.document.file_id)
    data = io.BytesIO()
    await bot.download_file(doc.file_path, data)
    data.seek(0)
    reader = csv.DictReader(io.StringIO(data.read().decode("utf-8-sig")))
    count = 0
    for row in reader:
        if row.get("code") and row.get("title"):
            add_movie(row["code"], row["title"], row.get("year",""),
                      row.get("genre",""), row.get("description",""))
            count += 1
    await state.clear()
    await message.answer(f"✅ Импортировано: <b>{count}</b>")


@dp.callback_query(F.data == "a_broadcast")
async def broadcast_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(Broadcast.text)
    await call.message.answer("📣 Отправь текст для рассылки:")
    await call.answer()


@dp.message(Broadcast.text)
async def broadcast_send(message: Message, state: FSMContext):
    users = db.execute("SELECT user_id FROM users").fetchall()
    await state.clear()
    sent = failed = 0
    for r in users:
        try:
            await bot.send_message(r["user_id"], message.text)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1
    await message.answer(f"📣 Готово.\n\n✅ {sent}\n❌ {failed}")


@dp.message(F.text)
async def lookup(message: Message):
    if message.text.startswith("/"):
        return
    save_user(message.from_user)

    missing = await missing_channels(message.from_user.id)
    if missing:
        await message.answer(
            "🔒 <b>Доступ закрыт</b>\n\n"
            "Нужно подписаться на все обязательные каналы.",
            reply_markup=subscription_keyboard(missing)
        )
        return

    code = message.text.strip()
    movie = db.execute("SELECT * FROM movies WHERE code=?", (code.upper(),)).fetchone()

    db.execute("UPDATE users SET lookups=lookups+1,last_seen=? WHERE user_id=?",
               (now(), message.from_user.id))
    db.execute(
        "INSERT INTO lookup_log(user_id,code,found,created_at) VALUES(?,?,?,?)",
        (message.from_user.id, code.upper(), 1 if movie else 0, now())
    )
    db.commit()

    if not movie:
        await message.answer("❌ <b>Код не найден</b>\n\nПроверь код и попробуй ещё раз.")
        return

    lines = [f"🎬 <b>{movie['title']}</b>"]
    if movie["year"]: lines.append(f"📅 {movie['year']}")
    if movie["genre"]: lines.append(f"🎭 {movie['genre']}")
    if movie["description"]: lines.append(f"\n📝 {movie['description']}")
    lines.append(f"\n🔑 Код: <code>{movie['code']}</code>")
    await message.answer("\n".join(lines))


async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("КиноКоды v3 started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
