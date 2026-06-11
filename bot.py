import logging
import os
import asyncio
import re
import json
import time
import hashlib
import hmac
from datetime import datetime, date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from telethon import TelegramClient
from telethon.sessions import StringSession

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
BOOKBOT_CHANNEL   = int(os.environ.get("BOOKBOT_CHANNEL", "0"))  # BookTherapyBot ka channel ID

# Secret key for secure tokens
SECRET_KEY        = os.environ.get("SECRET_KEY", BOT_TOKEN[:20])

SETTINGS_FILE  = "settings.json"
BANNED_FILE    = "banned.json"
USERS_FILE     = "users.json"
USAGE_FILE     = "usage.json"
THUMBNAIL_PATH = "/tmp/thumb.jpg"
THUMB_FILE_ID_FILE = "/tmp/thumb_file_id.txt"
# ============================================================

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r'https?://[^\s]+')
telethon_client: TelegramClient = None

# ════════════════════════════════════════════════════════════
#  SECURE TOKEN — link guessing se bachao
# ════════════════════════════════════════════════════════════

def make_token(msg_id: int) -> str:
    """msg_id ke liye ek secure HMAC token banao"""
    key = SECRET_KEY.encode()
    msg = str(msg_id).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:12]

def verify_token(msg_id: int, token: str) -> bool:
    """Token valid hai ya nahi check karo"""
    return hmac.compare_digest(make_token(msg_id), token)

def make_file_arg(msg_id: int) -> str:
    """file_MSGID_TOKEN format"""
    return f"file_{msg_id}_{make_token(msg_id)}"

def parse_file_arg(arg: str):
    """(msg_id, valid) return karo"""
    parts = arg.replace("file_", "").split("_")
    if len(parts) == 2:
        try:
            msg_id = int(parts[0])
            token = parts[1]
            return msg_id, verify_token(msg_id, token)
        except Exception:
            pass
    elif len(parts) == 1:
        # Purane links bina token ke — reject karo
        return None, False
    return None, False

# ════════════════════════════════════════════════════════════
#  DATA HELPERS
# ════════════════════════════════════════════════════════════

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
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
            "🔗 *Link Process:* Koi bhi link bhejo — main video process karke link dunga\n\n"
            "━━━━━━━━━━━━━━━━━\n"
            "👇 Seedha koi link bhejo!"
        ),
        "daily_limit": 5,
        "auto_delete_seconds": 0,
        "delete_msg": "🗑 Yeh file {time} baad delete ho jayegi.",
        "after_delete_msg": "⏰ File delete ho gayi. Dobara download karne ke liye link use karo."
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

# ── Daily limit tracking ────────────────────────────────────
def get_usage():
    return load_json(USAGE_FILE, {})

def check_and_increment_usage(user_id: int, limit: int) -> tuple[int, bool]:
    """(count_used, allowed) return karo"""
    if limit <= 0:
        return 0, True  # 0 = unlimited
    usage = get_usage()
    today = str(date.today())
    uid = str(user_id)
    if uid not in usage or usage[uid].get("date") != today:
        usage[uid] = {"date": today, "count": 0}
    count = usage[uid]["count"]
    if count >= limit:
        save_json(USAGE_FILE, usage)
        return count, False
    usage[uid]["count"] += 1
    save_json(USAGE_FILE, usage)
    return usage[uid]["count"], True

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
#  SEND FILE — with daily limit + secure token + auto delete
# ════════════════════════════════════════════════════════════

