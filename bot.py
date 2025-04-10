# telegram_bot.py
# -*- coding: utf-8 -*-

import os
import sqlite3
import json
import logging
import traceback
import asyncio
from datetime import datetime
from urllib.parse import quote

# Используем aiohttp для внутреннего сервера
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, WebAppInfo
from telegram.constants import ParseMode, ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ApplicationBuilder

# --- Настройки ---
# ВАЖНО: Укажи свой настоящий токен!
TOKEN = "7910000913:AAG4Mz_wo8Le9ZvItZZMkqtoMUA0VjmEowg"
# URL твоего WebApp на GitHub Pages или другом хостинге
WEB_APP_URL = "https://lenayok.github.io/monkey/"
DB_NAME = "user_files.db"
DOWNLOAD_FOLDER = "downloads"
INTERNAL_API_HOST = '127.0.0.1' # Внутренний API слушает только локально
INTERNAL_API_PORT = 8081        # Порт для внутреннего API бота

# --- Настройка логирования ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()] # Вывод в консоль
)
# Уменьшаем спам от библиотек http
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.ExtBot").setLevel(logging.INFO)
logging.getLogger("telegram.ext.Updater").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# --- Функции для работы с БД ---
def init_db():
    """Инициализирует базу данных и создает папку для загрузок."""
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        # Добавил UNIQUE constraint и COLLATE NOCASE
        c.execute('''CREATE TABLE IF NOT EXISTS files
                     (user_id INTEGER NOT NULL,
                      file_name TEXT NOT NULL COLLATE NOCASE,
                      upload_date TEXT NOT NULL,
                      PRIMARY KEY (user_id, file_name))''')
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully.")
    except sqlite3.Error as e:
        logger.error(f"Database initialization error: {e}", exc_info=True)
        raise # Прерываем запуск, если БД не инициализирована

