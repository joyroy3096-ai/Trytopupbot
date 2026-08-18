import aiohttp
import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask

from pymongo import MongoClient

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

# ===== LOGGING =====
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ===== CONFIG =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MAIN_ADMIN = 7375002067

API_URL = "https://api.pinbot.shop/like"

# Separate API keys per like tier.
# API_KEY_100 -> used for /like, /100like, /100auto  (100 Like)
# API_KEY_200 -> used for /likes, /200like, /200auto, /autolike (200 Like)
API_KEY_100 = "100like_a637a56c-bdf8-4079-9fac-6173a41f59e8"
API_KEY_200 = "200like_dd12e9b5-0f16-4506-b0ae-03eb856b63d9"

REGION = "BD"
DEFAULT_AUTO_TIME = "08:00"   # default daily run time for auto-like plans (Asia/Dhaka)

# ===== PERSISTENT STORAGE (MONGODB ATLAS) =====
# Everything below is kept in memory for speed (same as before), but is now
# loaded from / saved to a MongoDB Atlas (free tier) database instead of a
# local JSON file. This is required on free hosts (Render/Replit/etc.) where
# the local disk is wiped on every restart/redeploy -- a JSON file there does
# NOT survive restarts, but a cloud database does.
#
# ⚠️ WARNING: Your MongoDB password is hardcoded below for convenience.
# Do NOT upload this file to a public GitHub repo or share it with anyone.
# If this ever leaks, immediately change your MongoDB Atlas password
# (Database Access -> Edit -> Edit Password).
MONGO_URI = os.environ.get("MONGO_URI", "")
DB_NAME = os.environ.get("MONGO_DB_NAME", "gsm_like_bot")

_mongo_client = None
_db = None

def get_db():
    """Lazily creates the MongoDB client/connection (sync pymongo, run in a thread)."""
    global _mongo_client, _db
    if _db is None:
        if not MONGO_URI:
            raise RuntimeError(
                "MONGO_URI environment variable is not set. "
                "Add it in your hosting platform's Environment Variables."
            )
        _mongo_client = MongoClient(MONGO_URI)
        _db = _mongo_client[DB_NAME]
    return _db

# users[user_id] = {"used": 0, "limit": 1, "is_vip": False}
# groups[group_id] = {"used": 0, "limit": 0, "is_vip": False}
users = {}
groups = {}

# unlocked_groups: groups unlocked via /id command, allowed to use like commands
unlocked_groups = set()

# autoplans[plan_id] = {
#   "id", "user_id", "chat_id", "uid", "quality", "cost",
#   "days_left", "run_time", "next_run" (datetime), "mode"
# }
autoplans = {}
_plan_id_counter = 0

def _save_data_sync():
    """Blocking Mongo write. Upserts a single 'state' document holding everything."""
    db = get_db()
    doc = {
        "_id": "bot_state",
        "users": {str(k): v for k, v in users.items()},
        "groups": {str(k): v for k, v in groups.items()},
        "unlocked_groups": [str(g) for g in unlocked_groups],
        "autoplans": {
            str(pid): {**p, "next_run": p["next_run"].isoformat()}
            for pid, p in autoplans.items()
        },
        "plan_id_counter": _plan_id_counter,
        "updated_at": datetime.utcnow().isoformat(),
    }
    db.state.replace_one({"_id": "bot_state"}, doc, upsert=True)

def save_data():
    """Fire-and-forget async save so command handlers don't block on network I/O."""
    async def _runner():
        try:
            await asyncio.to_thread(_save_data_sync)
        except Exception as e:
            logging.warning("Save data (Mongo) failed: %s", e)
    try:
        asyncio.get_running_loop()
        asyncio.create_task(_runner())
    except RuntimeError:
        try:
            _save_data_sync()
        except Exception as e:
            logging.warning("Save data (Mongo) failed: %s", e)

def load_data():
    """Restores users/groups/unlocked_groups/autoplans from MongoDB on startup."""
    global _plan_id_counter
    try:
        db = get_db()
        data = db.state.find_one({"_id": "bot_state"})
        if not data:
            logging.info("No saved state found in MongoDB yet (fresh start).")
            return

        for k, v in data.get("users", {}).items():
            users[int(k)] = v
        for k, v in data.get("groups", {}).items():
            groups[int(k)] = v
        for gid in data.get("unlocked_groups", []):
            unlocked_groups.add(int(gid))
        for pid, p in data.get("autoplans", {}).items():
            p["next_run"] = datetime.fromisoformat(p["next_run"])
            autoplans[int(pid)] = p
        _plan_id_counter = data.get("plan_id_counter", 0)

        logging.info("Loaded saved data from MongoDB: %d users, %d groups, %d autoplans",
                     len(users), len(groups), len(autoplans))
    except Exception as e:
        logging.warning("Load data (Mongo) failed: %s", e)

load_data()

# ===== HELPERS =====
def ensure_user(user_id: int):
    if user_id not in users:
        users[user_id] = {"used": 0, "limit": 0, "is_vip": False}
    return users[user_id]

def ensure_group(group_id: int):
    if group_id not in groups:
        groups[group_id] = {"used": 0, "limit": 0, "is_vip": False}
    return groups[group_id]

def html_escape(text):
    text = str(text)
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )

def now_str():
    return datetime.now(ZoneInfo("Asia/Dhaka")).strftime("%I:%M:%S %p")

# ===== CREDIT / ACCESS HELPERS =====
def is_admin(user_id: int) -> bool:
    return user_id == MAIN_ADMIN

GROUP_LOCKED_MESSAGE = (
    "🔒 এই গ্রুপে বট এখনো Unlock করা হয়নি।\n"
    "ব্যবহার করতে Admin এর সাথে যোগাযোগ করুন: https://t.me/PrantoLab"
)

async def enforce_group_lock(update: Update) -> bool:
    """
    Checks whether commands are allowed in this chat.
    Returns True if allowed to proceed. If blocked, sends the lock
    message (with admin contact link) and returns False.
    Private chats and admin are always allowed.
    """
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or not update.message:
        return True
    if chat.type not in ("group", "supergroup"):
        return True
    if is_admin(user.id):
        return True
    if chat.id in unlocked_groups:
        return True
    await update.message.reply_text(GROUP_LOCKED_MESSAGE, disable_web_page_preview=True)
    return False