async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str):
    user = update.effective_user

    if is_banned(user.id):
        await update.message.reply_text("🚫 Tum banned ho.")
        return

    # Token verify karo
    msg_id, valid = parse_file_arg(arg)
    if not valid or msg_id is None:
        await update.message.reply_text("❌ Invalid ya expired link hai.")
        return

    settings = get_settings()

    # Daily limit check (admins exempt)
    if not is_admin(user.id):
        limit = settings.get("daily_limit", 5)
        count, allowed = check_and_increment_usage(user.id, limit)
        if not allowed:
            await update.message.reply_text(
                f"⚠️ *Daily limit reach ho gayi!*\n\n"
                f"Tum aaj `{limit}` videos download kar chuke ho.\n"
                f"Kal dobara aao! 🙏",
                parse_mode="Markdown"
            )
            return

    try:
        sent_msg = await context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=DB_CHANNEL_ID,
            message_id=msg_id
        )
        logger.info(f"File {msg_id} forward kari user {user.id} ko")

        # Auto delete schedule karo
        delete_after = settings.get("auto_delete_seconds", 0)
        if delete_after and delete_after > 0:
            after_msg = settings.get("after_delete_msg", "⏰ File delete ho gayi.")

            # Time display
            if delete_after >= 3600:
                time_str = f"{delete_after // 3600} ghante"
            elif delete_after >= 60:
                time_str = f"{delete_after // 60} minute"
            else:
                time_str = f"{delete_after} second"

            del_msg_text = settings.get("delete_msg", "🗑 Yeh file {time} baad delete ho jayegi.")
            notice = await update.message.reply_text(
                del_msg_text.replace("{time}", time_str)
            )

            async def auto_delete():
                await asyncio.sleep(delete_after)
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=sent_msg.message_id
                    )
                except Exception:
                    pass
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=notice.message_id
                    )
                except Exception:
                    pass
                try:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=after_msg
                    )
                except Exception:
                    pass

            asyncio.create_task(auto_delete())

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
            "🔗 Koi valid link nahi mila!\n\nMujhe koi URL bhejo.",
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
        try:
            target_entity = await telethon_client.get_entity(TARGET_BOT)
        except Exception:
            target_entity = TARGET_BOT

        sent = await telethon_client.send_message(target_entity, link_to_process)
        logger.info(f"✅ Link bheja {TARGET_BOT} | msg_id={sent.id} | user={user.id}")

    except Exception as e:
        logger.error(f"Telethon send error: {type(e).__name__}: {e}")
        await processing_msg.edit_text(
            f"❌ Link bhejne mein error aaya.\nError: `{type(e).__name__}`\n\nDobara try karo.",
            parse_mode="Markdown")

# ════════════════════════════════════════════════════════════
#  CHANNEL POST
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
        # Seedha is message ka link banao — koi forward nahi
        file_ref = msg.message_id
        file_arg = make_file_arg(file_ref)
        link = f"https://t.me/{BOT_USERNAME}?start={file_arg}"

        # ── Thumbnail inject (no download/upload) ────────────
        if msg.video:
            try:
                tg_msg = await telethon_client.get_messages(msg.chat_id, ids=file_ref)
                if tg_msg and tg_msg.media and hasattr(tg_msg.media, 'document'):
                    doc = tg_msg.media.document
                    # Thumbnail set hai to use karo, warna None (existing hatega)
                    if os.path.exists(THUMBNAIL_PATH):
                        thumb = await telethon_client.upload_file(THUMBNAIL_PATH)
                    else:
                        thumb = None
                    # Same file_id reuse — sirf thumbnail naya
                    new_msg = await telethon_client.send_file(
                        msg.chat_id,
                        file=doc,
                        thumb=thumb,
                        caption=tg_msg.message or "",
                        supports_streaming=True
                    )
                    await telethon_client.delete_messages(msg.chat_id, [file_ref])
                    file_ref = new_msg.id
                    file_arg = make_file_arg(file_ref)
                    link = f"https://t.me/{BOT_USERNAME}?start={file_arg}"
                    logger.info(f"✅ Thumbnail inject done, new msg_id={file_ref}")
            except Exception as e:
                logger.warning(f"Thumbnail inject fail, original link use hoga: {e}")

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

        # Pending users notify karo
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
    await show_admin_panel(update, context)

