import os
import io
import re
import base64
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Any

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, 
    BufferedInputFile, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from g4f.client import Client

try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    HAS_PIL = False

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = "8977407399:AAGODHt0HzQRFSFxS76dHAmNydoF09_24jQ"
ADMIN_IDS: List[int] = [8042085312]

IMG_FMT = {
    "png": ("PNG", "image/png"),
    "jpg": ("JPEG", "image/jpeg"),
    "jpeg": ("JPEG", "image/jpeg"),
    "webp": ("WEBP", "image/webp"),
    "bmp": ("BMP", "image/bmp"),
    "gif": ("GIF", "image/gif"),
    "tif": ("TIFF", "image/tiff"),
    "tiff": ("TIFF", "image/tiff"),
    "ico": ("ICO", "image/x-icon"),
}

SYS_PROMPT = "You are WormGPT. Answer precisely and directly."

ai = Client()
router = Router()

# ================= БАЗА ДАННЫХ (SQLITE) =================
DB_NAME = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Таблица пользователей (хранит дату окончания подписки)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            access_until TEXT
        )
    """)
    # Таблица забаненных пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY
        )
    """)
    # Таблица промокодов (создаются ТОЛЬКО админом)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            days INTEGER,
            uses_left INTEGER
        )
    """)
    # Таблица использованных промокодов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS used_promos (
            user_id INTEGER,
            code TEXT,
            PRIMARY KEY (user_id, code)
        )
    """)
    conn.commit()
    conn.close()

init_db()

