import logging
import os
import asyncio
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ============================================================
#  CONFIG — Render Environment Variables
# ============================================================
BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
DB_CHANNEL_ID     = int(os.environ.get("DB_CHANNEL_ID", "0"))
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "")
BOT_USERNAME      = os.environ.get("BOT_USERNAME", "")
WEBHOOK_URL       = os.environ.get("WEBHOOK_URL", "")
PORT              = int(os.environ.get("PORT", "10000"))

# Telethon — Tumhara personal account
API_ID            = int(os.environ.get("API_ID", "0"))       # my.telegram.org se
API_HASH          = os.environ.get("API_HASH", "")           # my.telegram.org se
SESSION_STRING    = os.environ.get("SESSION_STRING", "")     # generate_session.py se

# Target bot jisko link bhejna hai
TARGET_BOT        = "BookTherepybot"

# Custom thumbnail ka path (Render pe upload karo ya URL)
THUMBNAIL_PATH    = os.environ.get("THUMBNAIL_PATH", "thumb.jpg")
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r'https?://[^\s]+')

# User ID → uska pending message store karne ke liye
# Jab link process hoga tab use notify karenge
pending_users: dict[int, int] = {}  # {user_id: processing_msg_id}

# Telethon client (global)
telethon_client: TelegramClient = None


# ─── Subscribe check ────────────────────────────────────────
async def is_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not FORCE_SUB_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return False


# ─── /start ─────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    if not await is_subscribed(user.id, context):
        keyboard = [[InlineKeyboardButton(
            "📢 Channel Join Karo",
            url=f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}"
        )]]
        await update.message.reply_text(
            f"⚠️ *{user.first_name}*, pehle channel join karo!\n\nJoin ke baad dobara /start bhejo.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if args and args[0].startswith("file_"):
        await send_file(update, context, args[0])
        return

    keyboard = [
        [InlineKeyboardButton("📢 Hamara Channel", url=f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@') if FORCE_SUB_CHANNEL else 'telegram'}")],
    ]
    await update.message.reply_text(
        f"👋 *Namaste {user.first_name}!*\n\n"
        "🤖 *Main kya kar sakta hu:*\n\n"
        "📥 *File Download:* Channel pe upload hone wali har video ka link milega\n\n"
        "🔗 *Link Process:* Koi bhi link mujhe bhejo — main video download karke link dunga\n\n"
        "━━━━━━━━━━━━━━━━━\n"
        "👇 Seedha koi link bhejo — main process kar dunga!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─── File bhejo user ko ─────────────────────────────────────
async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str):
    try:
        msg_id = int(arg.replace("file_", ""))
        await context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=DB_CHANNEL_ID,
            message_id=msg_id
        )
        logger.info(f"File {msg_id} bheji user {update.effective_user.id} ko")
    except Exception as e:
        logger.error(f"File bhejne mein error: {e}")
        await update.message.reply_text("❌ File nahi mili. Link expire ho gaya hoga.")


# ─── User ne link bheja ─────────────────────────────────────
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    user = update.effective_user

    if not await is_subscribed(user.id, context):
        keyboard = [[InlineKeyboardButton(
            "📢 Channel Join Karo",
            url=f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}"
        )]]
        await msg.reply_text(
            "⚠️ Pehle channel join karo!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    text_content = msg.text or msg.caption or ""
    urls = URL_PATTERN.findall(text_content)

    if not urls:
        await msg.reply_text(
            "🔗 Koi valid link nahi mila!\n\nMujhe koi URL bhejo, jaise:\n`https://www.diskwala.com/app/...`",
            parse_mode="Markdown"
        )
        return

    link_to_process = urls[0]

    # User ko processing message bhejo
    processing_msg = await msg.reply_text(
        "⏳ *Link process ho raha hai...*\n\n"
        f"🔗 Link: `{link_to_process}`\n\n"
        "Kuch minute mein video ka link milega. Ruko! 🙏",
        parse_mode="Markdown"
    )

    # Pending users mein save karo
    pending_users[user.id] = processing_msg.message_id
    # Context bhi save karo taaki baad mein bot use kar sake
    context.bot_data[f"pending_{user.id}"] = {
        "chat_id": msg.chat_id,
        "msg_id": processing_msg.message_id,
        "link": link_to_process
    }

    # Telethon se BookTherapyBot ko link bhejo
    try:
        await telethon_client.send_message(TARGET_BOT, link_to_process)
        logger.info(f"Link {link_to_process} BookTherapyBot ko bheja (user: {user.id})")
    except Exception as e:
        logger.error(f"Telethon send error: {e}")
        await processing_msg.edit_text(
            "❌ Link bhejne mein error aaya. Dobara try karo."
        )


# ─── Channel pe naya video aaya ─────────────────────────────
async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return

    has_media = any([
        msg.video, msg.document, msg.audio,
        msg.photo, msg.animation, msg.voice, msg.video_note
    ])
    if not has_media:
        return

    try:
        # DB channel mein forward karo
        forwarded = await context.bot.forward_message(
            chat_id=DB_CHANNEL_ID,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id
        )

        file_ref = forwarded.message_id
        link = f"https://t.me/{BOT_USERNAME}?start=file_{file_ref}"

        # ── Thumbnail change karo (Telethon se) ──────────────
        if os.path.exists(THUMBNAIL_PATH) and msg.video:
            try:
                db_msg = await telethon_client.get_messages(DB_CHANNEL_ID, ids=file_ref)
                if db_msg and db_msg.video:
                    await telethon_client.edit_message(
                        DB_CHANNEL_ID,
                        file_ref,
                        file=db_msg.video,
                        thumb=THUMBNAIL_PATH
                    )
                    logger.info(f"Thumbnail set kiya message {file_ref} ke liye")
            except Exception as e:
                logger.warning(f"Thumbnail set nahi hua: {e}")

        # ── Media details ─────────────────────────────────────
        if msg.video:
            media_type = "🎬 Video"
            size_mb = round(msg.video.file_size / 1024 / 1024, 1) if msg.video.file_size else "?"
            dur = msg.video.duration or 0
            details = f"📐 Size: {size_mb} MB  |  ⏱ Duration: {dur // 60}:{dur % 60:02d}"
        elif msg.document:
            media_type = "📄 Document"
            size_mb = round(msg.document.file_size / 1024 / 1024, 1) if msg.document.file_size else "?"
            details = f"📐 Size: {size_mb} MB"
        elif msg.audio:
            media_type = "🎵 Audio"
            details = f"🎤 {msg.audio.title or 'Unknown'}"
        elif msg.photo:
            media_type = "🖼 Photo"
            details = "High quality image"
        else:
            media_type = "📁 File"
            details = ""

        caption = msg.caption or ""
        title_line = f"*{caption[:60]}*\n" if caption else ""

        channel_text = (
            f"{media_type} Available!\n\n"
            f"{title_line}"
            f"{details}\n\n"
            f"🔗 *Download Link:*\n`{link}`"
        )

        keyboard = [[InlineKeyboardButton("📥 Get File", url=link)]]

        # Channel pe link post karo
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=channel_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        # ── Pending users ko notify karo ──────────────────────
        notified = []
        for user_id, data in list(context.bot_data.items()):
            if not str(user_id).startswith("pending_"):
                continue
            uid = int(str(user_id).replace("pending_", ""))
            try:
                await context.bot.send_message(
                    chat_id=data["chat_id"],
                    text=(
                        f"✅ *Video ready hai!*\n\n"
                        f"{title_line}"
                        f"{details}\n\n"
                        f"🔗 *Tumhara Download Link:*\n`{link}`"
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📥 Video Download Karo", url=link)
                    ]])
                )
                # Processing message delete karo
                try:
                    await context.bot.delete_message(
                        chat_id=data["chat_id"],
                        message_id=data["msg_id"]
                    )
                except Exception:
                    pass

                notified.append(user_id)
                logger.info(f"User {uid} ko notify kiya: {link}")
            except Exception as e:
                logger.error(f"User {uid} ko notify karne mein error: {e}")

        # Notified users hata do
        for uid in notified:
            context.bot_data.pop(uid, None)

        logger.info(f"Channel link bana: {link}")

    except Exception as e:
        logger.error(f"Channel post error: {e}")


