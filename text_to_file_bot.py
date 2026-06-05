#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import zipfile
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = "8683503979:AAFgyrJbkBeiUKZ0beHbKSWgLvuRFPEU6lM"

KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("📄 Текст", callback_data='mode_text'),
    InlineKeyboardButton("📁 TXT", callback_data='mode_txt'),
]])


# --- Декодируем имя файла из zip (Windows cp437/cp866) ---
def decode_zip_filename(name: str) -> str:
    for enc_from, enc_to in [('cp437', 'cp866'), ('cp437', 'cp1251')]:
        try:
            decoded = name.encode(enc_from).decode(enc_to)
            if decoded.isprintable():
                return decoded
        except Exception:
            pass
    return name


# --- Читаем байты с автоопределением кодировки ---
def decode_content(raw: bytes) -> str:
    for enc in ('utf-8-sig', 'utf-8', 'cp1251', 'cp866', 'latin-1'):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    return raw.decode('utf-8', errors='replace')


# --- Конвертируем JSON аккаунта в строку формата logs.txt ---
# Формат: TOKEN {"deviceType":...,"deviceId":...} password
def json_to_logline(content: str):
    try:
        data = json.loads(content)
    except Exception:
        return None

    token = data.get('token', '')
    password = data.get('password') or ''
    device_id = data.get('device_id', '')
    client_session_id = data.get('client_session_id', 1)
    cp = data.get('connection_params', {})

    device_type = cp.get('device_type', '')
    app_version = cp.get('app_version', '')
    build_number = cp.get('build_number', 0)
    locale = cp.get('locale', '')
    device_locale = cp.get('device_locale', locale)
    timezone = cp.get('timezone', '')
    os_version = cp.get('os_version', '')
    device_name = cp.get('device_name', '')
    screen = cp.get('screen', '')

    # Строим headerUserAgent
    if device_type == 'IOS':
        os_ver_underscored = os_version.replace('iOS ', '').replace('.', '_')
        header_ua = (
            f"Mozilla/5.0 ({device_name}; CPU iPhone OS {os_ver_underscored} "
            f"like Mac OS X) AppleWebKit/605.1.15"
        )
    else:
        header_ua = (
            f"Mozilla/5.0 (Linux; {os_version}; {device_name}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        )

    params = {
        "deviceType": device_type,
        "appVersion": app_version,
        "buildNumber": build_number,
        "release": 1,
        "locale": locale,
        "deviceLocale": device_locale,
        "timezone": timezone,
        "clientSessionId": client_session_id,
        "osVersion": os_version,
        "deviceName": device_name,
        "screen": screen,
        "headerUserAgent": header_ua,
        "deviceId": device_id,
    }

    params_str = json.dumps(params, ensure_ascii=False, separators=(',', ':'))
    return f"{token} {params_str} {password}".strip()


# --- Обработчик текстовых сообщений -> сразу в .txt ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return

    filename = f"text_{datetime.now().strftime('%H%M%S')}.txt"
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(text)
        with open(filename, 'rb') as f:
            await update.message.reply_document(document=f, filename=filename)
    finally:
        if os.path.exists(filename):
            os.remove(filename)


# --- Обработчик ZIP ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.lower().endswith('.zip'):
        await update.message.reply_text("⚠️ Поддерживаются только .zip архивы.")
        return

    msg = await update.message.reply_text("⏳ Скачиваю архив...")

    zip_path = f"temp_{update.effective_user.id}.zip"
    file = await doc.get_file()
    await file.download_to_drive(zip_path)

    context.user_data['last_zip'] = zip_path
    context.user_data['input_mode'] = 'zip'

    await msg.edit_text(
        "📦 Архив получен. Выберите действие:",
        reply_markup=KEYBOARD,
    )


# --- Обработчик кнопок ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    input_mode = context.user_data.get('input_mode')

    if input_mode != 'zip':
        await query.edit_message_text("❌ Сначала отправь .zip архив.")
        return

    zip_path = context.user_data.get('last_zip')
    if not zip_path or not os.path.exists(zip_path):
        await query.edit_message_text("❌ Архив не найден. Отправь снова.")
        return

    await query.edit_message_text("⏳ Читаю архив...")

    user_id = update.effective_user.id
    chat_id = query.message.chat_id

    try:
        pages = []
        with zipfile.ZipFile(zip_path, 'r') as z:
            for raw_name in sorted(z.namelist()):
                display_name = decode_zip_filename(raw_name)
                ext = os.path.splitext(display_name)[1].lower()
                if ext not in ('.txt', '.json', '.dat', '.log', '.csv', '.md'):
                    continue
                with z.open(raw_name) as f:
                    content = decode_content(f.read())
                pages.append((display_name, content))

        if not pages:
            await query.edit_message_text(
                "⚠️ В архиве не найдено текстовых файлов.\n"
                "Поддерживаются: .txt .json .dat .log .csv .md"
            )
            return

        total = len(pages)

        # ТЕКСТ: каждая страница = отдельное сообщение
        if query.data == 'mode_text':
            await query.edit_message_text(f"📤 Отправляю {total} страниц текстом...")

            for idx, (name, content) in enumerate(pages, 1):
                full = content
                for chunk in [full[i:i + 4000] for i in range(0, len(full), 4000)]:
                    await context.bot.send_message(chat_id=chat_id, text=chunk)

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Готово! Страниц отправлено: {total}"
            )

        # TXT: все JSON -> формат logs.txt -> один файл
        elif query.data == 'mode_txt':
            await query.edit_message_text(f"📁 Конвертирую {total} аккаунтов...")

            tmp = f"tmp_{user_id}_merged.txt"
            converted = 0
            skipped = 0

            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    for name, content in pages:
                        ext = os.path.splitext(name)[1].lower()
                        if ext == '.json':
                            line = json_to_logline(content)
                            if line:
                                f.write(line + '\n')
                                converted += 1
                            else:
                                skipped += 1
                        else:
                            f.write(content)
                            if not content.endswith('\n'):
                                f.write('\n')
                            converted += 1

                out_name = f"logs_{converted}.txt"
                caption = f"✅ Готово! Аккаунтов: {converted}"
                if skipped:
                    caption += f" | Пропущено: {skipped}"

                with open(tmp, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=out_name,
                        caption=caption,
                    )
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)

    except zipfile.BadZipFile:
        await query.edit_message_text("❌ Повреждённый архив. Попробуй другой файл.")
    except Exception as e:
        logger.error(f"Error processing zip: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Ошибка: {e}")
    finally:
        if zip_path and os.path.exists(zip_path):
            os.remove(zip_path)
        context.user_data.pop('last_zip', None)
        context.user_data.pop('input_mode', None)


# --- Запуск ---
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("🤖 Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
