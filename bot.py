import logging
import os
import asyncio
import re
import json
import hashlib
import hmac as hmac_mod
from datetime import date
from collections import deque

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from motor.motor_asyncio import AsyncIOMotorClient

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
MONGO_URI         = os.environ.get("MONGO_URI", "")
TARGET_BOT        = "BookTherepybot"
THUMB_BOT         = "Daddyrenamle11bot"
# ============================================================

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r'https?://[^\s]+')
telethon_client: TelegramClient = None
application_ref = []
mongo_db = None

# Loop rokne ke liye
processed_msg_ids: set = set()

# ════════════════════════════════════════════════════════════
#  QUEUE SYSTEM
# ════════════════════════════════════════════════════════════

class LinkQueue:
    def __init__(self):
        self.queue: deque = deque()   # [{user_id, chat_id, msg_id, link}]
        self.processing = False
        self.lock = asyncio.Lock()

    def position(self, user_id: int) -> int:
        """User ki queue position (1-based), 0 agar nahi"""
        for i, item in enumerate(self.queue):
            if item["user_id"] == user_id:
                return i + 1
        return 0

    async def add(self, item: dict) -> int:
        """Queue mein add karo, position return karo"""
        async with self.lock:
            # Agar user already queue mein hai to update karo
            for i, q in enumerate(self.queue):
                if q["user_id"] == item["user_id"]:
                    self.queue[i] = item
                    return i + 1
            self.queue.append(item)
            return len(self.queue)

    async def get_next(self) -> dict:
        async with self.lock:
            if self.queue:
                return self.queue.popleft()
            return None

    def size(self) -> int:
        return len(self.queue)

link_queue = LinkQueue()

# Current processing item
current_processing: dict = None

# ThumbBot pending — DB_CHANNEL_ID message ID → queue item
thumb_pending: dict = {}

# ════════════════════════════════════════════════════════════
#  SECURE TOKEN
# ════════════════════════════════════════════════════════════

def make_token(msg_id: int) -> str:
    return hmac_mod.new(SECRET_KEY.encode(), str(msg_id).encode(), hashlib.sha256).hexdigest()[:12]

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
#  MONGODB HELPERS
# ════════════════════════════════════════════════════════════

async def init_mongo():
    global mongo_db
    client = AsyncIOMotorClient(MONGO_URI)
    mongo_db = client["filesharebot"]
    logger.info("MongoDB connected!")

async def get_setting(key: str, default=None):
    doc = await mongo_db.settings.find_one({"key": key})
    return doc["value"] if doc else default

async def set_setting(key: str, value):
    await mongo_db.settings.update_one(
        {"key": key}, {"$set": {"value": value}}, upsert=True
    )

async def get_all_settings() -> dict:
    defaults = {
        "start_msg": "👋 *Namaste {name}!*\n\n🤖 Main File Share Bot hu.\n\n📥 *File Download:* Channel pe upload hone wali har video ka link milega\n\n🔗 *Link Process:* Koi bhi link bhejo — main video process karke link dunga\n\n━━━━━━━━━━━━━━━━━\n👇 Seedha koi link bhejo!",
        "daily_limit": 5,
        "auto_delete_seconds": 0,
        "delete_msg": "🗑 Yeh file {time} baad delete ho jayegi.",
        "after_delete_msg": "⏰ File delete ho gayi. Dobara download karne ke liye link use karo."
    }
    cursor = mongo_db.settings.find({})
    async for doc in cursor:
        defaults[doc["key"]] = doc["value"]
    return defaults

async def is_banned(uid: int) -> bool:
    doc = await mongo_db.banned.find_one({"uid": uid})
    return doc is not None

async def ban_user(uid: int):
    await mongo_db.banned.update_one({"uid": uid}, {"$set": {"uid": uid}}, upsert=True)

async def unban_user(uid: int):
    await mongo_db.banned.delete_one({"uid": uid})

async def add_user(uid: int, name: str):
    await mongo_db.users.update_one(
        {"uid": uid}, {"$set": {"uid": uid, "name": name}}, upsert=True
    )

