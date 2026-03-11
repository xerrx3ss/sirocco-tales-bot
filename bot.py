import asyncio
import logging
import sqlite3
import os
from typing import Dict, List, Optional, Tuple

from telegram import Bot, Update, InputMediaPhoto, InputMediaVideo
from telegram.ext import Application, CommandHandler, ContextTypes
from telethon import TelegramClient, events
from telethon.tl.types import Message, MessageMediaPhoto, MessageMediaDocument

BOT_TOKEN = os.environ.get('BOT_TOKEN')
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH')
SOURCE_CHAT = -1003762494464
TARGET_CHANNEL = -1003542660901

ALLOW_KEYWORDS = ["· · ─────── ·🌵· ─────── · ·"]
BLOCK_KEYWORDS = ["Бемби", "бемби", "заметка"]

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

conn = sqlite3.connect('messages.db', check_same_thread=False)
conn.execute('''CREATE TABLE IF NOT EXISTS message_links
             (original_id INTEGER PRIMARY KEY,
              copy_id INTEGER,
              original_chat_id INTEGER,
              target_chat_id INTEGER,
              media_group_id TEXT,
              is_first INTEGER DEFAULT 0)''')
conn.commit()

def save_link(original_id: int, copy_id: int, original_chat_id: int,
              target_chat_id: int, media_group_id: str = None, is_first: bool = False):
    conn.execute(
        "INSERT OR REPLACE INTO message_links VALUES (?, ?, ?, ?, ?, ?)",
        (original_id, copy_id, original_chat_id, target_chat_id, media_group_id, 1 if is_first else 0)
    )
    conn.commit()

def get_copy_id(original_id: int) -> Optional[int]:
    cursor = conn.execute("SELECT copy_id FROM message_links WHERE original_id = ?", (original_id,))
    result = cursor.fetchone()
    return result[0] if result else None

def get_links_by_group(group_id: str) -> List[Tuple[int, int, bool]]:
    cursor = conn.execute(
        "SELECT original_id, copy_id, is_first FROM message_links WHERE media_group_id = ?",
        (group_id,)
    )
    return [(row[0], row[1], row[2]) for row in cursor.fetchall()]

def delete_link(original_id: int):
    conn.execute("DELETE FROM message_links WHERE original_id = ?", (original_id,))
    conn.commit()

def delete_links_by_group(group_id: str):
    conn.execute("DELETE FROM message_links WHERE media_group_id = ?", (group_id,))
    conn.commit()

def check_filters(text: str) -> Tuple[bool, Optional[str]]:
    if not text:
        return False, None
    text_lower = text.lower()
    for word in BLOCK_KEYWORDS:
        if word.lower() in text_lower:
            return False, word
    for word in ALLOW_KEYWORDS:
        if word in text:
            return True, word
    return False, None

ptb_app = Application.builder().token(BOT_TOKEN).build()
tl_client = TelegramClient('user_session', API_ID, API_HASH)

pending_albums: Dict[str, List[Message]] = {}

async def send_album_to_channel(bot: Bot, messages: List[Message]) -> Optional[str]:
    try:
        first_msg = messages[0]
        text = first_msg.text or first_msg.message or ""
        allowed, word = check_filters(text)
        if not allowed:
            return None

        media_group = []
        for i, msg in enumerate(messages):
            file = await msg.download_media(file=bytes)
            if isinstance(msg.media, MessageMediaPhoto):
                if i == 0:
                    media_group.append(InputMediaPhoto(media=file, caption=text))
                else:
                    media_group.append(InputMediaPhoto(media=file))
            elif isinstance(msg.media, MessageMediaDocument):
                if i == 0:
                    media_group.append(InputMediaVideo(media=file, caption=text))
                else:
                    media_group.append(InputMediaVideo(media=file))

        if media_group:
            sent = await bot.send_media_group(
                chat_id=TARGET_CHANNEL,
                media=media_group
            )
            group_id = str(messages[0].grouped_id)
            for i, msg in enumerate(sent):
                save_link(messages[i].id, msg.message_id, SOURCE_CHAT, TARGET_CHANNEL, group_id, i == 0)
            return group_id
    except Exception as e:
        logger.error(f"Ошибка отправки альбома: {e}")
        return None

