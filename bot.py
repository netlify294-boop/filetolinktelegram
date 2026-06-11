import logging
import os
import asyncio
import re
import json

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import ForwardMessagesRequest
from telethon.tl.types import InputChannel

# ============================================================
#  CONFIG
# ============================================================
BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
DB_CHANNEL_ID     = int(os.environ.get("DB_CHANNEL_ID", "0"))
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "")
BOT_USERNAME      = os.environ.get("BOT_USERNAME", "")
WEBHOOK_URL       = os.environ.get("WEBHOOK_URL", "")
PORT              = int(os.environ.get("PORT", "10000"))
API_ID            = int(os.environ.get("API_ID", "0"))
API_HASH          = os.environ.get("API_HASH", "")
SESSION_STRING    = os.environ.get("SESSION_STRING", "")
ADMIN_IDS         = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
TARGET_BOT        = "BookTherepybot"

# Data files
SETTINGS_FILE  = "settings.json"
BANNED_FILE    = "banned.json"
USERS_FILE     = "users.json"
THUMBNAIL_PATH = "thumb.jpg"
# ============================================================

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r'https?://[^\s]+')
telethon_client: TelegramClient = None

# ConversationHandler states
SET_START_MSG, SET_BROADCAST = range(2)


# ════════════════════════════════════════════════════════════
#  DATA HELPERS
# ════════════════════════════════════════════════════════════

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def get_settings():
    return load_json(SETTINGS_FILE, {
        "start_msg": (
            "👋 *Namaste {name}!*\n\n"
            "🤖 Main File Share Bot hu.\n\n"
            "📥 *File Download:* Channel pe upload hone wali har video ka link milega\n\n"
            "🔗 *Link Process:* Koi bhi link bhejo — main video download karke link dunga\n\n"
            "━━━━━━━━━━━━━━━━━\n"
            "👇 Seedha koi link bhejo!"
        )
    })

def save_settings(data):
    save_json(SETTINGS_FILE, data)

def get_banned():
    return load_json(BANNED_FILE, [])

def is_banned(user_id):
    return user_id in get_banned()

def ban_user(user_id):
    banned = get_banned()
    if user_id not in banned:
        banned.append(user_id)
        save_json(BANNED_FILE, banned)

def unban_user(user_id):
    banned = get_banned()
    if user_id in banned:
        banned.remove(user_id)
        save_json(BANNED_FILE, banned)

def add_user(user_id, name):
    users = load_json(USERS_FILE, {})
    users[str(user_id)] = name
    save_json(USERS_FILE, users)

def get_all_users():
    return load_json(USERS_FILE, {})

def is_admin(user_id):
    return user_id in ADMIN_IDS


# ════════════════════════════════════════════════════════════
#  SUBSCRIBE CHECK
# ════════════════════════════════════════════════════════════

async def is_subscribed(user_id, context):
    if not FORCE_SUB_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return False