async def get_all_users() -> list:
    cursor = mongo_db.users.find({})
    return await cursor.to_list(length=None)

async def check_and_increment_usage(uid: int, limit: int):
    if limit <= 0:
        return 0, True
    today = str(date.today())
    doc = await mongo_db.usage.find_one({"uid": uid, "date": today})
    count = doc["count"] if doc else 0
    if count >= limit:
        return count, False
    await mongo_db.usage.update_one(
        {"uid": uid, "date": today},
        {"$inc": {"count": 1}},
        upsert=True
    )
    return count + 1, True

async def save_thumb_file_id(file_id: str):
    await set_setting("thumb_file_id", file_id)

async def get_thumb_file_id() -> str:
    return await get_setting("thumb_file_id", "")

THUMBNAIL_PATH = "/tmp/thumb.jpg"

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
        return True

def is_admin(uid):
    return uid in ADMIN_IDS

# ════════════════════════════════════════════════════════════
#  QUEUE PROCESSOR — ek ek link process karo
# ════════════════════════════════════════════════════════════

async def process_next_in_queue():
    """Queue se next item uthao aur process karo"""
    global current_processing

    if link_queue.processing:
        return

    item = await link_queue.get_next()
    if not item:
        link_queue.processing = False
        current_processing = None
        return

    link_queue.processing = True
    current_processing = item

    try:
        app = application_ref[0]

        # User ko queue position update karo
        try:
            await app.bot.edit_message_text(
                chat_id=item["chat_id"],
                message_id=item["status_msg_id"],
                text="⚙️ *Tumhara link ab process ho raha hai...*\n\nPlease wait! 🙏",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        # BookTherapyBot ko link bhejo
        try:
            target_entity = await telethon_client.get_entity(TARGET_BOT)
        except Exception:
            target_entity = TARGET_BOT

        sent = await telethon_client.send_message(target_entity, item["link"])
        logger.info(f"Link bheja {TARGET_BOT}: {item['link']} | user={item['user_id']}")

        # Ab wait karenge channel post handler ke liye
        # Jab DB_CHANNEL_ID pe video aayegi, woh item["user_id"] se match karega

    except Exception as e:
        logger.error(f"Queue process error: {e}")
        app = application_ref[0]
        try:
            await app.bot.edit_message_text(
                chat_id=item["chat_id"],
                message_id=item["status_msg_id"],
                text=f"❌ Error aaya: `{type(e).__name__}`\n\nDobara try karo.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        link_queue.processing = False
        current_processing = None
        # Next item process karo
        asyncio.create_task(process_next_in_queue())

# ════════════════════════════════════════════════════════════
#  /start
# ════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await add_user(user.id, user.first_name)

    if await is_banned(user.id):
        await update.message.reply_text("🚫 Tum banned ho. Admin se contact karo.")
        return

    if not await is_subscribed(user.id, context):
        kb = [[InlineKeyboardButton("📢 Channel Join Karo",
            url=f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}")]]
        await update.message.reply_text(
            f"⚠️ *{user.first_name}*, pehle channel join karo!\n\nJoin ke baad dobara /start bhejo.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return

    args = context.args
    if args and args[0].startswith("file_"):
        await send_file(update, context, args[0])
        return

    settings = await get_all_settings()
    text = settings["start_msg"].replace("{name}", user.first_name)
    kb = []
    if FORCE_SUB_CHANNEL:
        kb.append([InlineKeyboardButton("📢 Hamara Channel",
            url=f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}")])
    await update.message.reply_text(text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb) if kb else None)

# ════════════════════════════════════════════════════════════
#  SEND FILE
# ════════════════════════════════════════════════════════════

async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str):
    user = update.effective_user
    if await is_banned(user.id):
        await update.message.reply_text("🚫 Tum banned ho.")
        return

    msg_id, valid = parse_file_arg(arg)
    if not valid or msg_id is None:
        await update.message.reply_text("❌ Invalid ya expired link hai.")
        return

    settings = await get_all_settings()
    if not is_admin(user.id):
        limit = settings.get("daily_limit", 5)
        count, allowed = await check_and_increment_usage(user.id, limit)
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
                        await context.bot.delete_message(
                            chat_id=update.effective_chat.id, message_id=mid)
                    except Exception:
                        pass
                after_msg = settings.get("after_delete_msg", "⏰ File delete ho gayi.")
                try:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id, text=after_msg)
                except Exception:
                    pass

            asyncio.create_task(auto_delete())

        logger.info(f"File {msg_id} bheji user {user.id}")
    except Exception as e:
        logger.error(f"Forward fail: {e}")
        await update.message.reply_text("❌ File nahi mili. Link expire ho gaya hoga.")

# ════════════════════════════════════════════════════════════
#  USER MESSAGE — link queue mein daalo
# ════════════════════════════════════════════════════════════

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    user = update.effective_user
    await add_user(user.id, user.first_name)

    if await is_banned(user.id):
        await msg.reply_text("🚫 Tum banned ho. Admin se contact karo.")
        return

    if not await is_subscribed(user.id, context):
        kb = [[InlineKeyboardButton("📢 Channel Join Karo",
            url=f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}")]]
        await msg.reply_text("⚠️ Pehle channel join karo!", reply_markup=InlineKeyboardMarkup(kb))
        return

    text_content = msg.text or msg.caption or ""
    urls = URL_PATTERN.findall(text_content)
    if not urls:
        await msg.reply_text("🔗 Koi valid link nahi mila!\n\nMujhe koi URL bhejo.", parse_mode="Markdown")
        return

    link_to_process = urls[0]

    # Queue mein daalo
    queue_size = link_queue.size()
    is_currently_processing = link_queue.processing

    if is_currently_processing or queue_size > 0:
        position = queue_size + 1  # Current processing ke baad
        status_msg = await msg.reply_text(
            f"📋 *Queue mein add ho gaya!*\n\n"
            f"🔗 Link: `{link_to_process}`\n\n"
            f"📍 Tumhari position: *#{position}*\n"
            f"⏱ Est. wait: *~{position * 45} seconds*\n\n"
            f"Jab tumhari baari aayegi, automatically process hoga! 🙏",
            parse_mode="Markdown"
        )
    else:
        status_msg = await msg.reply_text(
            f"⏳ *Link process ho raha hai...*\n\n"
            f"🔗 `{link_to_process}`\n\n"
            f"Thodi der mein video ka link milega. Ruko! 🙏",
            parse_mode="Markdown"
        )

    queue_item = {
        "user_id": user.id,
        "chat_id": msg.chat_id,
        "status_msg_id": status_msg.message_id,
        "link": link_to_process
    }

    await link_queue.add(queue_item)

    # Agar kuch process nahi ho raha to shuru karo
    if not link_queue.processing:
        asyncio.create_task(process_next_in_queue())

# ════════════════════════════════════════════════════════════
#  CHANNEL POST HANDLER
# ════════════════════════════════════════════════════════════

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_processing

    msg = update.channel_post
    if not msg:
        return

    if msg.message_id in processed_msg_ids:
        processed_msg_ids.discard(msg.message_id)
        return

    has_media = any([msg.video, msg.document, msg.audio,
                     msg.photo, msg.animation, msg.voice, msg.video_note])
    if not has_media:
        return

    try:
        file_ref = msg.message_id
        caption = msg.caption or ""

        # Channel 1 — BookTherapyBot ne video upload ki
        if int(msg.chat_id) == int(DB_CHANNEL_ID):
            if msg.video and current_processing:
                # Daddyrenamle11bot ko bhejo thumbnail change ke liye
                try:
                    tg_msg = await telethon_client.get_messages(msg.chat_id, ids=file_ref)
                    if tg_msg and tg_msg.media:
                        sent = await telethon_client.send_file(
                            THUMB_BOT,
                            file=tg_msg.media,
                            caption=caption
                        )
                        thumb_pending[sent.id] = {
                            "caption": caption,
                            "queue_item": current_processing
                        }
                        logger.info(f"Video {file_ref} {THUMB_BOT} ko bheja | tracking id={sent.id}")
                        return
                except Exception as e:
                    logger.warning(f"{THUMB_BOT} send fail, direct link: {e}")

            # Fallback ya non-video — seedha link banao
            await finish_processing(msg.chat_id, file_ref, caption, current_processing)
            return

        # Channel 2 — Daddyrenamle11bot ne thumbnail changed video upload ki
        if FINAL_CHANNEL_ID and int(msg.chat_id) == int(FINAL_CHANNEL_ID):
            logger.info(f"Channel 2 pe video: {file_ref}")
            # Thumb pending se match dhundo
            if thumb_pending:
                pending_key = next(iter(thumb_pending))
                data = thumb_pending.pop(pending_key)
                await finish_processing(msg.chat_id, file_ref, caption, data["queue_item"])
            else:
                await finish_processing(msg.chat_id, file_ref, caption, current_processing)
            return

    except Exception as e:
        logger.error(f"Channel post error: {e}")

# ════════════════════════════════════════════════════════════
#  FINISH PROCESSING — link banao, user ko do, next start karo
# ════════════════════════════════════════════════════════════

async def finish_processing(source_chat_id, file_ref, caption, queue_item):
    global current_processing

    app = application_ref[0]

    file_arg = make_file_arg(file_ref)
    link = f"https://t.me/{BOT_USERNAME}?start={file_arg}"
    logger.info(f"Link bana: {link}")

    # Channel pe link message bhejo
    try:
        kb = [[InlineKeyboardButton("📥 Get File", url=link)]]
        cap_line = f"{caption[:60]}\n" if caption else ""
        await app.bot.send_message(
            chat_id=source_chat_id,
            text=f"🎬 *Video Available!*\n\n{cap_line}🔗 *Download Link:*\n`{link}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    except Exception as e:
        logger.error(f"Channel link msg error: {e}")

    # User ko notify karo
    if queue_item:
        try:
            await app.bot.send_message(
                chat_id=queue_item["chat_id"],
                text=f"✅ *Video ready hai!*\n\n🔗 *Download Link:*\n`{link}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📥 Video Download Karo", url=link)
                ]])
            )
            try:
                await app.bot.delete_message(
                    chat_id=queue_item["chat_id"],
                    message_id=queue_item["status_msg_id"]
                )
            except Exception:
                pass
            logger.info(f"User {queue_item['user_id']} notify kiya")
        except Exception as e:
            logger.error(f"User notify error: {e}")

    # Queue ke baaki users ki position update karo
    current_processing = None
    link_queue.processing = False

    # Queue mein baaki users ko position update karo
    app_ref = application_ref[0]
    for i, item in enumerate(link_queue.queue):
        try:
            pos = i + 1
            await app_ref.bot.edit_message_text(
                chat_id=item["chat_id"],
                message_id=item["status_msg_id"],
                text=f"📋 *Queue Update*\n\n"
                     f"🔗 `{item['link']}`\n\n"
                     f"📍 Tumhari position: *#{pos}*\n"
                     f"⏱ Est. wait: *~{pos * 45} seconds*\n\n"
                     f"Thoda aur ruko! 🙏",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    # Next item process karo
    asyncio.create_task(process_next_in_queue())

# ════════════════════════════════════════════════════════════
#  ADMIN PANEL
# ════════════════════════════════════════════════════════════

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Sirf admins ke liye!")
        return
    await show_admin_panel(update, context)

async def show_admin_panel(update, context):
    users = await get_all_users()
    banned_count = await mongo_db.banned.count_documents({})
    settings = await get_all_settings()
    limit = settings.get("daily_limit", 5)
    del_sec = settings.get("auto_delete_seconds", 0)
    del_str = f"{del_sec}s" if del_sec else "Off"
    thumb_fid = await get_thumb_file_id()
    thumb_status = "✅" if thumb_fid else "❌"
    queue_size = link_queue.size()
    processing = "✅ Chal raha hai" if link_queue.processing else "⏸ Idle"

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
        f"🚫 Banned: `{banned_count}`\n"
        f"📋 Queue: `{queue_size}` pending\n"
        f"⚙️ Processor: {processing}\n"
        f"🖼 Thumbnail: {thumb_status}\n"
        f"📥 Daily Limit: `{limit}` videos\n"
        f"⏱ Auto Delete: `{del_str}`"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("🚫 Sirf admins ke liye!")
        return

    data = query.data
    settings = await get_all_settings()
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]])

    if data == "admin_back":
        await show_admin_panel(update, context)
    elif data == "admin_stats":
        users = await get_all_users()
        banned_count = await mongo_db.banned.count_documents({})
        await query.edit_message_text(
            f"📊 *Stats*\n\n"
            f"👥 Users: `{len(users)}`\n"
            f"🚫 Banned: `{banned_count}`\n"
            f"📋 Queue: `{link_queue.size()}` pending\n"
            f"⚙️ Processing: `{'Yes' if link_queue.processing else 'No'}`",
            parse_mode="Markdown", reply_markup=back_kb)
    elif data == "admin_users":
        users = await get_all_users()
        text = "👥 *Users:*\n\n"
        for u in users[:30]:
            doc = await mongo_db.banned.find_one({"uid": u["uid"]})
            mark = " 🚫" if doc else ""
            text += f"• {u['name']}{mark} (`{u['uid']}`)\n"
        if len(users) > 30:
            text += f"\n...aur {len(users)-30} more"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_kb)
    elif data == "admin_setstartmsg":
        context.user_data["admin_action"] = "set_start_msg"
        await query.edit_message_text(
            "✏️ *Start Message*\n\n`{name}` se user ka naam aayega.\n\n/cancel se wapas.",
            parse_mode="Markdown")
    elif data == "admin_setthumb":
        context.user_data["admin_action"] = "set_thumb"
        await query.edit_message_text(
            "🖼 *Thumbnail*\n\nPhoto bhejo.\n\n/cancel se wapas.", parse_mode="Markdown")
    elif data == "admin_setlimit":
        context.user_data["admin_action"] = "set_limit"
        await query.edit_message_text(
            f"📥 *Daily Limit*\n\nAbhi: `{settings.get('daily_limit', 5)}`\n\nNumber bhejo (`0` = unlimited).\n\n/cancel se wapas.",
            parse_mode="Markdown")
    elif data == "admin_setdelete":
        context.user_data["admin_action"] = "set_delete"
        await query.edit_message_text(
            f"⏱ *Auto Delete*\n\nAbhi: `{settings.get('auto_delete_seconds', 0)}s`\n\nSeconds mein (`0` = off).\n• 300 = 5 min\n• 3600 = 1 ghanta\n\n/cancel se wapas.",
            parse_mode="Markdown")
    elif data == "admin_setdelmsg":
        context.user_data["admin_action"] = "set_del_msg"
        await query.edit_message_text(
            f"🗑 *Delete Notice*\n\n`{{time}}` se time aayega.\n\nAbhi:\n`{settings.get('delete_msg', '')}`\n\n/cancel se wapas.",
            parse_mode="Markdown")
    elif data == "admin_setafterdelmsg":
        context.user_data["admin_action"] = "set_after_del_msg"
        await query.edit_message_text(
            f"📩 *After Delete Message*\n\nAbhi:\n`{settings.get('after_delete_msg', '')}`\n\n/cancel se wapas.",
            parse_mode="Markdown")
    elif data == "admin_broadcast":
        context.user_data["admin_action"] = "broadcast"
        await query.edit_message_text(
            "📢 *Broadcast*\n\nMessage bhejo (text/photo/video).\n\n/cancel se wapas.",
            parse_mode="Markdown")
    elif data == "admin_ban":
        context.user_data["admin_action"] = "ban"
        await query.edit_message_text(
            "🚫 *Ban User*\n\nUser ID bhejo.\n\n/cancel se wapas.", parse_mode="Markdown")
    elif data == "admin_unban":
        context.user_data["admin_action"] = "unban"
        await query.edit_message_text(
            "✅ *Unban User*\n\nUser ID bhejo.\n\n/cancel se wapas.", parse_mode="Markdown")

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
    settings = await get_all_settings()

    if action == "set_start_msg":
        if msg.text:
            await set_setting("start_msg", msg.text)
            context.user_data.pop("admin_action", None)
            await msg.reply_text("✅ Start message update ho gaya!")
        else:
            await msg.reply_text("⚠️ Sirf text bhejo!")

    elif action == "set_thumb":
        if msg.photo:
            photo = msg.photo[-1]
            await save_thumb_file_id(photo.file_id)
            file = await context.bot.get_file(photo.file_id)
            await file.download_to_drive(THUMBNAIL_PATH)
            context.user_data.pop("admin_action", None)
            await msg.reply_text("✅ *Thumbnail save ho gaya!*", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ Photo bhejo!")

    elif action == "set_limit":
        if msg.text and msg.text.strip().isdigit():
            await set_setting("daily_limit", int(msg.text.strip()))
            context.user_data.pop("admin_action", None)
            lim = int(msg.text.strip())
            await msg.reply_text(f"✅ Daily limit: `{'Unlimited' if lim == 0 else lim}`", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ Number bhejo!")

    elif action == "set_delete":
        if msg.text and msg.text.strip().isdigit():
            await set_setting("auto_delete_seconds", int(msg.text.strip()))
            context.user_data.pop("admin_action", None)
            sec = int(msg.text.strip())
            await msg.reply_text(f"✅ Auto delete: `{'Off' if sec == 0 else str(sec) + ' seconds'}`", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ Seconds mein number bhejo!")

    elif action == "set_del_msg":
        if msg.text:
            await set_setting("delete_msg", msg.text)
            context.user_data.pop("admin_action", None)
            await msg.reply_text("✅ Delete notice set ho gaya!")
        else:
            await msg.reply_text("⚠️ Text bhejo!")

    elif action == "set_after_del_msg":
        if msg.text:
            await set_setting("after_delete_msg", msg.text)
            context.user_data.pop("admin_action", None)
            await msg.reply_text("✅ After-delete message set ho gaya!")
        else:
            await msg.reply_text("⚠️ Text bhejo!")

    elif action == "broadcast":
        users = await get_all_users()
        context.user_data.pop("admin_action", None)
        sent = failed = 0
        status_msg = await msg.reply_text(f"📢 Broadcasting {len(users)} users ko...")
        for u in users:
            uid = u["uid"]
            if await is_banned(uid):
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
            await ban_user(int(msg.text.strip()))
            context.user_data.pop("admin_action", None)
            await msg.reply_text(f"🚫 User `{msg.text.strip()}` ban!", parse_mode="Markdown")
        else:
            await msg.reply_text("⚠️ User ID bhejo!")

    elif action == "unban":
        if msg.text and msg.text.strip().isdigit():
            await unban_user(int(msg.text.strip()))
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
    await ban_user(int(context.args[0]))
    await update.message.reply_text(f"🚫 `{context.args[0]}` ban!", parse_mode="Markdown")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: `/unban USER_ID`", parse_mode="Markdown"); return
    await unban_user(int(context.args[0]))
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
    if not MONGO_URI: raise ValueError("MONGO_URI missing!")
    if not API_ID or not API_HASH or not SESSION_STRING:
        raise ValueError("API_ID/API_HASH/SESSION_STRING missing!")

    await init_mongo()

    telethon_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await telethon_client.start()
    me = await telethon_client.get_me()
    logger.info(f"Telethon: {me.first_name} (@{me.username})")

    # Thumbnail restore karo agar file_id saved hai
    thumb_fid = await get_thumb_file_id()
    if thumb_fid and not os.path.exists(THUMBNAIL_PATH):
        try:
            app_temp = ApplicationBuilder().token(BOT_TOKEN).build()
            async with app_temp:
                tg_file = await app_temp.bot.get_file(thumb_fid)
                await tg_file.download_to_drive(THUMBNAIL_PATH)
                logger.info("Thumbnail restored from MongoDB!")
        except Exception as e:
            logger.warning(f"Thumb restore fail: {e}")

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