def add_file_db(user_id: int, file_name: str, upload_date: str) -> bool:
    """Добавляет или обновляет запись о файле в БД."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO files (user_id, file_name, upload_date) VALUES (?, ?, ?)",
                  (user_id, file_name, upload_date))
        conn.commit()
        conn.close()
        logger.info(f"File record '{file_name}' added/updated for user {user_id}.")
        return True
    except sqlite3.Error as e:
        logger.error(f"Error adding/updating file record '{file_name}' for user {user_id}: {e}", exc_info=True)
        return False

def get_user_files_db(user_id: int) -> list:
    """Получает список файлов пользователя из БД, сортированный по дате."""
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT file_name, upload_date FROM files WHERE user_id = ? ORDER BY upload_date DESC", (user_id,))
        files = c.fetchall()
        conn.close()
        return [(row['file_name'], row['upload_date']) for row in files]
    except sqlite3.Error as e:
        logger.error(f"Error getting files for user {user_id}: {e}", exc_info=True)
        return []

def delete_file_db_and_disk(user_id: int, file_name: str) -> bool:
    """Удаляет запись о файле из БД и сам файл с диска."""
    deleted_db = False
    deleted_disk = False
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM files WHERE user_id = ? AND file_name = ?", (user_id, file_name))
        conn.commit()
        deleted_db = conn.total_changes > 0
        conn.close()
        if deleted_db: logger.info(f"DB record deleted for {user_id}, {file_name}")
        else: logger.warning(f"DB record not found for deletion: {user_id}, {file_name}"); deleted_db = True
    except sqlite3.Error as e:
        logger.error(f"DB Error deleting {user_id}, {file_name}: {e}", exc_info=True)

    file_path = os.path.join(DOWNLOAD_FOLDER, file_name)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            deleted_disk = True
            logger.info(f"Disk file deleted: {file_path}")
        except OSError as e:
            logger.error(f"Disk Error deleting {file_path}: {e}", exc_info=True)
    else:
        logger.warning(f"Disk file not found for deletion: {file_path}")
        deleted_disk = True

    return deleted_db and deleted_disk

# --- Функции парсинга и конвертации (ИЗ ТВОЕГО ИСХОДНОГО КОДА) ---
def parse_account(line: str, shop: str) -> dict | None:
    """Парсит строку аккаунта в зависимости от магазина."""
    line = line.strip()
    if not line: return None
    separator = ':'
    if shop == "Meta Store" and "|" in line: separator = "|"
    elif ":" not in line and "|" in line: separator = "|"
    parts = line.split(separator)
    account = {"_original_line": line}; num_parts = len(parts)
    try:
        if shop == "Chivap Shop":
            if num_parts < 4: raise IndexError(f"Chivap: Exp 4+, got {num_parts}")
            account["user"]=parts[0].replace("@",""); account["pass"]=parts[1]; account["mail"]=parts[2]; account["mailpass"]=parts[3]
            if num_parts >= 5: account["token"]=parts[4];
            if num_parts >= 6: account["token2"]=parts[5]
        elif shop == "Grom Shop":
            if num_parts < 4: raise IndexError(f"Grom: Exp 4+, got {num_parts}")
            account["mail"]=parts[0]; account["mailpass"]=parts[1]; account["user"]=parts[2]; account["pass"]=parts[3]
            if num_parts >= 5: account["token"]=parts[4];
            if num_parts >= 6: account["token2"]=parts[5]
        elif shop == "Proliv Shop":
            if num_parts < 4: raise IndexError(f"Proliv: Exp 4+, got {num_parts}")
            account["mail"]=parts[0]; account["mailpass"]=parts[1]; account["user"]=parts[2]; account["pass"]=parts[3]
            if num_parts >= 5: account["geo"]=parts[4]
        elif shop == "Muted Shop":
            if num_parts < 4: raise IndexError(f"Muted: Exp 4+, got {num_parts}")
            account["user"]=parts[0].replace("@",""); account["pass"]=parts[1]; account["mail"]=parts[2]; account["mailpass"]=parts[3]
        elif shop == "Boom Store":
            if num_parts < 4: raise IndexError(f"Boom: Exp 4+, got {num_parts}")
            account["user"]=parts[0]; account["pass"]=parts[1]; account["mail"]=parts[2]; account["mailpass"]=parts[3]
            if num_parts >= 5: account["token"]=parts[4];
            if num_parts >= 6: account["token2"]=parts[5]
        elif shop == "Meta Store":
            if separator!="|": raise ValueError("Meta needs '|'");
            if num_parts < 3: raise IndexError(f"Meta: Exp 3+, got {num_parts}")
            mail_mailpass=parts[0].split(":");
            if len(mail_mailpass)!=2: raise ValueError(f"Meta: Invalid mail:pass {parts[0]}");
            account["mail"]=mail_mailpass[0]; account["mailpass"]=mail_mailpass[1]
            log_pass=parts[1].split(":");
            if len(log_pass)!=2: raise ValueError(f"Meta: Invalid log:pass {parts[1]}");
            account["pass"]=log_pass[1] # Assuming log:pass
            geo_user_parts=parts[2].split(" "); account["geo"]=geo_user_parts[0]; account["user"]=geo_user_parts[-1]
        elif shop == "Inferno Shop":
            if num_parts < 4: raise IndexError(f"Inferno: Exp 4+, got {num_parts}")
            account["user"]=parts[0].replace("@",""); account["pass"]=parts[1]; account["mail"]=parts[2]; account["mailpass"]=parts[3]
            if num_parts >= 5: account["token"]=parts[4];
            if num_parts >= 6: account["token2"]=parts[5]
        elif shop == "Monkey Shop":
            if num_parts < 4: raise IndexError(f"Monkey: Exp 4+, got {num_parts}")
            account["mail"]=parts[0]; account["mailpass"]=parts[1]; account["user"]=parts[2]; account["pass"]=parts[3]
            if num_parts >= 5: account["token"]=parts[4];
            if num_parts >= 6: account["token2"]=parts[5]
        elif shop == "Другой":
            if num_parts >= 1: account["field1"]=parts[0];
            if num_parts >= 2: account["field2"]=parts[1];
            if num_parts >= 3: account["field3"]=parts[2];
            if num_parts >= 4: account["field4"]=parts[3];
            if separator==':': # Guess log:pass:mail:mailpass
                if num_parts >= 1: account["user"]=parts[0];
                if num_parts >= 2: account["pass"]=parts[1];
                if num_parts >= 3: account["mail"]=parts[2];
                if num_parts >= 4: account["mailpass"]=parts[3];
            logger.debug(f"Parsed 'Другой': {account}")
        else: # Unknown shop
            logger.warning(f"Unknown shop '{shop}' for line '{line}' - trying default parse")
            if separator == ':' and num_parts >= 2:
                account["user"]=parts[0]; account["pass"]=parts[1]
                if num_parts >= 3: account["mail"]=parts[2]
                if num_parts >= 4: account["mailpass"]=parts[3]
            else: return None
        return {k: v for k, v in account.items() if v or k == '_original_line'}
    except(IndexError, ValueError)as e:
        logger.warning(f"Parsing error shop '{shop}' sep '{separator}' line: '{line}': {e}")
        return None

def convert_accounts(accounts: list[dict], format_type: str) -> tuple[list[str], int]:
    """Конвертирует список словарей аккаунтов в строки нужного формата."""
    converted_lines = []; conversion_errors = 0
    for account in accounts:
        if not isinstance(account, dict): conversion_errors += 1; continue
        original_line = account.get('_original_line', '')
        try:
            if format_type == "@username":
                user = account.get("user");
                if user: converted_lines.append(user if user.startswith('@') else f"@{user}")
                else: logger.warning(f"Missing 'user' for @username: {account}"); conversion_errors += 1
            elif format_type == "mail:mailpass":
                mail, mailpass = account.get('mail'), account.get('mailpass')
                if mail and mailpass:
                    line = f"{mail}:{mailpass}"; token, token2 = account.get("token"), account.get("token2")
                    if token: line += f":{token}"
                    if token and token2: line += f":{token2}" # Second only if first exists
                    converted_lines.append(line)
                else: logger.warning(f"Missing fields for mail:mailpass: {account}"); conversion_errors += 1
            elif format_type == "log:pass":
                user = account.get('user') or account.get('field1'); password = account.get('pass') or account.get('field2')
                if user and password: converted_lines.append(f"{user}:{password}")
                else: logger.warning(f"Missing fields for log:pass: {account}"); conversion_errors += 1
            elif format_type == "replace_colon_to_pipe":
                if original_line: converted_lines.append(original_line.replace(":", "|"))
                else: logger.warning(f"Missing original line for replace_colon_to_pipe: {account}"); conversion_errors += 1
            elif format_type == "replace_pipe_to_colon":
                if original_line: converted_lines.append(original_line.replace("|", ":"))
                else: logger.warning(f"Missing original line for replace_pipe_to_colon: {account}"); conversion_errors += 1
            else:
                logger.error(f"Unknown format_type for conversion: '{format_type}'")
                conversion_errors += 1
        except Exception as e:
            logger.error(f"Error converting account {account} to format '{format_type}': {e}", exc_info=True)
            conversion_errors += 1
    return converted_lines, conversion_errors


# --- Логика конвертации (ИСПРАВЛЕННАЯ ФУНКЦИЯ) ---
async def perform_conversion_task(context: ContextTypes.DEFAULT_TYPE, user_id: int, file_name: str, shop: str, format_type: str):
    """Выполняет полную логику конвертации и отправки файла."""
    chat_id = user_id
    task_id = f"Task_{user_id}_{file_name[:10]}" # ID для логов
    logger.info(f"{task_id}: Starting conversion: file='{file_name}', shop='{shop}', format='{format_type}'")
    converted_file_path = None # Определяем переменную заранее для блока finally

    try:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        file_path = os.path.join(DOWNLOAD_FOLDER, file_name)
        logger.info(f"{task_id}: Checking source file '{file_path}'")

        if not os.path.exists(file_path):
            logger.error(f"{task_id}: Source file not found: '{file_path}'")
            await context.bot.send_message(chat_id, f"❌ Ошибка: Исходный файл `{file_name}` не найден.", parse_mode=ParseMode.MARKDOWN)
            return

        lines, accounts, parsing_errors, file_read_success = [], [], 0, False
        encodings_to_try = ['utf-8-sig', 'utf-8', 'cp1251']

        for encoding in encodings_to_try:
            try:
                logger.info(f"{task_id}: Reading file '{file_path}' with encoding '{encoding}'.")
                with open(file_path, "r", encoding=encoding, errors='ignore') as f:
                    lines = f.readlines()
                file_read_success = True
                logger.info(f"{task_id}: Read {len(lines)} lines ('{encoding}')")
                break
            except UnicodeDecodeError: logger.warning(f"{task_id}: Failed decode ('{encoding}')")
            except Exception as read_err: logger.error(f"{task_id}: Read error ('{encoding}'): {read_err}", exc_info=True)

        if not file_read_success:
            logger.error(f"{task_id}: Failed read '{file_path}'")
            await context.bot.send_message(chat_id, f"❌ Ошибка чтения `{file_name}`.", parse_mode=ParseMode.MARKDOWN); return

        logger.info(f"{task_id}: Parsing {len(lines)} lines for shop '{shop}'...")
        for line_num, line in enumerate(lines):
            account = parse_account(line, shop)
            if account: accounts.append(account)
            elif line.strip(): parsing_errors += 1; logger.debug(f"{task_id}: Parse fail line {line_num+1}")

        parsed_count = len(accounts)
        logger.info(f"{task_id}: Parsed: {parsed_count}. Failed: {parsing_errors}.")
        if not accounts:
             msg = f"❌ Не найдено аккаунтов в `{file_name}` для `{shop}`." + (f" ({parsing_errors} ошибок)" if parsing_errors else "")
             await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN); return

        logger.info(f"{task_id}: Converting {parsed_count} accounts to format '{format_type}'...")
        converted_lines, conversion_errors = convert_accounts(accounts, format_type)
        converted_count = len(converted_lines)
        logger.info(f"{task_id}: Converted: {converted_count}. Errors: {conversion_errors}.")

        # Проверяем, есть ли *хоть какие-то* строки в результате (даже если пустые)
        if converted_count == 0:
             msg = f"❌ Ошибка конвертации `{file_name}` в `{format_type}`." + (f" ({conversion_errors} ошибок)" if conversion_errors else "")
             await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN); return

        # Собираем имя файла и путь
        base_name, _ = os.path.splitext(file_name)
        safe_format_name = "".join(c if c.isalnum() else '_' for c in format_type)
        converted_file_name = f"{base_name}_{shop}_{safe_format_name}.txt".replace(" ", "_")
        converted_file_path = os.path.join(DOWNLOAD_FOLDER, converted_file_name) # Присваиваем путь переменной

        logger.info(f"{task_id}: Saving converted file to '{converted_file_path}'...")
        valid_lines_written = 0
        try:
            # Отфильтровываем пустые строки *перед* записью
            valid_lines = [line for line in converted_lines if line and line.strip()]
            valid_lines_written = len(valid_lines)

            if valid_lines_written == 0:
                logger.warning(f"{task_id}: All {converted_count} converted lines resulted in empty strings for format '{format_type}'.")
                await context.bot.send_message(chat_id, f"⚠️ Конвертация `{file_name}` в `{format_type}` дала только пустые строки.", parse_mode=ParseMode.MARKDOWN)
                # Не создаем пустой файл, просто выходим
                return # <-- Важный выход здесь

            # Пишем только валидные строки
            with open(converted_file_path, "w", encoding="utf-8") as f:
                f.writelines(line + "\n" for line in valid_lines)
                f.flush(); os.fsync(f.fileno()) # Force write to disk
            logger.info(f"{task_id}: Saved and flushed {valid_lines_written} lines.")

        except Exception as write_err:
             logger.error(f"{task_id}: Error saving/flushing: {write_err}", exc_info=True);
             await context.bot.send_message(chat_id, "❌ Ошибка сохранения результата."); return

        # Отправляем файл, только если были записаны строки
        logger.info(f"{task_id}: Sending document '{converted_file_name}' ({valid_lines_written} lines)")
        caption = f"✅ *Конвертация завершена!*\n\nИсх: `{file_name}`\nМаг: `{shop}`\nФормат: `{format_type}`\n\nУспешно строк: {valid_lines_written}"
        total_errors = parsing_errors + conversion_errors;
        if total_errors > 0: caption += f"\nПропущено (ошибки): {total_errors}"

        try:
            with open(converted_file_path, "rb") as f_doc:
                await context.bot.send_document(chat_id=chat_id, document=f_doc, filename=converted_file_name, caption=caption, parse_mode=ParseMode.MARKDOWN)
            logger.info(f"{task_id}: Document sent successfully.")
        except Exception as send_err:
            logger.error(f"{task_id}: Error sending document: {send_err}", exc_info=True)
            await context.bot.send_message(chat_id, f"❌ Ошибка отправки файла `{converted_file_name}`.")
        # finally блок теперь ниже, вне try отправки

    except Exception as e:
        # Ловим любые другие ошибки в задаче
        logger.error(f"CRITICAL ERROR in conversion task {task_id}: {e}", exc_info=True)
        try: await context.bot.send_message(chat_id, "❌ Критическая ошибка во время конвертации.")
        except Exception: pass
    finally:
        # Удаляем временный файл, если он был создан
        if converted_file_path and os.path.exists(converted_file_path):
            try:
                os.remove(converted_file_path)
                logger.info(f"{task_id}: Removed temp file: '{converted_file_path}'")
            except OSError as del_err:
                logger.error(f"{task_id}: Error removing temp file '{converted_file_path}': {del_err}")


# --- Обработчики Telegram ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start."""
    user = update.effective_user; logger.info(f"User {user.id} started."); context.bot_data.setdefault(user.id, {})['name'] = user.first_name or user.username or ''
    main_keyboard = [["Мои пачки"]]; reply_markup_main = ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
    user_params = f"&userId={user.id}&userName={quote(user.first_name or user.username or '')}"
    web_app_button = InlineKeyboardButton("🚀 Открыть меню Monkey", web_app=WebAppInfo(url=f"{WEB_APP_URL}?tab=main-page{user_params}"))
    inline_keyboard = InlineKeyboardMarkup([[web_app_button]])
    await update.message.reply_text(f"Привет, {user.first_name}! 👋\nИспользуй кнопки или меню.", reply_markup=reply_markup_main)
    await update.message.reply_text("Меню инструментов:", reply_markup=inline_keyboard)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовые сообщения, в т.ч. кнопку 'Мои пачки'."""
    user = update.effective_user; text = update.message.text; logger.info(f"User {user.id} text: '{text}'"); context.bot_data.setdefault(user.id, {})['name'] = user.first_name or user.username or ''
    if text == "Мои пачки": await show_user_files(update, context, user.id)
    else: await update.message.reply_text("Не понял команду. Используйте /start или кнопки.", reply_markup=update.message.reply_markup)

async def show_user_files(update_or_query: Update | CallbackQueryHandler, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Отображает список файлов пользователя с кнопками управления."""
    is_update = isinstance(update_or_query, Update); query = None if is_update else update_or_query.callback_query
    sender = update_or_query.message.reply_text if is_update else query.edit_message_text
    chat_id = update_or_query.effective_chat.id; files = get_user_files_db(user_id); keyboard = []
    text = "🗂️ *Ваши файлы:*\n(Нажмите для действий)" if files else "У вас пока нет загруженных файлов."
    for fname, udate_str in files:
        try: udate = datetime.strptime(udate_str, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%y %H:%M")
        except: udate = udate_str
        btn_text = f"📄 {fname[:30]}{'...' if len(fname)>30 else ''} ({udate})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"file_{fname}")])
    keyboard.append([InlineKeyboardButton("➕ Загрузить новый", callback_data="upload_pack")])
    uname = quote(context.bot_data.get(user_id, {}).get('name', ''))
    keyboard.append([InlineKeyboardButton("🚀 Открыть меню Monkey", web_app=WebAppInfo(url=f"{WEB_APP_URL}?tab=main-page&userId={user_id}&userName={uname}"))])
    reply_markup = InlineKeyboardMarkup(keyboard)
    try: await sender(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error send/edit file list for {user_id}: {e}", exc_info=True)
        if query:
            try: await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN); await query.message.delete()
            except Exception as send_err: logger.error(f"Failed send new file list after edit fail: {send_err}", exc_info=True)

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на инлайн-кнопки."""
    query = update.callback_query; user = query.from_user; data = query.data; chat_id = query.message.chat_id
    logger.info(f"User {user.id} inline button: '{data}'"); context.bot_data.setdefault(user.id, {})['name'] = user.first_name or user.username or ''; await query.answer()
    uname = quote(context.bot_data.get(user.id, {}).get('name', ''))
    if data.startswith("file_"):
        file_name = data[len("file_"):]; encoded_fname = quote(file_name)
        web_app_url = f"{WEB_APP_URL}?tab=converter&file={encoded_fname}&userId={user.id}&userName={uname}"
        keyboard = [[InlineKeyboardButton("🔄 Конвертировать (WebApp)", web_app=WebAppInfo(url=web_app_url))], [InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{file_name}")], [InlineKeyboardButton("🔙 Назад", callback_data="back_to_list")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try: await query.edit_message_text(f"Выбран: 📄 `{file_name}`\nДействия:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Edit fail file options {user.id}: {e}"); await context.bot.send_message(chat_id,f"Выбран: `{file_name}`", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("delete_"):
        file_name = data[len("delete_"):]; await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        if delete_file_db_and_disk(user.id, file_name): logger.info(f"File '{file_name}' deleted for {user.id}."); await show_user_files(query, context, user.id)
        else: logger.error(f"Failed delete file '{file_name}' for {user.id}."); await query.edit_message_text(f"❌ Ошибка удаления `{file_name}`.", reply_markup=query.message.reply_markup, parse_mode=ParseMode.MARKDOWN)
    elif data == "upload_pack":
        try: await query.edit_message_text("Отправьте `.txt` файл в этот чат.")
        except Exception as e: logger.warning(f"Edit fail upload prompt {user.id}: {e}"); await context.bot.send_message(chat_id,"Отправьте `.txt` файл."); await query.message.delete()
    elif data == "back_to_list": await show_user_files(query, context, user.id)

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает получение файла от пользователя."""
    user = update.effective_user; message = update.message; chat_id = message.chat_id; context.bot_data.setdefault(user.id, {})['name'] = user.first_name or user.username or ''
    if message.document and message.document.file_name:
        file = message.document; file_name = file.file_name
        if file_name.lower().endswith(".txt"):
            file_id = file.file_id; logger.info(f"Received TXT '{file_name}' from {user.id}"); await message.reply_chat_action(ChatAction.UPLOAD_DOCUMENT)
            try:
                dl_file = await context.bot.get_file(file_id); safe_file_name = os.path.basename(file_name); file_path = os.path.join(DOWNLOAD_FOLDER, safe_file_name)
                await dl_file.download_to_drive(file_path); logger.info(f"Downloaded to {file_path}")
                upload_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if add_file_db(user.id, safe_file_name, upload_date):
                    uname = quote(context.bot_data.get(user.id, {}).get('name', '')); encoded_fname = quote(safe_file_name)
                    web_app_url = f"{WEB_APP_URL}?tab=converter&file={encoded_fname}&userId={user.id}&userName={uname}"
                    keyboard = [[InlineKeyboardButton("🔄 Конвертировать (WebApp)", web_app=WebAppInfo(url=web_app_url))], [InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{safe_file_name}")], [InlineKeyboardButton("🗂 Показать все", callback_data="back_to_list")]]
                    await message.reply_text(f"✅ Файл `{safe_file_name}` загружен!", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
                else: logger.error(f"Failed add DB record for {user.id}, {safe_file_name}"); await message.reply_text("❌ Ошибка сохранения файла в БД.")
            except Exception as e: logger.error(f"Error handling file {user.id}: {e}", exc_info=True); await message.reply_text(f"❌ Ошибка обработки файла `{file_name}`.")
        else: await message.reply_text(f"⚠️ Нужен файл `.txt`, а не `{file_name}`.", parse_mode=ParseMode.MARKDOWN)
    elif message.document: await message.reply_text("⚠️ Получен документ без имени. Нужен `.txt` файл.")

# --- Обработчик данных из Web App (только для 'upload_pack') ---
async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает данные, полученные из Web App (только 'upload_pack')."""
    user = update.effective_user;
    if not user or not update.message or not update.message.web_app_data: return
    data_str = update.message.web_app_data.data; logger.info(f"Received web_app_data from {user.id}: '{data_str[:100]}...'"); context.bot_data.setdefault(user.id, {})['name'] = user.first_name or user.username or ''
    if data_str == "upload_pack": logger.info(f"User {user.id} requested file upload via WebApp."); await context.bot.send_message(user.id, "Отправьте мне `.txt` файл в этот чат.")
    else: logger.warning(f"Ignored unexpected web_app_data from {user.id}: '{data_str[:100]}...'")

# --- Внутренний HTTP сервер бота ---
async def handle_trigger_conversion(request: web.Request):
    """Обрабатывает запрос от Flask-сервера на запуск конвертации."""
    try:
        ptb_app = request.app['ptb_application']
        if not ptb_app: raise RuntimeError("PTB application not found in aiohttp context!")
        data = await request.json(); logger.info(f"Internal API received: {data}")
        user_id, file_name, shop, format_type = data.get('user_id'), data.get('file_name'), data.get('shop'), data.get('format_type')
        if not all([isinstance(user_id, int), isinstance(file_name, str), isinstance(shop, str), isinstance(format_type, str)]): return web.json_response({"status": "error", "message": "Invalid data types"}, status=400)
        if not all([user_id, file_name, shop, format_type]): return web.json_response({"status": "error", "message": "Missing fields"}, status=400)
        context_data = {"user_id": user_id, "file_name": file_name, "shop": shop, "format_type": format_type}
        job = ptb_app.job_queue.run_once(lambda ctx: perform_conversion_task(ctx, **ctx.job.data), when=0, data=context_data, name=f"conversion_{user_id}_{file_name[:10]}")
        if job: logger.info(f"Conversion task scheduled (job ID: {job.name}) for {user_id}, file '{file_name}'."); return web.json_response({"status": "ok", "message": "Task scheduled"})
        else: logger.error(f"Failed schedule task for {user_id} via JobQueue!"); return web.json_response({"status": "error", "message": "Failed schedule task"}, status=500)
    except json.JSONDecodeError: logger.error("Internal API: Invalid JSON"); return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)
    except Exception as e: logger.error(f"Internal API error: {e}", exc_info=True); return web.json_response({"status": "error", "message": f"Internal bot error: {e}"}, status=500)

async def run_internal_server(ptb_app: Application):
    """Запускает aiohttp сервер для внутреннего API."""
    app = web.Application(); app['ptb_application'] = ptb_app
    app.router.add_post('/trigger_conversion', handle_trigger_conversion)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, INTERNAL_API_HOST, INTERNAL_API_PORT)
    logger.info(f"Starting internal bot API server on http://{INTERNAL_API_HOST}:{INTERNAL_API_PORT}")
    try:
        await site.start()
        # Ждем вечно (пока не будет отменено извне, например, из main)
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Internal server task cancelling...")
    finally:
        logger.info("Shutting down internal API server runner...")
        await runner.cleanup()
        logger.info("Internal API server runner stopped.")

# --- Основная функция запуска бота (ИСПРАВЛЕННАЯ) ---
async def main():
    """Инициализирует и запускает бота и внутренний API сервер."""
    logger.info("Initializing database...")
    init_db() # Вызываем перед созданием Application

    logger.info("Building PTB application...")
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    # --- Регистрация обработчиков PTB ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    application.add_handler(CallbackQueryHandler(handle_button))

    internal_server_task = None
    try:
        # async with для управления initialize/shutdown PTB
        async with application:
            logger.info("Starting internal API server task...")
            # Запускаем внутренний сервер как фоновую задачу asyncio
            internal_server_task = asyncio.create_task(run_internal_server(application), name="InternalAPIServer")

            logger.info("Starting PTB polling (non-blocking)...")
            # Запускаем поллинг асинхронно
            await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            # Запускаем обработку входящих обновлений
            await application.start()

            logger.info("Bot and internal API server are running. Press Ctrl+C to stop.")
            # Ждем завершения работы (например, по Ctrl+C)
            # Используем Event().wait() для ожидания сигнала остановки
            stop_event = asyncio.Event()
            await stop_event.wait() # Будет ждать вечно, пока не прервут

    except (KeyboardInterrupt, SystemExit):
        logger.info("Received stop signal (KeyboardInterrupt/SystemExit).")
    except Exception as e:
        logger.critical(f"CRITICAL ERROR during bot runtime: {e}", exc_info=True)
    finally:
        logger.info("Shutting down...")
        # При выходе из 'async with application' PTB остановится сам.
        # Нам нужно остановить нашу фоновую задачу.
        if internal_server_task and not internal_server_task.done():
            logger.info("Cancelling internal server task...")
            internal_server_task.cancel()
            try:
                # Даем время серверу завершиться
                await asyncio.wait_for(internal_server_task, timeout=5.0)
            except asyncio.CancelledError:
                logger.info("Internal server task successfully cancelled.")
            except asyncio.TimeoutError:
                logger.warning("Internal server task did not cancel within timeout.")
            except Exception as task_exc:
                logger.error("Error during internal server task cancellation:", exc_info=task_exc)
        logger.info("Shutdown process finished.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"FATAL ERROR in asyncio run: {e}", flush=True)
        logging.critical(f"FATAL ERROR in asyncio run:", exc_info=True)
    finally:
        print("Script finished.", flush=True)
        logging.info("Script finished.")