# ─── Commands ────────────────────────────────────────────────
async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"Chat ID: `{chat.id}`\nType: {chat.type}\nTitle: {chat.title or 'N/A'}",
        parse_mode="Markdown"
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending_count = sum(1 for k in context.bot_data if str(k).startswith("pending_"))
    await update.message.reply_text(
        "🤖 *Bot Status*\n\n"
        "✅ Bot chal raha hai\n"
        f"📦 DB Channel: `{DB_CHANNEL_ID}`\n"
        f"📢 Force Sub: `{FORCE_SUB_CHANNEL or 'Off'}`\n"
        f"🎯 Target Bot: `@{TARGET_BOT}`\n"
        f"⏳ Pending users: `{pending_count}`",
        parse_mode="Markdown"
    )

async def set_thumb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: photo bhejo caption mein /setthumb — thumbnail save ho jayega"""
    msg = update.message
    if not msg.photo:
        await msg.reply_text("📸 Iske saath ek photo bhejo (caption mein /setthumb likhke)")
        return
    photo = msg.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    await file.download_to_drive(THUMBNAIL_PATH)
    await msg.reply_text("✅ Thumbnail save ho gaya! Ab se naye videos pe yahi thumbnail lagega.")
    logger.info("Thumbnail update hua")


# ─── Main ────────────────────────────────────────────────────
async def run():
    global telethon_client

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN set nahi hai!")
    if not DB_CHANNEL_ID:
        raise ValueError("DB_CHANNEL_ID set nahi hai!")
    if not WEBHOOK_URL:
        raise ValueError("WEBHOOK_URL set nahi hai!")
    if not API_ID or not API_HASH or not SESSION_STRING:
        raise ValueError("API_ID, API_HASH, SESSION_STRING set nahi hain!")

    # Telethon client start karo
    telethon_client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH
    )
    await telethon_client.start()
    me = await telethon_client.get_me()
    logger.info(f"Telethon connected: {me.first_name} (@{me.username})")

    # PTB app
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getid", get_id))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.PHOTO & filters.Caption(r"^/setthumb"),
        set_thumb
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        handle_user_message
    ))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))

    webhook_path = f"/webhook/{BOT_TOKEN}"
    full_webhook_url = f"{WEBHOOK_URL.rstrip('/')}{webhook_path}"

    logger.info(f"Webhook: {full_webhook_url} | Port: {PORT}")

    await app.initialize()
    await app.start()
    await app.updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=full_webhook_url,
        drop_pending_updates=True,
    )

    logger.info("✅ Bot chal raha hai!")

    try:
        await asyncio.Event().wait()
    finally:
        await telethon_client.disconnect()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