def check_like_access(chat_id: int, user: dict, group: dict, cost: int, user_id: int = 0):
    """
    Returns (allowed: bool, reason: str|None, mode: str|None)
    mode is one of "user", "admin"
    """
    # Admin is always unlimited — no checks, no deduction
    if is_admin(user_id):
        return True, None, "admin"

    # Group access check — group must be unlocked via /id, or command won't work
    if chat_id not in unlocked_groups:
        return False, GROUP_LOCKED_MESSAGE, None

    # Per-user credit — user must have a limit set (admin sets it via /addlimit)
    remain = user["limit"] - user["used"]
    if user["limit"] <= 0 or remain < cost:
        return False, "❌ Limit শেষ বা Limit নেই। Admin এর সাথে যোগাযোগ করুন।", None

    return True, None, "user"

def deduct_credit(mode: str, user: dict, group: dict, cost: int):
    """Deducts credit and returns (total_str, used_now_str)."""
    if mode == "admin":
        return "∞", 0
    # "user" mode
    user["used"] += cost
    return user["limit"], user["used"]

# ===== LIKE API =====
async def call_like_api_once(uid: str, api_key: str):
    """Single API call. Returns (ok, added, name, before, after, error_text)."""
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL, params={"playerid": uid, "api_key": api_key}) as response:
                if response.status != 200:
                    return False, 0, "", "", "", f"❌ API Error: {response.status}"

                try:
                    data = await response.json(content_type=None)
                except Exception:
                    text = await response.text()
                    return False, 0, "", "", "", f"❌ Invalid API response:\n{text[:300]}"

    except Exception as e:
        logging.exception("Like request failed")
        return False, 0, "", "", "", f"❌ Request failed: {e}"

    try:
        added = int(data.get("LikesGivenByAPI", 0))
    except Exception:
        added = 0

    if added <= 0:
        error_msg = data.get("message") or data.get("error") or "❌ Failed"
        return False, 0, "", "", "", str(error_msg)

    name = html_escape(data.get("PlayerNickname", "Unknown"))
    before = html_escape(data.get("LikesbeforeCommand", "N/A"))
    after = html_escape(data.get("LikesafterCommand", "N/A"))
    return True, added, name, before, after, None

async def call_like_api(uid: str, quality: int):
    """
    Picks the correct API key for the requested Like tier:
    100 -> API_KEY_100, 200 -> API_KEY_200
    """
    api_key = API_KEY_100 if quality == 100 else API_KEY_200
    return await call_like_api_once(uid, api_key)

