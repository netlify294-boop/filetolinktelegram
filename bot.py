import logging
import os
import asyncio
import re
import json
from datetime import date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ============================================================
#  CONFIG
# ============================================================
BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
DB_CHANNEL_ID     = int(os.environ.get("DB_CHANNEL_ID", "0"))
FINAL_CHANNEL_ID  = int(os.environ.get("FINAL_CHANNEL_ID", "0"))
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "")
BOT_USERNAME      = os.environ.get("BOT_USERNAME", "")
WEBHOOK_URL       = os.environ.get("WEBHOOK_URL", "")
PORT              = int(os.environ.get("PORT", "10000"))
API_ID            = int(os.environ.get("API_ID", "0"))
API_HASH          = os.environ.get("API_HASH", "")
SESSION_STRING    = os.environ.get("SESSION_STRING", "")
ADMIN_IDS         = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
SECRET_KEY        = os.environ.get("SECRET_KEY", BOT_TOKEN[:20])
TARGET_BOT        = "BookTherepybot"
THUMB_BOT         = "VideosThumb_hgbot"

SETTINGS_FILE      = "settings.json"
BANNED_FILE        = "banned.json"
USERS_FILE         = "users.json"
USAGE_FILE         = "usage.json"
THUMBNAIL_PATH     = "/tmp/thumb.jpg"
THUMB_FILE_ID_FILE = "/tmp/thumb_file_id.txt"
# ============================================================

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r'https?://[^\s]+')
telethon_client: TelegramClient = None
application_ref = []

# Processed IDs — loop rokne ke liye
processed_msg_ids: set = set()

# ThumbBot pending queue
# KEY = caption string (same caption se match karenge ThumbBot response se)
# VALUE = { "caption": ..., "bot_data": ... }
thumb_pending: dict = {}

# ════════════════════════════════════════════════════════════
#  SECURE TOKEN
# ════════════════════════════════════════════════════════════

import hashlib, hmac as hmac_mod

def make_token(msg_id: int) -> str:
    key = SECRET_KEY.encode()
    return hmac_mod.new(key, str(msg_id).encode(), hashlib.sha256).hexdigest()[:12]

def verify_token(msg_id: int, token: str) -> bool:
    return hmac_mod.compare_digest(make_token(msg_id), token)

def make_file_arg(msg_id: int) -> str:
    return f"file_{msg_id}_{make_token(msg_id)}"

def parse_file_arg(arg: str):
    parts = arg.replace("file_", "").split("_")
    if len(parts) == 2:
        try:
            msg_id = int(parts[0])
            return msg_id, verify_token(msg_id, parts[1])
        except Exception:
            pass
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
        "start_msg": "👋 *Namaste {name}!*\n\n🤖 Main File Share Bot hu.\n\n📥 *File Download:* Channel pe upload hone wali har video ka link milega\n\n🔗 *Link Process:* Koi bhi link bhejo — main video process karke link dunga\n\n━━━━━━━━━━━━━━━━━\n👇 Seedha koi link bhejo!",
        "daily_limit": 5,
        "auto_delete_seconds": 0,
        "delete_msg": "🗑 Yeh file {time} baad delete ho jayegi.",
        "after_delete_msg": "⏰ File delete ho gayi. Dobara download karne ke liye link use karo."
    })

def save_settings(data):
    save_json(SETTINGS_FILE, data)

def get_banned():
    return load_json(BANNED_FILE, [])

def is_banned(uid):
    return uid in get_banned()

def ban_user(uid):
    b = get_banned()
    if uid not in b:
        b.append(uid)
        save_json(BANNED_FILE, b)

def unban_user(uid):
    b = get_banned()
    if uid in b:
        b.remove(uid)
        save_json(BANNED_FILE, b)

def add_user(uid, name):
    u = load_json(USERS_FILE, {})
    u[str(uid)] = name
    save_json(USERS_FILE, u)

def get_all_users():
    return load_json(USERS_FILE, {})

def is_admin(uid):
    return uid in ADMIN_IDS