# ════════════════════════════════════════════════════════════
#  /start
# ════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.first_name)

    if is_banned(user.id):
        await update.message.reply_text("🚫 Tum banned ho. Admin se contact karo.")
        return

    args = context.args

    if not await is_subscribed(user.id, context):
        keyboard = [[InlineKeyboardButton("📢 Channel Join Karo",
            url=f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}")]]
        await update.message.reply_text(
            f"⚠️ *{user.first_name}*, pehle channel join karo!\n\nJoin ke baad dobara /start bhejo.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if args and args[0].startswith("file_"):
        await send_file(update, context, args[0])
        return

    settings = get_settings()
    msg_text = settings["start_msg"].replace("{name}", user.first_name)

    keyboard = []
    if FORCE_SUB_CHANNEL:
        keyboard.append([InlineKeyboardButton("📢 Hamara Channel",
            url=f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}")])

    await update.message.reply_text(msg_text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)


# ════════════════════════════════════════════════════════════
#  FILE SEND — Forward nahi, seedha copy
# ════════════════════════════════════════════════════════════

async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str):
    user = update.effective_user
    if is_banned(user.id):
        await update.message.reply_text("🚫 Tum banned ho.")
        return

    msg_id = int(arg.replace("file_", ""))

    try:
        await context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=DB_CHANNEL_ID,
            message_id=msg_id
        )
        logger.info(f"File {msg_id} forward kari user {user.id} ko")
    except Exception as e:
        logger.error(f"Forward fail: {type(e).__name__}: {e}")
        await update.message.reply_text("❌ File nahi mili. Link expire ho gaya hoga.")


# ════════════════════════════════════════════════════════════
#  USER MESSAGE — link process
# ════════════════════════════════════════════════════════════

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    user = update.effective_user
    add_user(user.id, user.first_name)

    if is_banned(user.id):
        await msg.reply_text("🚫 Tum banned ho. Admin se contact karo.")
        return

    if not await is_subscribed(user.id, context):
        keyboard = [[InlineKeyboardButton("📢 Channel Join Karo",
            url=f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}")]]
        await msg.reply_text("⚠️ Pehle channel join karo!",
            reply_markup=InlineKeyboardMarkup(keyboard))
        return

    text_content = msg.text or msg.caption or ""
    urls = URL_PATTERN.findall(text_content)

    if not urls:
        await msg.reply_text(
            "🔗 Koi valid link nahi mila!\n\nMujhe koi URL bhejo, jaise:\n`https://www.diskwala.com/app/...`",
            parse_mode="Markdown")
        return

    link_to_process = urls[0]
    processing_msg = await msg.reply_text(
        "⏳ *Link process ho raha hai...*\n\n"
        f"🔗 `{link_to_process}`\n\n"
        "Thodi der mein video ka link milega. Ruko! 🙏",
        parse_mode="Markdown")

    context.bot_data[f"pending_{user.id}"] = {
        "chat_id": msg.chat_id,
        "msg_id": processing_msg.message_id,
        "link": link_to_process
    }

    try:
        # Entity pehle resolve karo — username ya peer se
        try:
            target_entity = await telethon_client.get_entity(TARGET_BOT)
        except Exception:
            # Fallback: direct username string
            target_entity = TARGET_BOT

        sent = await telethon_client.send_message(target_entity, link_to_process)
        logger.info(f"✅ Link bheja BookTherapyBot ko | msg_id={sent.id} | user={user.id} | link={link_to_process}")

    except Exception as e:
        logger.error(f"Telethon send error: {type(e).__name__}: {e}")
        await processing_msg.edit_text(
            f"❌ Link BookTherapyBot ko bhejne mein error aaya.\n\n"
            f"Error: `{type(e).__name__}`\n\n"
            "Kuch der baad dobara try karo.",
            parse_mode="Markdown"
        )


# ════════════════════════════════════════════════════════════
#  CHANNEL POST — naya video aaya
# ════════════════════════════════════════════════════════════

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return

    has_media = any([msg.video, msg.document, msg.audio,
                     msg.photo, msg.animation, msg.voice, msg.video_note])
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

        # Thumbnail lagao agar file hai
        if os.path.exists(THUMBNAIL_PATH) and msg.video:
            try:
                db_msg = await telethon_client.get_messages(DB_CHANNEL_ID, ids=file_ref)
                if db_msg and db_msg.media:
                    await telethon_client.edit_message(
                        DB_CHANNEL_ID, file_ref,
                        file=db_msg.media, thumb=THUMBNAIL_PATH
                    )
                    logger.info(f"Thumbnail set: msg {file_ref}")
            except Exception as e:
                logger.warning(f"Thumbnail error: {e}")

        # Media details
        if msg.video:
            media_type = "🎬 Video"
            size_mb = round(msg.video.file_size / 1024 / 1024, 1) if msg.video.file_size else "?"
            dur = msg.video.duration or 0
            details = f"📐 Size: {size_mb} MB  |  ⏱ {dur // 60}:{dur % 60:02d}"
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
            f"{title_line}{details}\n\n"
            f"🔗 *Download Link:*\n`{link}`"
        )
        keyboard = [[InlineKeyboardButton("📥 Get File", url=link)]]

        await context.bot.send_message(
            chat_id=msg.chat_id, text=channel_text,
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

        # Pending users ko notify karo
        notified = []
        for key, data in list(context.bot_data.items()):
            if not str(key).startswith("pending_"):
                continue
            uid = int(str(key).replace("pending_", ""))
            try:
                await context.bot.send_message(
                    chat_id=data["chat_id"],
                    text=(
                        f"✅ *Video ready hai!*\n\n"
                        f"{title_line}{details}\n\n"
                        f"🔗 *Tumhara Download Link:*\n`{link}`"
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📥 Video Download Karo", url=link)
                    ]])
                )
                try:
                    await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["msg_id"])
                except Exception:
                    pass
                notified.append(key)
            except Exception as e:
                logger.error(f"Notify error user {uid}: {e}")

        for k in notified:
            context.bot_data.pop(k, None)

        logger.info(f"Channel link bana: {link}")

    except Exception as e:
        logger.error(f"Channel post error: {e}")


# ════════════════════════════════════════════════════════════
#  ADMIN PANEL
# ════════════════════════════════════════════════════════════

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("🚫 Sirf admins ke liye!")
        return

    users = get_all_users()
    banned = get_banned()
    pending = sum(1 for k in context.bot_data if str(k).startswith("pending_"))
    thumb_status = "✅ Set hai" if os.path.exists(THUMBNAIL_PATH) else "❌ Set nahi"

    keyboard = [
        [InlineKeyboardButton("✏️ Start Message", callback_data="admin_setstartmsg"),
         InlineKeyboardButton("🖼 Thumbnail", callback_data="admin_setthumb")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
         InlineKeyboardButton("👥 Users List", callback_data="admin_users")],
        [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban"),
         InlineKeyboardButton("✅ Unban User", callback_data="admin_unban")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
    ]

    await update.message.reply_text(
        "🛠 *Admin Panel*\n\n"
        f"👥 Total Users: `{len(users)}`\n"
        f"🚫 Banned: `{len(banned)}`\n"
        f"⏳ Pending requests: `{pending}`\n"
        f"🖼 Thumbnail: {thumb_status}\n\n"
        "Neeche se option chuno:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer()

    if not is_admin(user.id):
        await query.edit_message_text("🚫 Sirf admins ke liye!")
        return

    data = query.data

    if data == "admin_stats":
        users = get_all_users()
        banned = get_banned()
        pending = sum(1 for k in context.bot_data if str(k).startswith("pending_"))
        await query.edit_message_text(
            "📊 *Stats*\n\n"
            f"👥 Total Users: `{len(users)}`\n"
            f"🚫 Banned Users: `{len(banned)}`\n"
            f"⏳ Pending requests: `{pending}`\n"
            f"🖼 Thumbnail: {'✅' if os.path.exists(THUMBNAIL_PATH) else '❌'}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="admin_back")
            ]])
        )

    elif data == "admin_users":
        users = get_all_users()
        banned = get_banned()
        text = "👥 *Users List:*\n\n"
        for uid, name in list(users.items())[:30]:
            ban_mark = " 🚫" if int(uid) in banned else ""
            text += f"• {name}{ban_mark} (`{uid}`)\n"
        if len(users) > 30:
            text += f"\n...aur {len(users)-30} users"
        await query.edit_message_text(text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="admin_back")
            ]]))

    elif data == "admin_setstartmsg":
        context.user_data["admin_action"] = "set_start_msg"
        await query.edit_message_text(
            "✏️ *Start Message Set Karo*\n\n"
            "Naya start message bhejo.\n"
            "`{name}` likhne pe user ka naam aa jayega.\n\n"
            "Example:\n`👋 Namaste {name}! Welcome to our bot.`\n\n"
            "/cancel se wapas jao.",
            parse_mode="Markdown"
        )

    elif data == "admin_setthumb":
        context.user_data["admin_action"] = "set_thumb"
        await query.edit_message_text(
            "🖼 *Thumbnail Set Karo*\n\n"
            "Ab mujhe ek photo bhejo — woh thumbnail ban jayega.\n\n"
            "/cancel se wapas jao.",
            parse_mode="Markdown"
        )

    elif data == "admin_broadcast":
        context.user_data["admin_action"] = "broadcast"
        await query.edit_message_text(
            "📢 *Broadcast Message*\n\n"
            "Woh message bhejo jo sab users ko bhejna hai.\n"
            "Text, photo, video sab chalega.\n\n"
            "/cancel se wapas jao.",
            parse_mode="Markdown"
        )

    elif data == "admin_ban":
        context.user_data["admin_action"] = "ban"
        await query.edit_message_text(
            "🚫 *Ban User*\n\n"
            "User ka ID bhejo jise ban karna hai.\n"
            "Example: `123456789`\n\n"
            "/cancel se wapas jao.",
            parse_mode="Markdown"
        )

    elif data == "admin_unban":
        context.user_data["admin_action"] = "unban"
        await query.edit_message_text(
            "✅ *Unban User*\n\n"
            "User ka ID bhejo jise unban karna hai.\n"
            "Example: `123456789`\n\n"
            "/cancel se wapas jao.",
            parse_mode="Markdown"
        )

    elif data == "admin_back":
        await admin(update, context)