async def show_admin_panel(update, context):
    users = get_all_users()
    banned = get_banned()
    pending = sum(1 for k in context.bot_data if str(k).startswith("pending_"))
    settings = get_settings()
    thumb_status = "✅ Set hai" if (os.path.exists(THUMBNAIL_PATH) or os.path.exists(THUMB_FILE_ID_FILE)) else "❌ Nahi"
    limit = settings.get("daily_limit", 5)
    del_sec = settings.get("auto_delete_seconds", 0)
    del_str = f"{del_sec}s" if del_sec else "Off"

    keyboard = [
        [InlineKeyboardButton("✏️ Start Message", callback_data="admin_setstartmsg"),
         InlineKeyboardButton("🖼 Thumbnail", callback_data="admin_setthumb")],
        [InlineKeyboardButton(f"📥 Daily Limit: {limit}", callback_data="admin_setlimit"),
         InlineKeyboardButton(f"⏱ Auto Delete: {del_str}", callback_data="admin_setdelete")],
        [InlineKeyboardButton("🗑 Delete Notice Msg", callback_data="admin_setdelmsg"),
         InlineKeyboardButton("📩 After Delete Msg", callback_data="admin_setafterdelmsg")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
         InlineKeyboardButton("👥 Users List", callback_data="admin_users")],
        [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban"),
         InlineKeyboardButton("✅ Unban User", callback_data="admin_unban")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
    ]

    text = (
        "🛠 *Admin Panel*\n\n"
        f"👥 Total Users: `{len(users)}`\n"
        f"🚫 Banned: `{len(banned)}`\n"
        f"⏳ Pending: `{pending}`\n"
        f"🖼 Thumbnail: {thumb_status}\n"
        f"📥 Daily Limit: `{limit}` videos\n"
        f"⏱ Auto Delete: `{del_str}`\n\n"
        "Option chuno:"
    )

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await msg.reply_text(text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer()

    if not is_admin(user.id):
        await query.edit_message_text("🚫 Sirf admins ke liye!")
        return

    data = query.data

    if data == "admin_back":
        await show_admin_panel(update, context)

    elif data == "admin_stats":
        users = get_all_users()
        banned = get_banned()
        pending = sum(1 for k in context.bot_data if str(k).startswith("pending_"))
        await query.edit_message_text(
            f"📊 *Stats*\n\n👥 Users: `{len(users)}`\n🚫 Banned: `{len(banned)}`\n⏳ Pending: `{pending}`\n🖼 Thumb: {'✅' if os.path.exists(THUMBNAIL_PATH) else '❌'}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]))

    elif data == "admin_users":
        users = get_all_users()
        banned = get_banned()
        text = "👥 *Users List:*\n\n"
        for uid, name in list(users.items())[:30]:
            mark = " 🚫" if int(uid) in banned else ""
            text += f"• {name}{mark} (`{uid}`)\n"
        if len(users) > 30:
            text += f"\n...aur {len(users)-30} users"
        await query.edit_message_text(text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]))

    elif data == "admin_setstartmsg":
        context.user_data["admin_action"] = "set_start_msg"
        await query.edit_message_text(
            "✏️ *Start Message Set Karo*\n\n`{name}` likhne pe user ka naam aayega.\n\n/cancel se wapas jao.",
            parse_mode="Markdown")

    elif data == "admin_setthumb":
        context.user_data["admin_action"] = "set_thumb"
        await query.edit_message_text(
            "🖼 *Thumbnail Set Karo*\n\nEk photo bhejo — woh thumbnail ban jayega.\n\n/cancel se wapas jao.",
            parse_mode="Markdown")

    elif data == "admin_setlimit":
        context.user_data["admin_action"] = "set_limit"
        settings = get_settings()
        await query.edit_message_text(
            f"📥 *Daily Download Limit Set Karo*\n\nAbhi: `{settings.get('daily_limit', 5)}`\n\n"
            "Number bhejo (jaise `5`).\n`0` likhne pe unlimited ho jayega.\n\n/cancel se wapas jao.",
            parse_mode="Markdown")

    elif data == "admin_setdelete":
        context.user_data["admin_action"] = "set_delete"
        settings = get_settings()
        await query.edit_message_text(
            f"⏱ *Auto Delete Time Set Karo*\n\nAbhi: `{settings.get('auto_delete_seconds', 0)}` seconds\n\n"
            "Seconds mein number bhejo:\n"
            "• `300` = 5 minute\n• `3600` = 1 ghanta\n• `0` = auto delete off\n\n/cancel se wapas jao.",
            parse_mode="Markdown")

    elif data == "admin_setdelmsg":
        context.user_data["admin_action"] = "set_del_msg"
        settings = get_settings()
        await query.edit_message_text(
            f"🗑 *Delete Notice Message Set Karo*\n\nJab file bhejo tab yeh message aata hai.\n"
            f"`{{time}}` likhne pe time aayega.\n\nAbhi:\n`{settings.get('delete_msg', '')}`\n\n/cancel se wapas jao.",
            parse_mode="Markdown")

    elif data == "admin_setafterdelmsg":
        context.user_data["admin_action"] = "set_after_del_msg"
        settings = get_settings()
        await query.edit_message_text(
            f"📩 *After Delete Message Set Karo*\n\nFile delete hone ke baad yeh message aayega.\n\n"
            f"Abhi:\n`{settings.get('after_delete_msg', '')}`\n\n/cancel se wapas jao.",
            parse_mode="Markdown")

    elif data == "admin_broadcast":
        context.user_data["admin_action"] = "broadcast"
        await query.edit_message_text(
            "📢 *Broadcast Message*\n\nMessage bhejo (text/photo/video).\n\n/cancel se wapas jao.",
            parse_mode="Markdown")

    elif data == "admin_ban":
        context.user_data["admin_action"] = "ban"
        await query.edit_message_text(
            "🚫 *Ban User*\n\nUser ka ID bhejo.\n\n/cancel se wapas jao.",
            parse_mode="Markdown")

    elif data == "admin_unban":
        context.user_data["admin_action"] = "unban"
        await query.edit_message_text(
            "✅ *Unban User*\n\nUser ka ID bhejo.\n\n/cancel se wapas jao.",
            parse_mode="Markdown")


async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_admin(user.id):
        await handle_user_message(update, context)
        return

    action = context.user_data.get("admin_action")
    if not action:
        await handle_user_message(update, context)
        return

    msg = update.message
    settings = get_settings()

    if action == "set_start_msg":
        if msg.text:
            settings["start_msg"] = msg.text
            save_settings(settings)
            context.user_data.pop("admin_action", None)
            await msg.reply_text("✅ Start message update ho gaya!", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ Sirf text bhejo!")

    elif action == "set_thumb":
        if msg.photo:
            photo = msg.photo[-1]
            # File ID save karo (permanent)
            with open(THUMB_FILE_ID_FILE, "w") as f:
                f.write(photo.file_id)
            # Download bhi karo (Telethon ke liye)
            file = await context.bot.get_file(photo.file_id)
            await file.download_to_drive(THUMBNAIL_PATH)
            context.user_data.pop("admin_action", None)
            await msg.reply_text("✅ *Thumbnail save ho gaya!*\nAb se naye videos pe yahi thumbnail lagega.", parse_mode="Markdown")
            logger.info(f"Thumbnail saved: {photo.file_id}")
        else:
            await msg.reply_text("⚠️ Photo bhejo!")

    elif action == "set_limit":
        if msg.text and msg.text.strip().isdigit():
            settings["daily_limit"] = int(msg.text.strip())
            save_settings(settings)
            context.user_data.pop("admin_action", None)
            lim = settings["daily_limit"]
            await msg.reply_text(f"✅ Daily limit set: `{'Unlimited' if lim == 0 else lim}`", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ Sirf number bhejo!")

    elif action == "set_delete":
        if msg.text and msg.text.strip().isdigit():
            settings["auto_delete_seconds"] = int(msg.text.strip())
            save_settings(settings)
            context.user_data.pop("admin_action", None)
            sec = settings["auto_delete_seconds"]
            await msg.reply_text(f"✅ Auto delete: `{'Off' if sec == 0 else f'{sec} seconds'}`", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ Sirf number (seconds) bhejo!")

    elif action == "set_del_msg":
        if msg.text:
            settings["delete_msg"] = msg.text
            save_settings(settings)
            context.user_data.pop("admin_action", None)
            await msg.reply_text("✅ Delete notice message set ho gaya!")
        else:
            await msg.reply_text("⚠️ Text bhejo!")

    elif action == "set_after_del_msg":
        if msg.text:
            settings["after_delete_msg"] = msg.text
            save_settings(settings)
            context.user_data.pop("admin_action", None)
            await msg.reply_text("✅ After-delete message set ho gaya!")
        else:
            await msg.reply_text("⚠️ Text bhejo!")

    elif action == "broadcast":
        users = get_all_users()
        context.user_data.pop("admin_action", None)
        sent = failed = 0
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
        await status_msg.edit_text(f"📢 *Done!*\n✅ Sent: `{sent}`\n❌ Failed: `{failed}`", parse_mode="Markdown")

    elif action == "ban":
        if msg.text and msg.text.strip().isdigit():
            uid = int(msg.text.strip())
            ban_user(uid)
            context.user_data.pop("admin_action", None)
            await msg.reply_text(f"🚫 User `{uid}` ban!", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ User ID (number) bhejo!")

    elif action == "unban":
        if msg.text and msg.text.strip().isdigit():
            uid = int(msg.text.strip())
            unban_user(uid)
            context.user_data.pop("admin_action", None)
            await msg.reply_text(f"✅ User `{uid}` unban!", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ User ID (number) bhejo!")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("admin_action", None)
    await update.message.reply_text("❌ Cancel ho gaya.")
    if is_admin(update.effective_user.id):
        await admin(update, context)

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: `/ban USER_ID`", parse_mode="Markdown"); return
    ban_user(int(context.args[0]))
    await update.message.reply_text(f"🚫 User `{context.args[0]}` ban!", parse_mode="Markdown")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: `/unban USER_ID`", parse_mode="Markdown"); return
    unban_user(int(context.args[0]))
    await update.message.reply_text(f"✅ User `{context.args[0]}` unban!", parse_mode="Markdown")

async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(f"ID: `{chat.id}`\nType: {chat.type}", parse_mode="Markdown")

# ════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════

async def run():
    global telethon_client

    if not BOT_TOKEN: raise ValueError("BOT_TOKEN missing!")
    if not WEBHOOK_URL: raise ValueError("WEBHOOK_URL missing!")
    if not DB_CHANNEL_ID: raise ValueError("DB_CHANNEL_ID missing!")
    if not API_ID or not API_HASH or not SESSION_STRING:
        raise ValueError("API_ID/API_HASH/SESSION_STRING missing!")

    telethon_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await telethon_client.start()
    me = await telethon_client.get_me()
    logger.info(f"Telethon: {me.first_name} (@{me.username})")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("getid", get_id))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_admin_input))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))

    webhook_path = f"/webhook/{BOT_TOKEN}"
    full_url = f"{WEBHOOK_URL.rstrip('/')}{webhook_path}"
    logger.info(f"Webhook: {full_url} | Port: {PORT}")

    await app.initialize()
    await app.start()
    await app.updater.start_webhook(
        listen="0.0.0.0", port=PORT,
        url_path=webhook_path, webhook_url=full_url,
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