def check_and_increment_usage(uid: int, limit: int):
    if limit <= 0:
        return 0, True
    usage = load_json(USAGE_FILE, {})
    today = str(date.today())
    key = str(uid)
    if key not in usage or usage[key].get("date") != today:
        usage[key] = {"date": today, "count": 0}
    count = usage[key]["count"]
    if count >= limit:
        save_json(USAGE_FILE, usage)
        return count, False
    usage[key]["count"] += 1
    save_json(USAGE_FILE, usage)
    return usage[key]["count"], True

# ════════════════════════════════════════════════════════════
#  SUBSCRIBE CHECK
# ════════════════════════════════════════════════════════════

async def is_subscribed(uid, context):
    if not FORCE_SUB_CHANNEL:
        return True
    try:
        m = await context.bot.get_chat_member(FORCE_SUB_CHANNEL, uid)
        return m.status not in ("left", "kicked")
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

    if not await is_subscribed(user.id, context):
        kb = [[InlineKeyboardButton("📢 Channel Join Karo", url=f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}")]]
        await update.message.reply_text(
            f"⚠️ *{user.first_name}*, pehle channel join karo!\n\nJoin ke baad dobara /start bhejo.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return

    args = context.args
    if args and args[0].startswith("file_"):
        await send_file(update, context, args[0])
        return

    settings = get_settings()
    text = settings["start_msg"].replace("{name}", user.first_name)
    kb = []
    if FORCE_SUB_CHANNEL:
        kb.append([InlineKeyboardButton("📢 Hamara Channel", url=f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}")])
    await update.message.reply_text(text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb) if kb else None)

# ════════════════════════════════════════════════════════════
#  SEND FILE
# ════════════════════════════════════════════════════════════

async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str):
    user = update.effective_user
    if is_banned(user.id):
        await update.message.reply_text("🚫 Tum banned ho.")
        return

    msg_id, valid = parse_file_arg(arg)
    if not valid or msg_id is None:
        await update.message.reply_text("❌ Invalid ya expired link hai.")
        return

    settings = get_settings()

    if not is_admin(user.id):
        limit = settings.get("daily_limit", 5)
        count, allowed = check_and_increment_usage(user.id, limit)
        if not allowed:
            await update.message.reply_text(
                f"⚠️ *Daily limit reach ho gayi!*\n\nTum aaj `{limit}` videos download kar chuke ho.\nKal dobara aao! 🙏",
                parse_mode="Markdown")
            return

    try:
        from_ch = FINAL_CHANNEL_ID if FINAL_CHANNEL_ID else DB_CHANNEL_ID
        sent_msg = await context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=from_ch,
            message_id=msg_id
        )

        delete_after = settings.get("auto_delete_seconds", 0)
        if delete_after and delete_after > 0:
            if delete_after >= 3600:
                time_str = f"{delete_after // 3600} ghante"
            elif delete_after >= 60:
                time_str = f"{delete_after // 60} minute"
            else:
                time_str = f"{delete_after} second"

            del_notice = settings.get("delete_msg", "🗑 Yeh file {time} baad delete ho jayegi.")
            notice = await update.message.reply_text(del_notice.replace("{time}", time_str))

            async def auto_delete():
                await asyncio.sleep(delete_after)
                for mid in [sent_msg.message_id, notice.message_id]:
                    try:
                        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=mid)
                    except Exception:
                        pass
                after_msg = settings.get("after_delete_msg", "⏰ File delete ho gayi.")
                try:
                    await context.bot.send_message(chat_id=update.effective_chat.id, text=after_msg)
                except Exception:
                    pass

            asyncio.create_task(auto_delete())

        logger.info(f"File {msg_id} bheji user {user.id}")
    except Exception as e:
        logger.error(f"Forward fail: {e}")
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
        kb = [[InlineKeyboardButton("📢 Channel Join Karo", url=f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}")]]
        await msg.reply_text("⚠️ Pehle channel join karo!", reply_markup=InlineKeyboardMarkup(kb))
        return

    text_content = msg.text or msg.caption or ""
    urls = URL_PATTERN.findall(text_content)

    if not urls:
        await msg.reply_text("🔗 Koi valid link nahi mila!\n\nMujhe koi URL bhejo.", parse_mode="Markdown")
        return

    link_to_process = urls[0]
    processing_msg = await msg.reply_text(
        f"⏳ *Link process ho raha hai...*\n\n🔗 `{link_to_process}`\n\nThodi der mein video ka link milega. Ruko! 🙏",
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
        logger.error(f"Telethon send error: {e}")
        await processing_msg.edit_text(
            f"❌ Link bhejne mein error.\nError: `{type(e).__name__}`\n\nDobara try karo.",
            parse_mode="Markdown")

# ════════════════════════════════════════════════════════════
#  CHANNEL 1 POST — BookTherapyBot ne video upload ki
#  ✅ FIX: Sirf ThumbBot ko bhejo aur RETURN karo
#         Koi bhi fallback nahi — original video forward nahi hogi
# ════════════════════════════════════════════════════════════

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return

    # Sirf Channel 1 (DB_CHANNEL_ID) ke posts handle karo
    if int(msg.chat_id) != int(DB_CHANNEL_ID):
        return

    # Apna khud processed message ignore karo (loop prevention)
    if msg.message_id in processed_msg_ids:
        processed_msg_ids.discard(msg.message_id)
        logger.info(f"Processed msg {msg.message_id} ignore kiya")
        return

    has_media = any([msg.video, msg.document, msg.audio,
                     msg.photo, msg.animation, msg.voice, msg.video_note])
    if not has_media:
        return

    file_ref = msg.message_id
    caption = msg.caption or ""

    # ✅ VIDEO hai — ThumbBot ko bhejo, aage kuch nahi
    if msg.video:
        try:
            tg_msg = await telethon_client.get_messages(msg.chat_id, ids=file_ref)
            if tg_msg and tg_msg.media:
                sent = await telethon_client.send_file(
                    THUMB_BOT,
                    file=tg_msg.media,
                    caption=caption
                )
                # ✅ Caption ko KEY banao — ThumbBot same caption ke saath processed video bhejega
                thumb_pending[caption] = {
                    "caption": caption,
                    "bot_data": context.bot_data
                }
                logger.info(f"✅ Video {file_ref} ThumbBot ko bheja | caption key='{caption[:50]}'")
            else:
                logger.warning(f"tg_msg ya media nahi mila msg_id={file_ref}, skip kar raha hu")
        except Exception as e:
            logger.error(f"ThumbBot ko bhejne mein error: {e} | skip kar raha hu")
        # ✅ HAMESHA RETURN — chahe success ho ya fail
        # Original video kabhi bhi directly forward nahi hogi
        return

    # Video nahi — document/audio/photo etc. directly process karo
    logger.info(f"Non-video media {file_ref} directly process ho raha hai")
    await process_final_video(msg.chat_id, file_ref, caption, context.bot_data)

# ════════════════════════════════════════════════════════════
#  FINAL VIDEO PROCESS — link banao aur user ko do
# ════════════════════════════════════════════════════════════

async def process_final_video(source_chat_id, file_ref, caption, bot_data):
    app = application_ref[0]

    # Agar FINAL_CHANNEL_ID set hai to wahan forward karo
    if FINAL_CHANNEL_ID and int(source_chat_id) != int(FINAL_CHANNEL_ID):
        try:
            fwd = await app.bot.forward_message(
                chat_id=FINAL_CHANNEL_ID,
                from_chat_id=source_chat_id,
                message_id=file_ref
            )
            file_ref = fwd.message_id
            processed_msg_ids.add(file_ref)
            final_chat = FINAL_CHANNEL_ID
            logger.info(f"Final channel pe forward hua: new msg_id={file_ref}")
        except Exception as e:
            logger.error(f"Final channel forward error: {e}")
            final_chat = source_chat_id
    else:
        final_chat = source_chat_id

    file_arg = make_file_arg(file_ref)
    link = f"https://t.me/{BOT_USERNAME}?start={file_arg}"
    logger.info(f"Link bana: {link}")

    # Channel pe link message bhejo
    try:
        kb = [[InlineKeyboardButton("📥 Get File", url=link)]]
        await app.bot.send_message(
            chat_id=final_chat,
            text=f"🎬 Video Available!\n\n🔗 Download Link:\n`{link}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    except Exception as e:
        logger.error(f"Channel link msg error: {e}")

    # Pending users ko notify karo
    notified = []
    for key, udata in list(bot_data.items()):
        if not str(key).startswith("pending_"):
            continue
        uid = int(str(key).replace("pending_", ""))
        try:
            await app.bot.send_message(
                chat_id=udata["chat_id"],
                text=f"✅ *Video ready hai!*\n\n🔗 *Download Link:*\n`{link}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📥 Video Download Karo", url=link)]]) 
            )
            try:
                await app.bot.delete_message(chat_id=udata["chat_id"], message_id=udata["msg_id"])
            except Exception:
                pass
            notified.append(key)
            logger.info(f"User {uid} notify kiya")
        except Exception as e:
            logger.error(f"Notify error {uid}: {e}")

    for k in notified:
        bot_data.pop(k, None)

# ════════════════════════════════════════════════════════════
#  ADMIN PANEL
# ════════════════════════════════════════════════════════════

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf admins ke liye!")
        return
    await show_admin_panel(update, context)

async def show_admin_panel(update, context):
    users = get_all_users()
    banned = get_banned()
    pending = sum(1 for k in context.bot_data if str(k).startswith("pending_"))
    settings = get_settings()
    limit = settings.get("daily_limit", 5)
    del_sec = settings.get("auto_delete_seconds", 0)
    del_str = f"{del_sec}s" if del_sec else "Off"
    thumb_status = "✅" if os.path.exists(THUMBNAIL_PATH) or os.path.exists(THUMB_FILE_ID_FILE) else "❌"

    kb = [
        [InlineKeyboardButton("✏️ Start Message", callback_data="admin_setstartmsg"),
         InlineKeyboardButton("🖼 Thumbnail", callback_data="admin_setthumb")],
        [InlineKeyboardButton(f"📥 Daily Limit: {limit}", callback_data="admin_setlimit"),
         InlineKeyboardButton(f"⏱ Auto Delete: {del_str}", callback_data="admin_setdelete")],
        [InlineKeyboardButton("🗑 Delete Notice", callback_data="admin_setdelmsg"),
         InlineKeyboardButton("📩 After Delete", callback_data="admin_setafterdelmsg")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
         InlineKeyboardButton("👥 Users List", callback_data="admin_users")],
        [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban"),
         InlineKeyboardButton("✅ Unban User", callback_data="admin_unban")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
    ]
    text = (
        "🛠 *Admin Panel*\n\n"
        f"👥 Users: `{len(users)}`\n"
        f"🚫 Banned: `{len(banned)}`\n"
        f"⏳ Pending: `{pending}`\n"
        f"🖼 Thumbnail: {thumb_status}\n"
        f"📥 Daily Limit: `{limit}` videos\n"
        f"⏱ Auto Delete: `{del_str}`"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("🚫 Sirf admins ke liye!")
        return

    data = query.data
    settings = get_settings()
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]])

    if data == "admin_back":
        await show_admin_panel(update, context)
    elif data == "admin_stats":
        users = get_all_users()
        banned = get_banned()
        pending = sum(1 for k in context.bot_data if str(k).startswith("pending_"))
        await query.edit_message_text(
            f"📊 *Stats*\n\n👥 Users: `{len(users)}`\n🚫 Banned: `{len(banned)}`\n⏳ Pending: `{pending}`",
            parse_mode="Markdown", reply_markup=back_kb)
    elif data == "admin_users":
        users = get_all_users()
        banned = get_banned()
        text = "👥 *Users:*\n\n"
        for uid, name in list(users.items())[:30]:
            mark = " 🚫" if int(uid) in banned else ""
            text += f"• {name}{mark} (`{uid}`)\n"
        if len(users) > 30:
            text += f"\n...aur {len(users)-30} more"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_kb)
    elif data == "admin_setstartmsg":
        context.user_data["admin_action"] = "set_start_msg"
        await query.edit_message_text("✏️ *Start Message Set Karo*\n\n`{name}` likhne pe user ka naam aayega.\n\n/cancel se wapas.", parse_mode="Markdown")
    elif data == "admin_setthumb":
        context.user_data["admin_action"] = "set_thumb"
        await query.edit_message_text("🖼 *Thumbnail Set Karo*\n\nPhoto bhejo.\n\n/cancel se wapas.", parse_mode="Markdown")
    elif data == "admin_setlimit":
        context.user_data["admin_action"] = "set_limit"
        await query.edit_message_text(f"📥 *Daily Limit*\n\nAbhi: `{settings.get('daily_limit', 5)}`\n\nNumber bhejo (`0` = unlimited).\n\n/cancel se wapas.", parse_mode="Markdown")
    elif data == "admin_setdelete":
        context.user_data["admin_action"] = "set_delete"
        await query.edit_message_text(f"⏱ *Auto Delete*\n\nAbhi: `{settings.get('auto_delete_seconds', 0)}s`\n\nSeconds mein (`0` = off).\n• 300 = 5 min\n• 3600 = 1 ghanta\n\n/cancel se wapas.", parse_mode="Markdown")
    elif data == "admin_setdelmsg":
        context.user_data["admin_action"] = "set_del_msg"
        await query.edit_message_text(f"🗑 *Delete Notice*\n\n`{{time}}` se time aayega.\n\nAbhi:\n`{settings.get('delete_msg', '')}`\n\n/cancel se wapas.", parse_mode="Markdown")
    elif data == "admin_setafterdelmsg":
        context.user_data["admin_action"] = "set_after_del_msg"
        await query.edit_message_text(f"📩 *After Delete Message*\n\nAbhi:\n`{settings.get('after_delete_msg', '')}`\n\n/cancel se wapas.", parse_mode="Markdown")
    elif data == "admin_broadcast":
        context.user_data["admin_action"] = "broadcast"
        await query.edit_message_text("📢 *Broadcast*\n\nMessage bhejo (text/photo/video).\n\n/cancel se wapas.", parse_mode="Markdown")
    elif data == "admin_ban":
        context.user_data["admin_action"] = "ban"
        await query.edit_message_text("🚫 *Ban User*\n\nUser ID bhejo.\n\n/cancel se wapas.", parse_mode="Markdown")
    elif data == "admin_unban":
        context.user_data["admin_action"] = "unban"
        await query.edit_message_text("✅ *Unban User*\n\nUser ID bhejo.\n\n/cancel se wapas.", parse_mode="Markdown")

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
            await msg.reply_text("✅ Start message update ho gaya!")
        else:
            await msg.reply_text("⚠️ Sirf text bhejo!")

    elif action == "set_thumb":
        if msg.photo:
            photo = msg.photo[-1]
            with open(THUMB_FILE_ID_FILE, "w") as f:
                f.write(photo.file_id)
            file = await context.bot.get_file(photo.file_id)
            await file.download_to_drive(THUMBNAIL_PATH)
            context.user_data.pop("admin_action", None)
            await msg.reply_text("✅ *Thumbnail save ho gaya!*", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ Photo bhejo!")

    elif action == "set_limit":
        if msg.text and msg.text.strip().isdigit():
            settings["daily_limit"] = int(msg.text.strip())
            save_settings(settings)
            context.user_data.pop("admin_action", None)
            lim = settings["daily_limit"]
            await msg.reply_text(f"✅ Daily limit: `{'Unlimited' if lim == 0 else lim}`", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ Number bhejo!")

    elif action == "set_delete":
        if msg.text and msg.text.strip().isdigit():
            settings["auto_delete_seconds"] = int(msg.text.strip())
            save_settings(settings)
            context.user_data.pop("admin_action", None)
            sec = settings["auto_delete_seconds"]
            await msg.reply_text(f"✅ Auto delete: `{'Off' if sec == 0 else str(sec) + ' seconds'}`", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ Seconds mein number bhejo!")

    elif action == "set_del_msg":
        if msg.text:
            settings["delete_msg"] = msg.text
            save_settings(settings)
            context.user_data.pop("admin_action", None)
            await msg.reply_text("✅ Delete notice set ho gaya!")
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
                    await context.bot.send_photo(chat_id=uid, photo=msg.photo[-1].file_id, caption=msg.caption or "", parse_mode="Markdown")
                elif msg.video:
                    await context.bot.send_video(chat_id=uid, video=msg.video.file_id, caption=msg.caption or "", parse_mode="Markdown")
                elif msg.text:
                    await context.bot.send_message(chat_id=uid, text=msg.text, parse_mode="Markdown")
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
        await status_msg.edit_text(f"📢 *Done!*\n✅ Sent: `{sent}`\n❌ Failed: `{failed}`", parse_mode="Markdown")

    elif action == "ban":
        if msg.text and msg.text.strip().isdigit():
            ban_user(int(msg.text.strip()))
            context.user_data.pop("admin_action", None)
            await msg.reply_text(f"🚫 User `{msg.text.strip()}` ban!", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ User ID bhejo!")

    elif action == "unban":
        if msg.text and msg.text.strip().isdigit():
            unban_user(int(msg.text.strip()))
            context.user_data.pop("admin_action", None)
            await msg.reply_text(f"✅ User `{msg.text.strip()}` unban!", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ User ID bhejo!")

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
    await update.message.reply_text(f"🚫 `{context.args[0]}` ban!", parse_mode="Markdown")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: `/unban USER_ID`", parse_mode="Markdown"); return
    unban_user(int(context.args[0]))
    await update.message.reply_text(f"✅ `{context.args[0]}` unban!", parse_mode="Markdown")

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

    # ════════════════════════════════════════════════════════
    #  THUMBBOT RESPONSE HANDLER
    #  ✅ FIX: ThumbBot ki processed video (msg.media) forward karo
    #          Original video se koi lena dena nahi yahan
    # ════════════════════════════════════════════════════════
    @telethon_client.on(events.NewMessage(chats=THUMB_BOT))
    async def on_thumb_response(event):
        msg = event.message

        # Sirf media wale messages process karo
        if not msg.media:
            logger.info("ThumbBot se text message aaya, ignore kar raha hu")
            return

        logger.info(f"✅ ThumbBot se processed video aayi | msg_id={msg.id}")

        if not thumb_pending:
            logger.warning("thumb_pending empty hai, koi pending entry nahi")
            return

        # ✅ Caption se exact match dhundo
        incoming_caption = msg.message or ""
        logger.info(f"ThumbBot caption: '{incoming_caption[:50]}'")

        if incoming_caption in thumb_pending:
            data = thumb_pending.pop(incoming_caption)
            logger.info(f"✅ Caption exact match: '{incoming_caption[:50]}'")
        elif thumb_pending:
            pending_key = next(iter(thumb_pending))
            data = thumb_pending.pop(pending_key)
            logger.warning(f"⚠️ Caption match nahi hua, FIFO fallback: '{pending_key[:50]}'")
        else:
            logger.warning("thumb_pending empty, koi match nahi")
            return

        target_ch = FINAL_CHANNEL_ID if FINAL_CHANNEL_ID else DB_CHANNEL_ID

        try:
            # ✅ ThumbBot ki processed video (msg.media) final channel pe bhejo
            fwd = await telethon_client.send_file(
                target_ch,
                file=msg.media,          # ThumbBot ki thumbnail-changed video
                caption=data["caption"],
                supports_streaming=True
            )
            final_ref = fwd.id
            processed_msg_ids.add(final_ref)
            logger.info(f"✅ Processed video final channel pe aayi | msg_id={final_ref}")

            file_arg = make_file_arg(final_ref)
            link = f"https://t.me/{BOT_USERNAME}?start={file_arg}"
            logger.info(f"Link bana: {link}")

            app = application_ref[0]
            kb = [[InlineKeyboardButton("📥 Get File", url=link)]]

            # Final channel pe link message bhejo
            try:
                await app.bot.send_message(
                    chat_id=target_ch,
                    text=f"🎬 Video Available!\n\n🔗 Download Link:\n`{link}`",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
            except Exception as e:
                logger.error(f"Channel link msg error: {e}")

            # Pending users ko notify karo
            bot_data = data["bot_data"]
            notified = []
            for key, udata in list(bot_data.items()):
                if not str(key).startswith("pending_"):
                    continue
                uid = int(str(key).replace("pending_", ""))
                try:
                    await app.bot.send_message(
                        chat_id=udata["chat_id"],
                        text=f"✅ *Video ready hai!*\n\n🔗 *Download Link:*\n`{link}`",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📥 Video Download Karo", url=link)]])
                    )
                    try:
                        await app.bot.delete_message(chat_id=udata["chat_id"], message_id=udata["msg_id"])
                    except Exception:
                        pass
                    notified.append(key)
                    logger.info(f"User {uid} notify kiya")
                except Exception as e:
                    logger.error(f"Notify error {uid}: {e}")

            for k in notified:
                bot_data.pop(k, None)

        except Exception as e:
            logger.error(f"ThumbBot response process error: {e}")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    application_ref.append(app)

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