# ===== SHARED LIKE FLOW (used by /like /100like /likes /200like /fflike) =====
async def process_like(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: str, quality: int):
    user_obj = update.effective_user
    chat = update.effective_chat
    if not user_obj or not chat:
        return

    target = update.message if update.message else (
        update.callback_query.message if update.callback_query else None
    )
    if not target:
        return

    user_id = user_obj.id
    chat_id = chat.id
    cost = quality // 100

    user = ensure_user(user_id)
    group = ensure_group(chat_id)

    ok, reason, mode = check_like_access(chat_id, user, group, cost, user_id)
    if not ok:
        await target.reply_text(reason)
        return

    if not uid or not uid.isdigit():
        await target.reply_text("❌ UID must be numeric")
        return

    msg = await target.reply_text("⏳ Processing...")

    api_ok, added, name, before, after, err = await call_like_api(uid, quality)
    if not api_ok:
        fail_text = (
            "<pre>"
            f"{quality} LIKE FAILED ❌\n"
            f"UID    : {uid}\n"
            f"Region : {REGION}\n"
            f"Error  : {err}\n"
            f"Time   : {now_str()}\n"
            "No limit deducted."
            "</pre>"
        )
        try:
            await msg.edit_text(fail_text, parse_mode=ParseMode.HTML)
        except Exception:
            await msg.edit_text(f"❌ {err}")
        return

    MIN_LIKES_FOR_DEDUCTION = 50  # এর কম Like যোগ হলে Credit কাটা হবে না

    if added < MIN_LIKES_FOR_DEDUCTION:
        low_text = (
            "<pre>"
            f"{quality} LIKE SENT (LOW) ⚠️\n"
            f"Name           : {name}\n"
            f"UID            : {uid}\n"
            f"Region         : {REGION}\n"
            f"Likes Before   : {before}\n"
            f"Likes Added    : +{added}\n"
            f"Likes After    : {after}\n"
            f"Time           : {now_str()}\n"
            f"Reason         : {MIN_LIKES_FOR_DEDUCTION} এর কম Like যোগ হয়েছে\n"
            "No limit deducted."
            "</pre>"
        )
        try:
            await msg.edit_text(low_text, parse_mode=ParseMode.HTML)
        except Exception:
            await msg.edit_text(f"⚠️ শুধু {added} Like যোগ হয়েছে, Credit কাটা হয়নি")
        return

    total, used_now = deduct_credit(mode, user, group, cost)
    remain_display = "∞" if mode == "admin" else str(max(int(total) - int(used_now), 0))
    deduct_display = "∞ (Admin)" if mode == "admin" else f"{cost} | Now: {remain_display}"
    ordered_by = html_escape(user_obj.first_name or "Unknown")

    header_line = (
        f'{quality} '
        '<tg-emoji emoji-id="6267161573324757039">👍</tg-emoji> '
        'SENT SUCCESS! '
        '<tg-emoji emoji-id="6291956780301816432">✅</tg-emoji>'
    )
    response_text = (
        f"{header_line}\n"
        "<pre>"
        f"Name           : {name}\n"
        f"UID            : {uid}\n"
        f"Region         : {REGION}\n"
        f"Likes Before   : {before}\n"
        f"Likes Added    : +{added}\n"
        f"Likes After    : {after}\n"
        f"Time           : {now_str()}\n"
        f"Ordered By     : {ordered_by}\n"
        f"Limit Deducted : {deduct_display}"
        "</pre>\n"
        "✦⃝★ Supported By : Pranto <tg-emoji emoji-id=\"6267097569722111582\">👑</tg-emoji> ✿𓆩"
    )

    try:
        await msg.edit_text(response_text, parse_mode=ParseMode.HTML)
    except Exception:
        await msg.edit_text("✅ Like sent successfully")

    save_data()

    try:
        await context.bot.send_message(
            chat_id=MAIN_ADMIN,
            text=response_text,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logging.warning("Admin log send failed: %s", e)

# ===== START =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not await enforce_group_lock(update):
        return
    await update.message.reply_text("✅ Bot started successfully.")

# ===== HELP (user commands only) =====
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not await enforce_group_lock(update):
        return
    text = (
        "<pre>"
        "╭─────────────────────────╮\n"
        "│ 🤖 BOT COMMANDS\n"
        "│──────────────────────────\n"
        "│ LIKE\n"
        "│ /like UID      - 100 Like (1 credit)\n"
        "│ /likes UID     - 200 Like (2 credit)\n"
        "│ /200like UID   - 200 Like (2 credit)\n"
        "│ /fflike UID    - Choose 100/200 Like\n"
        "│──────────────────────────\n"
        "│ AUTO LIKE\n"
        "│ /100auto UID DAYS  - Daily 100 Like\n"
        "│ /200auto UID DAYS  - Daily 200 Like\n"
        "│ /autostats         - Your auto plans\n"
        "│ /stopplan ID       - Stop your plan\n"
        "│──────────────────────────\n"
        "│ ACCOUNT\n"
        "│ /balance  - Your credit balance\n"
        "│ /remain   - Remaining credits\n"
        "╰─────────────────────────╯"
        "</pre>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ===== ADMIN HELP =====
async def adminhelp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != MAIN_ADMIN:
        return
    text = (
        "<pre>"
        "╭─────────────────────────╮\n"
        "│ 🤖 ALL COMMANDS\n"
        "│──────────────────────────\n"
        "│ USER COMMANDS\n"
        "│ /like UID      - 100 Like (1 credit)\n"
        "│ /likes UID     - 200 Like (2 credit)\n"
        "│ /200like UID   - 200 Like (2 credit)\n"
        "│ /fflike UID    - Choose 100/200 Like\n"
        "│ /100auto UID DAYS  - Daily 100 Like\n"
        "│ /200auto UID DAYS  - Daily 200 Like\n"
        "│ /autostats         - Your auto plans\n"
        "│ /stopplan ID       - Stop your plan\n"
        "│ /balance  - Your credit balance\n"
        "│ /remain   - Remaining credits\n"
        "│──────────────────────────\n"
        "│ ADMIN COMMANDS\n"
        "│ /id              - Show & unlock group\n"
        "│ /addlimit        - Reply user + set limit\n"
        "│ /addlimit_id UID LIMIT - Set by user ID\n"
        "│ /removelimit     - Reply user + subtract credit\n"
        "│ /removeuser UID  - Delete user data\n"
        "│ /removegroup GID - Delete group data\n"
        "│ /cancelplan ID   - Cancel any auto plan\n"
        "│ /settime HH:MM PLAN_ID - Change plan time\n"
        "│ /vipusers        - List user credit balances\n"
        "│ /users           - All users report (.txt)\n"
        "│ /autotasks       - All auto tasks (.txt)\n"
        "╰─────────────────────────╯"
        "</pre>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ===== LIKE COMMANDS =====
async def like_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/like UID and /100like UID -> 100 Like (1 credit)"""
    if not update.effective_user or not update.effective_chat or not update.message:
        return
    if not await enforce_group_lock(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /like UID")
        return
    await process_like(update, context, context.args[0].strip(), 100)

async def likes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/likes UID and /200like UID -> 200 Like (2 credits)"""
    if not update.effective_user or not update.effective_chat or not update.message:
        return
    if not await enforce_group_lock(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /likes UID")
        return
    await process_like(update, context, context.args[0].strip(), 200)

async def fflike_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/fflike UID -> choose 100 or 200 like via buttons"""
    if not update.effective_user or not update.effective_chat or not update.message:
        return
    if not await enforce_group_lock(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /fflike UID")
        return

    uid = context.args[0].strip()
    if not uid.isdigit():
        await update.message.reply_text("❌ UID must be numeric")
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💯 100 Like (1 Credit)", callback_data=f"fflike|100|{uid}"),
            InlineKeyboardButton("🔥 200 Like (2 Credit)", callback_data=f"fflike|200|{uid}")
        ]
    ])
    await update.message.reply_text(f"UID: {uid}\nকতটা Like দিবেন?", reply_markup=keyboard)

async def fflike_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    try:
        _, quality_str, uid = query.data.split("|")
        quality = int(quality_str)
    except Exception:
        return

    await process_like(update, context, uid, quality)

# ===== REMAIN =====
async def remain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_chat or not update.message:
        return
    if not await enforce_group_lock(update):
        return

    user_id = update.effective_user.id
    chat = update.effective_chat
    chat_id = chat.id

    name = update.effective_user.first_name

    if is_admin(user_id):
        response = (
            "<pre>"
            "╭─────────────────────────╮\n"
            "│ 📊 REMAIN STATUS INFO\n"
            f"│ Name        : {name}\n"
            "│ Mode        : 👑 ADMIN\n"
            "│ Total Limit : ∞ Unlimited\n"
            "│ Used        : —\n"
            "│ Remaining   : ∞ Unlimited\n"
            "╰─────────────────────────╯"
            "</pre>\n"
            "⚡ Stay active & enjoy 🥰"
        )
        await update.message.reply_text(response, parse_mode=ParseMode.HTML)
        return

    user = ensure_user(user_id)

    total = user["limit"]
    used = user["used"]
    mode = "USER"

    remain_val = max(total - used, 0)

    response = (
        "<pre>"
        "╭─────────────────────────╮\n"
        "│ 📊 REMAIN STATUS INFO\n"
        f"│ Name        : {name}\n"
        f"│ Mode        : {mode}\n"
        f"│ Total Limit : {total}\n"
        f"│ Used        : {used}\n"
        f"│ Remaining   : {remain_val}\n"
        "╰─────────────────────────╯"
        "</pre>\n"
        "⚡ Stay active & enjoy 🥰"
    )

    await update.message.reply_text(response, parse_mode=ParseMode.HTML)

# ===== BALANCE =====
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/balance -> your credit balance (user or group VIP)"""
    if not update.effective_user or not update.effective_chat or not update.message:
        return
    if not await enforce_group_lock(update):
        return

    user_id = update.effective_user.id
    chat = update.effective_chat
    chat_id = chat.id
    name = update.effective_user.first_name

    if is_admin(user_id):
        text = (
            "<pre>"
            "╭─────────────────────────╮\n"
            "│ 💳 YOUR BALANCE\n"
            f"│ Name      : {name}\n"
            "│ Mode      : 👑 ADMIN\n"
            "│ Total     : ∞ Unlimited\n"
            "│ Used      : —\n"
            "│ Remaining : ∞ Unlimited\n"
            "╰─────────────────────────╯"
            "</pre>"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    user = ensure_user(user_id)

    total, used, mode = user["limit"], user["used"], "USER"

    remain_val = max(total - used, 0)

    text = (
        "<pre>"
        "╭─────────────────────────╮\n"
        "│ 💳 YOUR BALANCE\n"
        f"│ Name      : {name}\n"
        f"│ Mode      : {mode}\n"
        f"│ Total     : {total} Credit\n"
        f"│ Used      : {used} Credit\n"
        f"│ Remaining : {remain_val} Credit\n"
        "╰─────────────────────────╯"
        "</pre>"
    )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ===== ID (Admin only — unlocks the group for like commands) =====
async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.effective_user or not update.message:
        return

    # Non-admin gets a clear message (no silent ignore)
    if update.effective_user.id != MAIN_ADMIN:
        await update.message.reply_text(
            "❌ এই কমান্ড শুধুমাত্র Admin ব্যবহার করতে পারবেন।\n"
            "যোগাযোগ: https://t.me/PrantoLab",
            disable_web_page_preview=True
        )
        return

    chat = update.effective_chat
    user = update.effective_user

    if chat.type in ("group", "supergroup"):
        unlocked_groups.add(chat.id)
        save_data()
        text = (
            "<pre>"
            f"Group ID : {chat.id}\n"
            f"Your ID  : {user.id}\n"
            "</pre>\n"
            "✅ এই গ্রুপ এখন Like কমান্ডের জন্য Unlock হয়ে গেছে!"
        )
    else:
        text = (
            "<pre>"
            f"Your ID : {user.id}\n"
            "</pre>"
        )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ===== USERS (Admin) =====
async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/users -> send all signed-up users as a .txt file (admin only)"""
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != MAIN_ADMIN:
        return

    if not users and not groups:
        await update.message.reply_text("No users found.")
        return

    import io
    lines = []
    lines.append("=" * 50)
    lines.append("ALL USERS REPORT")
    lines.append(f"Total Users: {len(users)}")
    lines.append("=" * 50)
    lines.append("")

    for uid, data in users.items():
        limit = data.get("limit", 0)
        used = data.get("used", 0)
        remain = max(limit - used, 0)
        vip = "VIP" if data.get("is_vip") else "USER"
        lines.append(f"User ID   : {uid}")
        lines.append(f"Type      : {vip}")
        lines.append(f"Limit     : {limit}")
        lines.append(f"Used      : {used}")
        lines.append(f"Remaining : {remain}")
        lines.append("-" * 30)

    lines.append("")
    lines.append("=" * 50)
    lines.append("ALL GROUPS")
    lines.append(f"Total Groups: {len(groups)}")
    lines.append("=" * 50)
    lines.append("")

    for gid, data in groups.items():
        limit = data.get("limit", 0)
        used = data.get("used", 0)
        remain = max(limit - used, 0)
        vip = "VIP GROUP" if data.get("is_vip") else "GROUP"
        unlocked = "Yes" if gid in unlocked_groups else "No"
        lines.append(f"Group ID  : {gid}")
        lines.append(f"Type      : {vip}")
        lines.append(f"Limit     : {limit}")
        lines.append(f"Used      : {used}")
        lines.append(f"Remaining : {remain}")
        lines.append(f"Unlocked  : {unlocked}")
        lines.append("-" * 30)

    content = "\n".join(lines)
    bio = io.BytesIO(content.encode("utf-8"))
    bio.name = "users_report.txt"
    bio.seek(0)
    await update.message.reply_document(
        document=bio,
        filename="users_report.txt",
        caption=f"📋 Users Report — {len(users)} users, {len(groups)} groups"
    )

# ===== AUTOTASKS (Admin) =====
async def autotasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/autotasks -> send all active auto-like plans as a .txt file (admin only)"""
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != MAIN_ADMIN:
        return

    if not autoplans:
        await update.message.reply_text("No active auto tasks.")
        return

    import io
    lines = []
    lines.append("=" * 50)
    lines.append("ALL AUTO LIKE TASKS")
    lines.append(f"Total Plans: {len(autoplans)}")
    lines.append("=" * 50)
    lines.append("")

    for plan_id, p in autoplans.items():
        next_run_str = p["next_run"].strftime("%Y-%m-%d %H:%M") if hasattr(p["next_run"], "strftime") else str(p["next_run"])
        lines.append(f"Plan ID   : {plan_id}")
        lines.append(f"User ID   : {p.get('user_id', '?')}")
        lines.append(f"Chat ID   : {p.get('chat_id', '?')}")
        lines.append(f"UID       : {p.get('uid', '?')}")
        lines.append(f"Quality   : {p.get('quality', '?')} Like/day")
        lines.append(f"Days Left : {p.get('days_left', '?')}")
        lines.append(f"Run Time  : {p.get('run_time', '?')} (Asia/Dhaka)")
        lines.append(f"Next Run  : {next_run_str}")
        lines.append(f"Mode      : {p.get('mode', '?')}")
        lines.append(f"By        : {p.get('by_name', '?')}")
        lines.append("-" * 30)

    content = "\n".join(lines)
    bio = io.BytesIO(content.encode("utf-8"))
    bio.name = "autotasks_report.txt"
    bio.seek(0)
    await update.message.reply_document(
        document=bio,
        filename="autotasks_report.txt",
        caption=f"🤖 Auto Tasks Report — {len(autoplans)} active plans"
    )

# ===== VIP USERS =====
async def vipusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    if update.effective_user.id != MAIN_ADMIN:
        return

    vip_list = [(uid, data["limit"], data["used"]) for uid, data in users.items() if data["is_vip"]]

    if not vip_list:
        await update.message.reply_text("No credit users found")
        return

    blocks = []
    for uid, limit, used in vip_list:
        remain = max(limit - used, 0)

        # Try to fetch live name/username from Telegram
        name = "Unknown"
        username = "—"
        try:
            chat = await context.bot.get_chat(uid)
            name = html_escape(chat.first_name or chat.full_name or "Unknown")
            username = f"@{chat.username}" if chat.username else "No username"
        except Exception:
            pass

        blocks.append(
            "├─────────────────────────\n"
            f"│ Name     : {name}\n"
            f"│ Username : {username}\n"
            f"│ User ID  : {uid}\n"
            f"│ Used     : {used}/{limit}\n"
            f"│ Remaining: {remain}"
        )

    body = "\n".join(blocks)
    text = (
        "<pre>"
        "╭─────────────────────────╮\n"
        "│ 👥 CREDIT USERS LIST\n"
        f"{body}\n"
        "╰─────────────────────────╯"
        "</pre>"
    )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ===== ADD LIMIT (formerly userlimit) =====
async def addlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    if update.effective_user.id != MAIN_ADMIN:
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user message with /addlimit <number>")
        return

    if not context.args:
        await update.message.reply_text("Usage: /addlimit 10")
        return

    try:
        limit = int(context.args[0])
        if limit < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Limit must be a positive number")
        return

    target_user = update.message.reply_to_message.from_user
    uid = target_user.id
    username = f"@{target_user.username}" if target_user.username else (target_user.first_name or "Unknown")
    user = ensure_user(uid)
    user["is_vip"] = True
    user["limit"] += limit  # top-up: add to existing balance, don't reset used
    save_data()

    remain = max(user["limit"] - user["used"], 0)
    text = (
        "<pre>"
        "╭─────────────────────────╮\n"
        "│ ✅ Credit Added!\n"
        f"│ Username : {username}\n"
        f"│ User ID  : {uid}\n"
        f"│ Added    : +{limit}\n"
        f"│ Total    : {user['limit']}\n"
        f"│ Used     : {user['used']}\n"
        f"│ Remaining: {remain}\n"
        "╰─────────────────────────╯"
        "</pre>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ===== REMOVE LIMIT (subtract credit — for correcting mistakes) =====
async def removelimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    if update.effective_user.id != MAIN_ADMIN:
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user message with /removelimit <number>")
        return

    if not context.args:
        await update.message.reply_text("Usage: /removelimit 2")
        return

    try:
        amount = int(context.args[0])
        if amount < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Amount must be a positive number")
        return

    target_user = update.message.reply_to_message.from_user
    uid = target_user.id
    username = f"@{target_user.username}" if target_user.username else (target_user.first_name or "Unknown")
    user = ensure_user(uid)
    user["limit"] = max(user["limit"] - amount, 0)  # subtract, never below 0
    save_data()

    remain = max(user["limit"] - user["used"], 0)
    text = (
        "<pre>"
        "╭─────────────────────────╮\n"
        "│ ✅ Credit Removed!\n"
        f"│ Username : {username}\n"
        f"│ User ID  : {uid}\n"
        f"│ Removed  : -{amount}\n"
        f"│ Total    : {user['limit']}\n"
        f"│ Used     : {user['used']}\n"
        f"│ Remaining: {remain}\n"
        "╰─────────────────────────╯"
        "</pre>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ===== AUTO LIKE HELPERS =====
def _new_plan_id():
    global _plan_id_counter
    _plan_id_counter += 1
    return _plan_id_counter

def _next_run_dt(run_time_str: str):
    hh, mm = map(int, run_time_str.split(":"))
    now = datetime.now(ZoneInfo("Asia/Dhaka"))
    nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return nxt

async def _delayed_first_like(app: Application, plan_id: int, delay: int = 15):
    """Auto Set করার সাথে সাথে (কিছু সেকেন্ড পর) প্রথম দিনের লাইক পাঠায়।
    এটা days_left কমায় এবং credit deduct করে (execute_auto_plan এর মাধ্যমে),
    কিন্তু next_run (পরের দিনের নির্ধারিত সময়) পরিবর্তন করে না —
    ফলে পরবর্তী দিনগুলোর অটো রান স্বাভাবিক run_time অনুযায়ীই চলবে।"""
    try:
        await asyncio.sleep(delay)
        await execute_auto_plan(app, plan_id, advance_next_run=False)
    except Exception as e:
        logging.exception("Delayed first auto-like failed: %s", e)

async def create_auto_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, quality: int):
    if not update.effective_user or not update.effective_chat or not update.message:
        return
    if not await enforce_group_lock(update):
        return

    cmd_name = "100auto" if quality == 100 else "200auto"

    if len(context.args) < 2:
        await update.message.reply_text(f"Usage: /{cmd_name} UID DAYS")
        return

    uid = context.args[0].strip()
    if not uid.isdigit():
        await update.message.reply_text("❌ UID must be numeric")
        return

    try:
        days = int(context.args[1])
        if days < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Days must be a positive number")
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    cost = quality // 100

    user = ensure_user(user_id)
    group = ensure_group(chat_id)

    ok, reason, mode = check_like_access(chat_id, user, group, cost, user_id)
    if not ok:
        await update.message.reply_text(reason)
        return

    # একই UID + একই Quality (100auto বা 200auto) এর জন্য আগে থেকে Active Plan
    # থাকলে সেটার সাথে Days যোগ হবে। কিন্তু UID সেম হলেও Quality আলাদা হলে
    # (যেমন একটায় 100auto, আরেকটায় 200auto) — সেগুলো আলাদা Plan হিসেবেই বসবে।
    existing_id = next(
        (pid for pid, p in autoplans.items() if p["uid"] == uid and p["quality"] == quality),
        None
    )

    if existing_id is not None:
        plan = autoplans[existing_id]
        plan["days_left"] += days
        plan["mode"] = mode
        plan["by_name"] = html_escape(update.effective_user.first_name or "Unknown")
        run_time = plan["run_time"]
        save_data()

        await update.message.reply_text(
            "<pre>"
            f"{quality} AUTO UPDATED\n\n"
            f"UID       : {uid}\n"
            f"Added Days: {days}\n"
            f"Total Days: {plan['days_left']}\n"
            f"By        : {plan['by_name']}\n"
            f"Run Time  : {run_time} (Asia/Dhaka)"
            "</pre>",
            parse_mode=ParseMode.HTML
        )
        return

    plan_id = _new_plan_id()
    run_time = DEFAULT_AUTO_TIME
    by_name = html_escape(update.effective_user.first_name or "Unknown")
    autoplans[plan_id] = {
        "id": plan_id,
        "user_id": user_id,
        "chat_id": chat_id,
        "uid": uid,
        "quality": quality,
        "cost": cost,
        "days_left": days,
        "run_time": run_time,
        "next_run": _next_run_dt(run_time),
        "mode": mode,
        "by_name": by_name,
        "ok_count": 0,
        "fail_count": 0,
    }
    save_data()

    # Auto Set করার সাথে সাথে (কিছুক্ষণ পর) প্রথম দিনের লাইক অটো পাঠানো হবে
    asyncio.create_task(_delayed_first_like(context.application, plan_id))

    await update.message.reply_text(
        "<pre>"
        f"{quality} AUTO SET\n\n"
        f"UID  : {uid}\n"
        f"Days : {days}\n"
        f"By   : {by_name}\n"
        f"Run Time : {run_time} (Asia/Dhaka)"
        "</pre>",
        parse_mode=ParseMode.HTML
    )

async def auto100_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/100auto UID DAYS -> Daily 100 Like"""
    await create_auto_plan(update, context, 100)

async def auto200_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/200auto UID DAYS and /autolike UID DAYS -> Daily 200 Like"""
    await create_auto_plan(update, context, 200)

async def settime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/settime HH:MM UID -> change daily run time of an auto plan"""
    if not update.effective_user or not update.message:
        return
    if not await enforce_group_lock(update):
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /settime HH:MM UID")
        return

    time_str = context.args[0].strip()
    uid = context.args[1].strip()

    try:
        hh, mm = time_str.split(":")
        hh, mm = int(hh), int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ সময়ের ফরম্যাট ভুল। Usage: /settime HH:MM UID (24hr)")
        return

    user_id = update.effective_user.id
    found = None
    for plan in autoplans.values():
        if plan["user_id"] == user_id and plan["uid"] == uid:
            found = plan
            break

    if not found:
        await update.message.reply_text(f"❌ আপনার UID {uid} এর কোনো Auto Like Plan পাওয়া যায়নি")
        return

    run_time = f"{hh:02d}:{mm:02d}"
    found["run_time"] = run_time
    found["next_run"] = _next_run_dt(run_time)
    save_data()

    await update.message.reply_text(f"✅ Run Time পরিবর্তন হয়েছে — UID {uid} => {run_time} (Asia/Dhaka)")

async def autostats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/autostats -> list ALL active auto-like plans, no matter where the
    command is used (Bot DM or any Group) — all auto plans are one unified list."""
    if not update.effective_user or not update.message:
        return
    if not await enforce_group_lock(update):
        return

    all_plans = list(autoplans.values())

    if not all_plans:
        await update.message.reply_text("কোনো Active Auto Like Plan নেই")
        return

    text = "<pre>"
    text += "╭─────────────────────────╮\n"
    text += "│ 🤖 AUTO LIKE PLANS\n"
    for p in all_plans:
        text += (
            "│──────────────────────────\n"
            f"│ Plan ID   : {p['id']}\n"
            f"│ UID       : {p['uid']}\n"
            f"│ Quality   : {p['quality']} Like/day\n"
            f"│ Days Left : {p['days_left']}\n"
            f"│ Run Time  : {p['run_time']}\n"
            f"│ By        : {p.get('by_name', '?')}\n"
            f"│ OK:{p.get('ok_count', 0)} Fail:{p.get('fail_count', 0)}\n"
        )
    text += "╰─────────────────────────╯\n"
    text += "Stop a plan: /stopplan PLAN_ID or /stopplan UID</pre>"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ===== STOP PLAN (user removes their own plan) =====
async def stopplan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stopplan PLAN_ID or UID — user cancels their own auto plan"""
    if not update.message or not update.effective_user:
        return
    if not await enforce_group_lock(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /stopplan PLAN_ID or /stopplan UID")
        return

    user_id = update.effective_user.id
    arg = context.args[0].strip()

    # Try to find by plan ID first, then by UID
    plan = None
    plan_id = None
    try:
        pid = int(arg)
        if pid in autoplans:
            plan = autoplans[pid]
            plan_id = pid
    except ValueError:
        pass

    # If not found by plan ID, search by UID
    if not plan:
        for pid, p in autoplans.items():
            if p["uid"] == arg and (user_id == MAIN_ADMIN or p["user_id"] == user_id):
                plan = p
                plan_id = pid
                break

    if not plan:
        await update.message.reply_text("❌ Plan not found. Check /autostats for your plans.")
        return

    # Admin can cancel any, users only their own
    if user_id != MAIN_ADMIN and plan["user_id"] != user_id:
        await update.message.reply_text("❌ এটা তোমার plan না")
        return

    autoplans.pop(plan_id, None)
    save_data()
    await update.message.reply_text(
        f"<pre>✅ Plan #{plan_id} Cancelled\nUID: {plan['uid']} | {plan['quality']} Like/day\nDays Left: {plan['days_left']}</pre>",
        parse_mode=ParseMode.HTML
    )

# ===== CANCEL PLAN (admin cancels any plan) =====
async def cancelplan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancelplan PLAN_ID or UID — admin cancels any auto plan"""
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != MAIN_ADMIN:
        return
    if not context.args:
        await update.message.reply_text("Usage: /cancelplan PLAN_ID or /cancelplan UID")
        return

    arg = context.args[0].strip()

    plan = None
    plan_id = None
    try:
        pid = int(arg)
        if pid in autoplans:
            plan = autoplans[pid]
            plan_id = pid
    except ValueError:
        pass

    # If not found by plan ID, search by UID
    if not plan:
        for pid, p in autoplans.items():
            if p["uid"] == arg:
                plan = p
                plan_id = pid
                break

    if not plan:
        await update.message.reply_text("❌ Plan not found. Check /autotasks for all plans.")
        return

    autoplans.pop(plan_id, None)
    save_data()
    await update.message.reply_text(
        f"<pre>"
        f"✅ Plan #{plan_id} Cancelled\n"
        f"UID      : {plan['uid']}\n"
        f"Quality  : {plan['quality']} Like/day\n"
        f"By       : {plan.get('by_name', '?')}\n"
        f"Days Left: {plan['days_left']}"
        f"</pre>",
        parse_mode=ParseMode.HTML
    )

# ===== REMOVE USER (admin) =====
async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/removeuser UID — delete user data entirely"""
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != MAIN_ADMIN:
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeuser USER_ID")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID must be a number")
        return

    if uid not in users:
        await update.message.reply_text("❌ User not found")
        return

    data = users.pop(uid)
    # Also cancel all their auto plans
    removed_plans = [pid for pid, p in list(autoplans.items()) if p["user_id"] == uid]
    for pid in removed_plans:
        autoplans.pop(pid, None)
    save_data()

    await update.message.reply_text(
        f"<pre>"
        f"🗑 User Removed\n"
        f"User ID  : {uid}\n"
        f"Was VIP  : {'Yes' if data.get('is_vip') else 'No'}\n"
        f"Limit    : {data.get('limit', 0)}\n"
        f"Plans    : {len(removed_plans)} cancelled"
        f"</pre>",
        parse_mode=ParseMode.HTML
    )

# ===== REMOVE GROUP (admin) =====
async def removegroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/removegroup GID — delete group data entirely"""
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != MAIN_ADMIN:
        return
    if not context.args:
        await update.message.reply_text("Usage: /removegroup GROUP_ID")
        return
    try:
        gid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Group ID must be a number")
        return

    if gid not in groups and gid not in unlocked_groups:
        await update.message.reply_text("❌ Group not found")
        return

    data = groups.pop(gid, {})
    unlocked_groups.discard(gid)
    removed_plans = [pid for pid, p in list(autoplans.items()) if p["chat_id"] == gid]
    for pid in removed_plans:
        autoplans.pop(pid, None)
    save_data()

    await update.message.reply_text(
        f"<pre>"
        f"🗑 Group Removed\n"
        f"Group ID : {gid}\n"
        f"Was VIP  : {'Yes' if data.get('is_vip') else 'No'}\n"
        f"Limit    : {data.get('limit', 0)}\n"
        f"Plans    : {len(removed_plans)} cancelled"
        f"</pre>",
        parse_mode=ParseMode.HTML
    )

# ===== ADD LIMIT BY USER ID (admin, no reply needed) =====
async def addlimit_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addlimit_id USER_ID LIMIT — set limit by user ID directly"""
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != MAIN_ADMIN:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addlimit_id USER_ID LIMIT")
        return
    try:
        uid = int(context.args[0])
        limit = int(context.args[1])
        if limit < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid USER_ID or LIMIT")
        return

    user = ensure_user(uid)
    user["is_vip"] = True
    user["limit"] += limit  # top-up: add to existing balance, don't reset used
    save_data()

    remain = max(user["limit"] - user["used"], 0)
    await update.message.reply_text(
        f"<pre>"
        f"╭─────────────────────────╮\n"
        f"│ ✅ Credit Added!\n"
        f"│ User ID  : {uid}\n"
        f"│ Added    : +{limit}\n"
        f"│ Total    : {user['limit']}\n"
        f"│ Used     : {user['used']}\n"
        f"│ Remaining: {remain}\n"
        f"╰─────────────────────────╯"
        f"</pre>",
        parse_mode=ParseMode.HTML
    )

# ===== AUTO LIKE RUNNER (BACKGROUND SCHEDULER) =====
async def notify_auto(app: Application, plan: dict, text: str):
    """Sends the auto-like result to the group/chat the plan was created in,
    AND as a DM to the user who owns the plan (so it reaches both places)."""
    try:
        await app.bot.send_message(plan["chat_id"], text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.warning("Auto like chat/group notify failed: %s", e)

    if plan["chat_id"] != plan["user_id"]:
        try:
            await app.bot.send_message(plan["user_id"], text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.warning("Auto like user DM notify failed: %s", e)

async def execute_auto_plan(app: Application, plan_id: int, advance_next_run: bool = True):
    plan = autoplans.get(plan_id)
    if not plan:
        return

    user = ensure_user(plan["user_id"])
    group = ensure_group(plan["chat_id"])
    cost = plan["cost"]

    if plan["mode"] == "admin":
        remain_credit = cost  # Admin — unlimited, always enough
    elif plan["mode"] == "user":
        remain_credit = user["limit"] - user["used"]
    elif plan["mode"] == "group":
        remain_credit = group["limit"] - group["used"]
    else:
        # Unknown/free mode — auto plans should never be free, block immediately
        remain_credit = 0

    if remain_credit < cost:
        # Credit শেষ/কম থাকলেও Auto Plan সাথে সাথে বন্ধ হবে না —
        # শুধু আজকের লাইকটা FAILED দেখাবে, Days Left কমবে, এবং
        # Days Left শেষ (0) হলে তবেই প্ল্যান স্বয়ংক্রিয়ভাবে শেষ হবে।
        plan["days_left"] -= 1
        plan["fail_count"] = plan.get("fail_count", 0) + 1

        fail_text = (
            "<pre>"
            f"{plan['quality']} AUTO LIKE FAILED ❌\n"
            f"UID        : {plan['uid']}\n"
            f"Region     : {REGION}\n"
            "Error      : Credit শেষ। Admin এর সাথে যোগাযোগ করে Limit যোগ করুন\n"
            f"Time       : {now_str()}\n"
            f"Days Left  : {plan['days_left']}\n"
            f"Ordered By : {plan.get('by_name', 'Auto')}\n"
            "No limit deducted."
            "</pre>"
        )
        await notify_auto(app, plan, fail_text)

        if plan["days_left"] <= 0:
            autoplans.pop(plan_id, None)
        elif advance_next_run:
            plan["next_run"] = plan["next_run"] + timedelta(days=1)

        save_data()
        return

    ok, added, name, before, after, err = await call_like_api(plan["uid"], plan["quality"])

    MIN_LIKES_FOR_DEDUCTION = 50  # এর কম Like যোগ হলে Credit/Day কাটা হবে না

    if ok and added >= MIN_LIKES_FOR_DEDUCTION:
        if plan["mode"] == "admin":
            remain_after = None  # unlimited
        elif plan["mode"] == "user":
            user["used"] += cost
            remain_after = user["limit"] - user["used"]
        else:
            group["used"] += cost
            remain_after = group["limit"] - group["used"]

        plan["days_left"] -= 1
        plan["ok_count"] = plan.get("ok_count", 0) + 1

        header_line = (
            f'{plan["quality"]} AUTO '
            '<tg-emoji emoji-id="6267161573324757039">👍</tg-emoji> '
            'SENT SUCCESS! '
            '<tg-emoji emoji-id="6291956780301816432">✅</tg-emoji>'
        )
        text = (
            f"{header_line}\n"
            "<pre>"
            f"Name           : {name}\n"
            f"UID            : {plan['uid']}\n"
            f"Region         : {REGION}\n"
            f"Likes Before   : {before}\n"
            f"Likes Added    : +{added}\n"
            f"Likes After    : {after}\n"
            f"Time           : {now_str()}\n"
            f"Days Left      : {plan['days_left']}\n"
            f"Ordered By     : {plan.get('by_name', 'Auto')}\n"
            f"Limit Deducted : {cost} | Now: {'∞' if remain_after is None else float(max(remain_after, 0))}"
            "</pre>\n"
            "✦⃝★ Supported By : Pranto <tg-emoji emoji-id=\"6267097569722111582\">👑</tg-emoji> ✿𓆩"
        )
    elif ok:
        # Like পাঠানো সফল হয়েছে কিন্তু 50 এর কম Like যোগ হয়েছে —
        # তাই Days Left বা Credit কোনোটাই কাটা হবে না, তবে Fail হিসেবে গণনা হবে।
        plan["fail_count"] = plan.get("fail_count", 0) + 1
        text = (
            "<pre>"
            f"{plan['quality']} AUTO LIKE SENT (LOW) ⚠️\n"
            f"Name           : {name}\n"
            f"UID            : {plan['uid']}\n"
            f"Region         : {REGION}\n"
            f"Likes Before   : {before}\n"
            f"Likes Added    : +{added}\n"
            f"Likes After    : {after}\n"
            f"Time           : {now_str()}\n"
            f"Reason         : {MIN_LIKES_FOR_DEDUCTION} এর কম Like যোগ হয়েছে\n"
            f"Days Left      : {plan['days_left']} (অপরিবর্তিত)\n"
            f"Ordered By     : {plan.get('by_name', 'Auto')}\n"
            "No limit deducted."
            "</pre>"
        )
    else:
        plan["fail_count"] = plan.get("fail_count", 0) + 1
        text = (
            "<pre>"
            f"{plan['quality']} AUTO LIKE FAILED ❌\n"
            f"UID    : {plan['uid']}\n"
            f"Region : {REGION}\n"
            f"Error  : {err}\n"
            f"Time   : {now_str()}\n"
            "No limit deducted."
            "</pre>"
        )

    await notify_auto(app, plan, text)

    if not ok:
        # API-level fail (network error, "already claimed today" ইত্যাদি) —
        # Days Left/Credit কাটবে না, কিন্তু next_run পরের দিনে এগিয়ে দিতে হবে,
        # নাহলে auto_like_runner প্রতি ৬০ সেকেন্ডে বারবার একই Fail পাঠাতে থাকবে (spam bug)।
        if advance_next_run:
            plan["next_run"] = plan["next_run"] + timedelta(days=1)
        save_data()
        return

    if plan["days_left"] <= 0:
        autoplans.pop(plan_id, None)
    elif advance_next_run:
        plan["next_run"] = plan["next_run"] + timedelta(days=1)

    save_data()

async def auto_like_runner(app: Application):
    while True:
        try:
            now = datetime.now(ZoneInfo("Asia/Dhaka"))
            for plan_id in list(autoplans.keys()):
                plan = autoplans.get(plan_id)
                if plan and now >= plan["next_run"]:
                    await execute_auto_plan(app, plan_id)
        except Exception as e:
            logging.exception("Auto like runner error: %s", e)

        await asyncio.sleep(60)

# ===== STARTUP =====
async def post_init(app: Application):
    asyncio.create_task(auto_like_runner(app))

    # Free host-এ deploy/restart হলে সাথে সাথে Admin কে জানিয়ে দেবে
    try:
        await app.bot.send_message(MAIN_ADMIN, "✅ Bot started successfully.")
    except Exception as e:
        logging.warning("Startup notify to admin failed: %s", e)

# ===== FLASK HEALTH-CHECK SERVER (keeps Render free Web Service alive) =====
flask_app = Flask(__name__)

@flask_app.route("/")
def health_check():
    return "Bot is running", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# ===== MAIN =====
def main():
    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN environment variable is not set. Add it in your "
            "hosting provider's Environment Variables settings."
        )

    if not MONGO_URI:
        raise ValueError(
            "MONGO_URI environment variable is not set. Add it in your "
            "hosting platform's Environment Variables, e.g.: "
            "mongodb+srv://user:pass@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority"
        )

    # Explicitly create and set an event loop for the main thread.
    # Newer Python versions (3.12+) no longer implicitly create one,
    # which breaks python-telegram-bot's internal asyncio.get_event_loop() call.
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Start
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("adminhelp", adminhelp_cmd))

    # Like commands
    app.add_handler(CommandHandler(["like", "100like"], like_cmd))
    app.add_handler(CommandHandler(["likes", "200like"], likes_cmd))
    app.add_handler(CommandHandler("fflike", fflike_cmd))
    app.add_handler(CallbackQueryHandler(fflike_callback, pattern=r"^fflike\|"))

    # Auto like commands
    app.add_handler(CommandHandler("100auto", auto100_cmd))
    app.add_handler(CommandHandler(["200auto", "autolike"], auto200_cmd))
    app.add_handler(CommandHandler("settime", settime_cmd))
    app.add_handler(CommandHandler("autostats", autostats_cmd))
    app.add_handler(CommandHandler("stopplan", stopplan_cmd))

    # Account
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("remain", remain))

    # Admin commands
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("vipusers", vipusers))
    app.add_handler(CommandHandler("addlimit", addlimit))
    app.add_handler(CommandHandler("addlimit_id", addlimit_id_cmd))
    app.add_handler(CommandHandler("removelimit", removelimit))
    app.add_handler(CommandHandler("removeuser", removeuser_cmd))
    app.add_handler(CommandHandler("removegroup", removegroup_cmd))
    app.add_handler(CommandHandler("cancelplan", cancelplan_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("autotasks", autotasks_cmd))

    # Start Flask health-check server in background thread so Render
    # (or any host expecting a web service) sees an open port.
    threading.Thread(target=run_flask, daemon=True).start()

    logging.info("Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