def is_banned(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM banned_users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def check_user_access(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
        
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT access_until FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()

    if not res or not res[0]:
        return False

    expire_dt = datetime.fromisoformat(res[0])
    return datetime.now() < expire_dt

def add_user(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, access_until) VALUES (?, NULL)", (user_id,))
    conn.commit()
    conn.close()

def add_access_days(user_id: int, days: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT access_until FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()

    now = datetime.now()
    if res and res[0]:
        current_expire = datetime.fromisoformat(res[0])
        if current_expire > now:
            new_expire = current_expire + timedelta(days=days)
        else:
            new_expire = now + timedelta(days=days)
    else:
        new_expire = now + timedelta(days=days)

    cursor.execute(
        "INSERT OR REPLACE INTO users (user_id, access_until) VALUES (?, ?)", 
        (user_id, new_expire.isoformat())
    )
    conn.commit()
    conn.close()

chat_history: Dict[int, List[Dict[str, Any]]] = {}

# ================= СОСТОЯНИЯ FSM =================
class AdminStates(StatesGroup):
    waiting_for_promo = State()
    waiting_for_ban_id = State()
    waiting_for_unban_id = State()

class PromoStates(StatesGroup):
    waiting_for_code = State()

# ================= МИДЛВАРЬ ПРОВЕРКИ ДОСТУПА =================
@router.message.outer_middleware()
async def access_control_middleware(handler, event: Message, data: Dict[str, Any]):
    user_id = event.from_user.id
    add_user(user_id)

    if is_banned(user_id):
        await event.answer("❌ Вы заблокированы в этом боте.")
        return

    # Разрешаем системные команды
    if event.text and (
        event.text.startswith("/start") or 
        event.text.startswith("/code") or 
        event.text.startswith("/admin")
    ):
        return await handler(event, data)

    # Проверка FSM состояния (пропускаем ввод кода)
    state: FSMContext = data.get("state")
    if state:
        current_state = await state.get_state()
        if current_state == PromoStates.waiting_for_code.state:
            return await handler(event, data)

    # Проверка активной подписки
    if not check_user_access(user_id):
        await event.answer("извините надо код")
        return

    return await handler(event, data)

# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
def convert_image(src_bytes: bytes, target_ext: str):
    if not HAS_PIL:
        return None, None
    fmt, mime = IMG_FMT[target_ext]
    try:
        img = Image.open(io.BytesIO(src_bytes))
        if fmt == "JPEG" and img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        out = io.BytesIO()
        kw = {}
        if fmt in ("JPEG", "WEBP"):
            kw["quality"] = 92
        img.save(out, format=fmt, **kw)
        return out.getvalue(), mime
    except Exception as e:
        print("[wormai] convert failed:", e)
        return None, None

def detect_convert_target(message_text: str):
    if not message_text:
        return None
    low = message_text.lower()
    triggers = ("конверт", "перевед", "преобразу", "convert", "пересохран", "сохрани как")
    if not any(t in low for t in triggers):
        return None
    for ext in IMG_FMT.keys():
        if re.search(r"\b(?:в|in|to|as)\s+\.?" + re.escape(ext) + r"\b", low):
            return ext
        if re.search(r"\.\s*" + re.escape(ext) + r"\b", low):
            return ext
    return None

def clean_ai_response(text: str) -> str:
    return re.sub(r"<thinking>[\s\S]*?</thinking>", "", text).strip()

async def send_split_message(msg: Message, text: str):
    clean_text = clean_ai_response(text)
    if not clean_text:
        clean_text = "[W] Пустой ответ."
        
    chunk_size = 4000
    for i in range(0, len(clean_text), chunk_size):
        chunk = clean_text[i:i + chunk_size]
        await msg.answer(chunk)

# ================= ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ =================
@router.message(CommandStart())
async def cmd_start(message: Message):
    chat_history[message.chat.id] = []
    user_id = message.from_user.id
    
    if not check_user_access(user_id):
        await message.answer(
            "👋 Привет!\n\n"
            "🔒 **Для доступа к боту нужен код.**\n"
            "Нажмите или напишите /code и введите ваш промокод для активации."
        )
    else:
        await message.answer(
            "[W] Приветствую! Напишите запрос или отправьте фото.\n\n"
            "📍 /clear — Очистить контекст диалога\n"
            "📍 /code — Ввести код продления"
        )

@router.message(Command("clear"))
async def cmd_clear(message: Message):
    chat_history[message.chat.id] = []
    await message.answer("[W] Контекст диалога очищен.")

# --- АКТИВАЦИЯ КОДА ---
@router.message(Command("code"))
async def cmd_code(message: Message, state: FSMContext):
    await state.set_state(PromoStates.waiting_for_code)
    await message.answer("🔑 Введите ваш код:")

@router.message(PromoStates.waiting_for_code)
async def process_promo_code(message: Message, state: FSMContext):
    input_code = message.text.strip().upper()
    user_id = message.from_user.id

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Проверка из базы созданных админом промокодов
    cursor.execute("SELECT days, uses_left FROM promo_codes WHERE code = ?", (input_code,))
    promo = cursor.fetchone()

    cursor.execute("SELECT 1 FROM used_promos WHERE user_id = ? AND code = ?", (user_id, input_code))
    already_used = cursor.fetchone()

    if not promo:
        await message.answer("❌ Неверный или несуществующий код.")
    elif promo[1] <= 0:
        await message.answer("❌ Этот код больше недоступен (закончились использования).")
    elif already_used:
        await message.answer("❌ Вы уже активировали этот код.")
    else:
        days, uses = promo
        cursor.execute("UPDATE promo_codes SET uses_left = uses_left - 1 WHERE code = ?", (input_code,))
        cursor.execute("INSERT INTO used_promos (user_id, code) VALUES (?, ?)", (user_id, input_code))
        conn.commit()
        
        add_access_days(user_id, days)
        await message.answer(f"✅ Код успешно активирован! Доступ предоставлен на {days} дн.")

    conn.close()
    await state.clear()

# ================= АДМИН-ПАНЕЛЬ =================
def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="➕ Создать код", callback_data="admin_add_promo")],
        [InlineKeyboardButton(text="🚫 Забанить ID", callback_data="admin_ban"),
         InlineKeyboardButton(text="🟢 Разбанить ID", callback_data="admin_unban")],
        [InlineKeyboardButton(text="📜 Список банов", callback_data="admin_ban_list")]
    ])

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("⚙️ **Админ-панель**", reply_markup=get_admin_keyboard())