async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Non-admin — seedha user handler pe bhejo
    if not is_admin(user.id):
        await handle_user_message(update, context)
        return

    action = context.user_data.get("admin_action")
    if not action:
        # Admin ne koi admin action set nahi kiya — normal user ki tarah handle karo
        await handle_user_message(update, context)
        return

    msg = update.message

    # ── Set Start Message ──────────────────────────────────
    if action == "set_start_msg":
        if msg.text:
            settings = get_settings()
            settings["start_msg"] = msg.text
            save_settings(settings)
            context.user_data.pop("admin_action", None)
            await msg.reply_text(
                "✅ *Start message update ho gaya!*\n\n"
                f"Preview:\n{msg.text.replace('{name}', user.first_name)}",
                parse_mode="Markdown"
            )
        else:
            await msg.reply_text("⚠️ Sirf text message bhejo!")

    # ── Set Thumbnail ──────────────────────────────────────
    elif action == "set_thumb":
        if msg.photo:
            photo = msg.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            await file.download_to_drive(THUMBNAIL_PATH)
            context.user_data.pop("admin_action", None)
            await msg.reply_text("✅ *Thumbnail save ho gaya!*\nAb se naye videos pe yahi thumbnail lagega.", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ Photo bhejo!")

    # ── Broadcast ──────────────────────────────────────────
    elif action == "broadcast":
        users = get_all_users()
        context.user_data.pop("admin_action", None)
        sent = 0
        failed = 0
        status_msg = await msg.reply_text(f"📢 Broadcasting {len(users)} users ko...")

        for uid_str in users:
            uid = int(uid_str)
            if is_banned(uid):
                continue
            try:
                if msg.photo:
                    await context.bot.send_photo(chat_id=uid, photo=msg.photo[-1].file_id,
                        caption=msg.caption or "", parse_mode="Markdown")
                elif msg.video:
                    await context.bot.send_video(chat_id=uid, video=msg.video.file_id,
                        caption=msg.caption or "", parse_mode="Markdown")
                elif msg.text:
                    await context.bot.send_message(chat_id=uid, text=msg.text, parse_mode="Markdown")
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1

        await status_msg.edit_text(
            f"📢 *Broadcast Complete!*\n\n"
            f"✅ Sent: `{sent}`\n❌ Failed: `{failed}`",
            parse_mode="Markdown"
        )

    # ── Ban ────────────────────────────────────────────────
    elif action == "ban":
        if msg.text and msg.text.strip().isdigit():
            uid = int(msg.text.strip())
            ban_user(uid)
            context.user_data.pop("admin_action", None)
            await msg.reply_text(f"🚫 User `{uid}` ban ho gaya!", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ Sirf user ID (number) bhejo!")

    # ── Unban ──────────────────────────────────────────────
    elif action == "unban":
        if msg.text and msg.text.strip().isdigit():
            uid = int(msg.text.strip())
            unban_user(uid)
            context.user_data.pop("admin_action", None)
            await msg.reply_text(f"✅ User `{uid}` unban ho gaya!", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ Sirf user ID (number) bhejo!")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("admin_action", None)
    await update.message.reply_text("❌ Action cancel ho gaya.")
    if is_admin(update.effective_user.id):
        await admin(update, context)


# ── Direct commands ────────────────────────────────────────
async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/ban USER_ID`", parse_mode="Markdown")
        return
    uid = int(context.args[0])
    ban_user(uid)
    await update.message.reply_text(f"🚫 User `{uid}` ban ho gaya!", parse_mode="Markdown")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/unban USER_ID`", parse_mode="Markdown")
        return
    uid = int(context.args[0])
    unban_user(uid)
    await update.message.reply_text(f"✅ User `{uid}` unban ho gaya!", parse_mode="Markdown")

async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"Chat ID: `{chat.id}`\nType: {chat.type}\nTitle: {chat.title or 'N/A'}",
        parse_mode="Markdown")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    users = get_all_users()
    banned = get_banned()
    pending = sum(1 for k in context.bot_data if str(k).startswith("pending_"))
    await update.message.reply_text(
        f"📊 Users: `{len(users)}` | Banned: `{len(banned)}` | Pending: `{pending}`",
        parse_mode="Markdown")


# ════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════

async def run():
    global telethon_client

    for var, val in [("BOT_TOKEN", BOT_TOKEN), ("WEBHOOK_URL", WEBHOOK_URL)]:
        if not val:
            raise ValueError(f"{var} set nahi hai!")
    if not DB_CHANNEL_ID:
        raise ValueError("DB_CHANNEL_ID set nahi hai!")
    if not API_ID or not API_HASH or not SESSION_STRING:
        raise ValueError("API_ID / API_HASH / SESSION_STRING set nahi hai!")

    telethon_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await telethon_client.start()
    me = await telethon_client.get_me()
    logger.info(f"Telethon: {me.first_name} (@{me.username})")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("getid", get_id))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("cancel", cancel))

    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))

    # Private messages — admin input ya user link
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_admin_input
    ))

    # Channel posts
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))

    webhook_path = f"/webhook/{BOT_TOKEN}"
    full_webhook_url = f"{WEBHOOK_URL.rstrip('/')}{webhook_path}"
    logger.info(f"Webhook: {full_webhook_url} | Port: {PORT}")

    await app.initialize()
    await app.start()
    await app.updater.start_webhook(
        listen="0.0.0.0", port=PORT,
        url_path=webhook_path, webhook_url=full_webhook_url,
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