@tl_client.on(events.NewMessage)
async def on_new_message(event):
    if event.chat_id != SOURCE_CHAT:
        return

    if event.message.grouped_id:
        group_id = str(event.message.grouped_id)
        if group_id not in pending_albums:
            pending_albums[group_id] = [event.message]
            await asyncio.sleep(2)
            messages = pending_albums.pop(group_id, [])
            if messages:
                await send_album_to_channel(ptb_app.bot, messages)
        else:
            pending_albums[group_id].append(event.message)
        return

    text = event.message.text or event.message.message or ""
    allowed, word = check_filters(text)
    if not allowed:
        return

    try:
        if event.message.media:
            file = await event.message.download_media(file=bytes)
            if isinstance(event.message.media, MessageMediaPhoto):
                sent = await ptb_app.bot.send_photo(
                    chat_id=TARGET_CHANNEL,
                    photo=file,
                    caption=text
                )
            elif isinstance(event.message.media, MessageMediaDocument):
                sent = await ptb_app.bot.send_document(
                    chat_id=TARGET_CHANNEL,
                    document=file,
                    caption=text
                )
            else:
                sent = await ptb_app.bot.send_message(
                    chat_id=TARGET_CHANNEL,
                    text=text
                )
        else:
            sent = await ptb_app.bot.send_message(
                chat_id=TARGET_CHANNEL,
                text=text
            )
        save_link(event.id, sent.message_id, SOURCE_CHAT, TARGET_CHANNEL)
        logger.info(f"✅ Сообщение {event.id} -> {sent.message_id}")
    except Exception as e:
        logger.error(f"Ошибка: {e}")

@tl_client.on(events.MessageEdited)
async def on_message_edited(event):
    if event.chat_id != SOURCE_CHAT:
        return

    text = event.message.text or event.message.message or ""
    allowed, word = check_filters(text)

    if event.message.grouped_id:
        group_id = str(event.message.grouped_id)
        links = get_links_by_group(group_id)

        if not links:
            return

        if not allowed:
            deleted_count = 0
            for orig_id, copy_id, is_first in links:
                try:
                    await ptb_app.bot.delete_message(
                        chat_id=TARGET_CHANNEL,
                        message_id=copy_id
                    )
                    deleted_count += 1
                except Exception as e:
                    logger.error(f"Ошибка удаления {copy_id}: {e}")
            delete_links_by_group(group_id)
            logger.info(f"🗑️ Весь альбом {group_id} удалён (фильтр: {word}). Удалено {deleted_count} сообщений")
            return

        for orig_id, copy_id, is_first in links:
            if is_first:
                try:
                    await ptb_app.bot.edit_message_caption(
                        chat_id=TARGET_CHANNEL,
                        message_id=copy_id,
                        caption=text
                    )
                    logger.info(f"✏️ Подпись альбома {group_id} изменена")
                except Exception as e:
                    logger.error(f"Ошибка редактирования подписи: {e}")
                break
        return

    copy_id = get_copy_id(event.id)

    if copy_id:
        if allowed:
            try:
                if event.message.media:
                    file = await event.message.download_media(file=bytes)
                    await ptb_app.bot.edit_message_media(
                        chat_id=TARGET_CHANNEL,
                        message_id=copy_id,
                        media=InputMediaPhoto(media=file, caption=text)
                    )
                else:
                    await ptb_app.bot.edit_message_text(
                        chat_id=TARGET_CHANNEL,
                        message_id=copy_id,
                        text=text
                    )
                logger.info(f"✏️ Изменено: {event.id}")
            except Exception as e:
                logger.error(f"Ошибка редактирования: {e}")
        else:
            try:
                await ptb_app.bot.delete_message(
                    chat_id=TARGET_CHANNEL,
                    message_id=copy_id
                )
                delete_link(event.id)
                logger.info(f"🗑️ Удалено (фильтр {word}): {event.id}")
            except Exception as e:
                logger.error(f"Ошибка удаления: {e}")
    else:
        if allowed:
            if event.message.media:
                file = await event.message.download_media(file=bytes)
                if isinstance(event.message.media, MessageMediaPhoto):
                    sent = await ptb_app.bot.send_photo(
                        chat_id=TARGET_CHANNEL,
                        photo=file,
                        caption=text
                    )
                else:
                    sent = await ptb_app.bot.send_document(
                        chat_id=TARGET_CHANNEL,
                        document=file,
                        caption=text
                    )
            else:
                sent = await ptb_app.bot.send_message(
                    chat_id=TARGET_CHANNEL,
                    text=text
                )
            save_link(event.id, sent.message_id, SOURCE_CHAT, TARGET_CHANNEL)
            logger.info(f"✅ Восстановлено: {event.id}")

@tl_client.on(events.MessageDeleted)
async def on_message_deleted(event):
    for msg_id in event.deleted_ids:
        copy_id = get_copy_id(msg_id)
        if copy_id:
            try:
                await ptb_app.bot.delete_message(
                    chat_id=TARGET_CHANNEL,
                    message_id=copy_id
                )
                delete_link(msg_id)
                logger.info(f"🗑️ Удалено: {msg_id}")
            except Exception as e:
                logger.error(f"Ошибка удаления {msg_id}: {e}")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor = conn.execute("SELECT COUNT(*) FROM message_links")
    count = cursor.fetchone()[0]
    await update.message.reply_text(f"📊 Всего скопировано сообщений: {count}")

ptb_app.add_handler(CommandHandler("stats", stats_command))

async def main():
    logger.info("🚀 Запуск бота...")
    await tl_client.start()
    logger.info("✅ Юзербот запущен")
    await ptb_app.initialize()
    await ptb_app.start()
    logger.info("✅ PTB бот запущен")
    try:
        await tl_client.run_until_disconnected()
    finally:
        await ptb_app.stop()
        await tl_client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
