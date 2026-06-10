import logging
import os
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# ============================================================
#  CONFIG — Render Environment Variables
# ============================================================
BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
DB_CHANNEL_ID     = int(os.environ.get("DB_CHANNEL_ID", "0"))
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "")
BOT_USERNAME      = os.environ.get("BOT_USERNAME", "")
WEBHOOK_URL       = os.environ.get("WEBHOOK_URL", "")
PORT              = int(os.environ.get("PORT", "10000"))
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def is_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not FORCE_SUB_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return False


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

    await update.message.reply_text(
        f"👋 *Namaste {user.first_name}!*\n\n"
        "🤖 Main File Share Bot hu.\n"
        "Channel pe jo bhi video/file upload hogi uska link yahan se milega.\n\n"
        f"📢 Channel: {FORCE_SUB_CHANNEL or 'N/A'}",
        parse_mode="Markdown"
    )


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
        forwarded = await context.bot.forward_message(
            chat_id=DB_CHANNEL_ID,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id
        )

        file_ref = forwarded.message_id
        link = f"https://t.me/{BOT_USERNAME}?start=file_{file_ref}"

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

        text = (
            f"{media_type} Available!\n\n"
            f"{title_line}"
            f"{details}\n\n"
            f"🔗 *Download Link:*\n`{link}`"
        )

        keyboard = [[InlineKeyboardButton("📥 Get File", url=link)]]

        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logger.info(f"Link bana: {link}")

    except Exception as e:
        logger.error(f"Channel post handle karne mein error: {e}")


async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"Chat ID: `{chat.id}`\nType: {chat.type}\nTitle: {chat.title or 'N/A'}",
        parse_mode="Markdown"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Bot Status*\n\n"
        "✅ Bot chal raha hai (Webhook mode)\n"
        f"📦 DB Channel: `{DB_CHANNEL_ID}`\n"
        f"📢 Force Sub: `{FORCE_SUB_CHANNEL or 'Off'}`",
        parse_mode="Markdown"
    )


# ─── Fix: asyncio event loop manually banao (Python 3.14 fix) ───
async def run():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN set nahi hai!")
    if not DB_CHANNEL_ID:
        raise ValueError("DB_CHANNEL_ID set nahi hai!")
    if not WEBHOOK_URL:
        raise ValueError("WEBHOOK_URL set nahi hai!")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getid", get_id))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))

    webhook_path = f"/webhook/{BOT_TOKEN}"
    full_webhook_url = f"{WEBHOOK_URL.rstrip('/')}{webhook_path}"

    logger.info(f"Webhook URL: {full_webhook_url}")
    logger.info(f"Port: {PORT}")

    await app.initialize()
    await app.start()
    await app.updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=full_webhook_url,
        drop_pending_updates=True,
    )

    logger.info("Bot chal raha hai! Ctrl+C se band karo.")

    # Hamesha chalta rahe jab tak stop signal na aaye
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