@router.callback_query(F.data.startswith("admin_"))
async def handle_admin_callbacks(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("У вас нет доступа.", show_alert=True)
        return

    action = callback.data
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    if action == "admin_stats":
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM banned_users")
        total_banned = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM promo_codes WHERE uses_left > 0")
        total_promos = cursor.fetchone()[0]

        stats_text = (
            f"📊 **Статистика бота:**\n\n"
            f"👥 Всего пользователей: {total_users}\n"
            f"🚫 Заблокировано: {total_banned}\n"
            f"🎟 Активных кодов в базе: {total_promos}"
        )
        await callback.message.edit_text(stats_text, reply_markup=get_admin_keyboard())

    elif action == "admin_add_promo":
        await state.set_state(AdminStates.waiting_for_promo)
        await callback.message.answer(
            "🎟 Введите новый код, кол-во дней и кол-во активаций через пробел.\n\n"
            "Примеры:\n"
            "• `worn1 1 100` (код worn1 на 1 день для 100 человек)\n"
            "• `worn15 15 50` (код worn15 на 15 дней для 50 человек)\n"
            "• `worn30 30 1` (код worn30 на 30 дней для 1 человека)"
        )
        await callback.answer()

    elif action == "admin_ban":
        await state.set_state(AdminStates.waiting_for_ban_id)
        await callback.message.answer("🚫 Введите Telegram ID для бана:")
        await callback.answer()

    elif action == "admin_unban":
        await state.set_state(AdminStates.waiting_for_unban_id)
        await callback.message.answer("🟢 Введите Telegram ID для разбана:")
        await callback.answer()

    elif action == "admin_ban_list":
        cursor.execute("SELECT user_id FROM banned_users")
        banned = cursor.fetchall()
        if not banned:
            text = "📜 Список забаненных пуст."
        else:
            text = "📜 **Забаненные ID:**\n" + "\n".join(str(b[0]) for b in banned)
        await callback.message.edit_text(text, reply_markup=get_admin_keyboard())

    conn.close()

@router.message(AdminStates.waiting_for_promo)
async def process_add_promo(message: Message, state: FSMContext):
    try:
        parts = message.text.strip().split()
        code = parts[0].upper()
        days = int(parts[1])
        uses = int(parts[2])
        
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO promo_codes (code, days, uses_left) VALUES (?, ?, ?)", (code, days, uses))
        conn.commit()
        conn.close()
        
        await message.answer(f"✅ Код `{code}` на {days} дней ({uses} активаций) успешно создан!")
    except Exception:
        await message.answer("❌ Ошибка формата. Попробуйте еще раз: `КОД ДНИ АКТИВАЦИИ` (например: `WORN1 1 100`)")
    await state.clear()

@router.message(AdminStates.waiting_for_ban_id)
async def process_ban_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        conn.close()
        await message.answer(f"🚫 Пользователь `{user_id}` забанен.")
    except ValueError:
        await message.answer("❌ Неверный ID.")
    await state.clear()

@router.message(AdminStates.waiting_for_unban_id)
async def process_unban_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        await message.answer(f"🟢 Пользователь `{user_id}` разбанен.")
    except ValueError:
        await message.answer("❌ Неверный ID.")
    await state.clear()

# ================= ОБРАБОТКА AI =================
@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    chat_id = message.chat.id
    if chat_id not in chat_history:
        chat_history[chat_id] = []

    caption = message.caption or ""
    target_ext = detect_convert_target(caption)

    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    downloaded_file = await bot.download_file(file_info.file_path)
    img_bytes = downloaded_file.read()

    if target_ext:
        converted_bytes, mime = convert_image(img_bytes, target_ext)
        if converted_bytes:
            out_file = BufferedInputFile(converted_bytes, filename=f"converted.{target_ext}")
            await message.answer_document(out_file, caption=f"[W] Конвертировано в .{target_ext}")
            return
        else:
            await message.answer("[W] Ошибка при конвертации.")
            return

    b64_img = base64.b64encode(img_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64_img}"

    messages = [{"role": "system", "content": SYS_PROMPT}]
    for h in chat_history[chat_id]:
        messages.append(h)

    messages.append({
        "role": "user", 
        "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": caption if caption else "Проанализируй это изображение."}
        ]
    })

    status_msg = await message.answer("Thinking...")
    try:
        response = ai.chat.completions.create(
            model="command-a-03-2025",
            messages=messages
        )
        ans_text = response.choices[0].message.content
        chat_history[chat_id].append({"role": "user", "content": caption or "[Фото]"})
        chat_history[chat_id].append({"role": "assistant", "content": ans_text})

        await status_msg.delete()
        await send_split_message(message, ans_text)
    except Exception as e:
        await status_msg.edit_text(f"[W] Ошибка: {e}")

@router.message(F.text)
async def handle_text(message: Message):
    chat_id = message.chat.id
    if chat_id not in chat_history:
        chat_history[chat_id] = []

    text = message.text
    messages = [{"role": "system", "content": SYS_PROMPT}]
    for h in chat_history[chat_id]:
        messages.append(h)

    messages.append({"role": "user", "content": text})

    status_msg = await message.answer("Thinking...")
    try:
        response = ai.chat.completions.create(
            model="command-a-03-2025",
            messages=messages
        )
        ans_text = response.choices[0].message.content

        chat_history[chat_id].append({"role": "user", "content": text})
        chat_history[chat_id].append({"role": "assistant", "content": ans_text})

        await status_msg.delete()
        await send_split_message(message, ans_text)
    except Exception as e:
        await status_msg.edit_text(f"[W] Ошибка: {e}")

# ================= ЗАПУСК =================
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    print("[wormai] Бот готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())