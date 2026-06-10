import os, asyncio, logging, time, sys, shutil, zipfile, re, secrets, base64
from datetime import datetime, timezone
from asyncio import create_subprocess_exec
from asyncio.subprocess import PIPE

import psutil
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes,
    filters
)

# ─────────────────────────────────────────────────────────────
# Live progress animation (classic style, nonstop until done)
# ─────────────────────────────────────────────────────────────

_PROGRESS_BAR_WIDTH = 20
_PROGRESS_EDIT_INTERVAL = 2.0  # Telegram rate-limit friendly


def _progress_fmt_time(secs: float) -> str:
    s = int(secs)
    m, s = divmod(s, 60)
    return f"{m:02d}:{s:02d}"


def _progress_bar(pct: int, frame_idx: int, width: int = _PROGRESS_BAR_WIDTH) -> str:
    filled = int(width * pct / 100)
    remaining = width - filled
    bar_chars = ["█"] * filled + ["░"] * remaining
    if remaining > 0 and pct < 100:
        pulse_pos = filled + (frame_idx % remaining)
        bar_chars[pulse_pos] = "▒"
    return "[" + "".join(bar_chars) + f"] {pct}%"


class LiveProgress:
    """
    Live classic progress bar that edits a Telegram message every ~2 seconds.
    Bar fills based on time-estimate (pip/npm don't expose real progress) and
    a moving "pulse" character keeps it visibly alive. Caps at 95% until
    .stop(success=True) is called, which jumps it to 100%.
    """

    def __init__(self, message, title: str = "Working"):
        self.message = message
        self.title = title
        self._running = False
        self._task = None
        self._start_ts = 0.0
        self._estimated = 60.0
        self._last_text = ""

    def _render(self, pct: int, frame_idx: int, elapsed: float, status: str) -> str:
        bar = _progress_bar(pct, frame_idx)
        return (
            f"⚙️ *{self.title}*\n\n"
            f"⏳ {status}\n"
            f"`{bar}`\n"
            f"⏱ {_progress_fmt_time(elapsed)}"
        )

    async def _safe_edit(self, text: str):
        if text == self._last_text:
            return
        self._last_text = text
        try:
            await self.message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

    async def start(self, status: str = "Starting..."):
        self._start_ts = time.time()
        await self._safe_edit(self._render(0, 0, 0.0, status))

    async def animate(self, estimated_seconds: float = 60.0, status: str = "Working..."):
        self._running = True
        self._estimated = max(5.0, estimated_seconds)
        self._start_ts = time.time()
        frame = 0
        try:
            while self._running:
                elapsed = time.time() - self._start_ts
                pct = min(95, int(elapsed / self._estimated * 100))
                await self._safe_edit(self._render(pct, frame, elapsed, status))
                frame += 1
                await asyncio.sleep(_PROGRESS_EDIT_INTERVAL)
        except asyncio.CancelledError:
            pass

    def run_in_background(self, estimated_seconds: float = 60.0, status: str = "Working..."):
        self._task = asyncio.create_task(self.animate(estimated_seconds, status))
        return self._task

    async def stop(self, success: bool = True, final_text: str = "Done"):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
        elapsed = time.time() - self._start_ts
        if success:
            bar = _progress_bar(100, 0)
            text = (
                f"✅ *{self.title}*\n\n"
                f"{final_text}\n"
                f"`{bar}`\n"
                f"⏱ {_progress_fmt_time(elapsed)}"
            )
        else:
            text = (
                f"❌ *{self.title} — Failed*\n\n"
                f"{final_text}\n"
                f"⏱ {_progress_fmt_time(elapsed)}"
            )
        self._last_text = ""
        await self._safe_edit(text)

# ─────────────────────────────────────────────────────────────
# Bootstrap
# ─────────────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
OWNER_ID        = int(os.getenv("OWNER_ID", "0"))
OWNER_USERNAME  = os.getenv("OWNER_USERNAME", "owner")
MONGODB_URI     = os.getenv("MONGODB_URI", "")
DATABASE_NAME   = os.getenv("DATABASE_NAME", "god_madara_hosting")
BASE_URL        = os.getenv("BASE_URL", "http://localhost:8080")
PORT            = int(os.getenv("PORT", "8080"))

# Primary DB (mandatory)
mongo_client = AsyncIOMotorClient(MONGODB_URI)
db           = mongo_client[DATABASE_NAME]
users_col    = db["users"]
projects_col = db["projects"]
tokens_col   = db["file_tokens"]
backups_col  = db["backups"]

# ─────────────────────────────────────────────────────────────
# 🗄️  Multiple Extra Databases (UNLIMITED)
# Just add to .env:
#   MONGODB_URI_1, DATABASE_NAME_1
#   MONGODB_URI_2, DATABASE_NAME_2
#   MONGODB_URI_3, DATABASE_NAME_3   ... and so on, no limit
# ─────────────────────────────────────────────────────────────
extra_clients = []          # list of AsyncIOMotorClient
extra_dbs     = []          # list of dicts: {"name": ..., "db": ..., "client": ...}

def _load_extra_databases():
    """Scan env for MONGODB_URI_N + DATABASE_NAME_N pairs and connect all."""
    seen_names = set()

    # Backward-compat: legacy MONGODB_URI_2 / DATABASE_NAME_2
    legacy_uri  = os.getenv("MONGODB_URI_2", "")
    legacy_name = os.getenv("DATABASE_NAME_2", "")
    if legacy_uri and legacy_name and legacy_name not in seen_names:
        try:
            client = AsyncIOMotorClient(legacy_uri)
            extra_clients.append(client)
            extra_dbs.append({"name": legacy_name, "db": client[legacy_name], "client": client})
            seen_names.add(legacy_name)
            logging.getLogger(__name__).info(f"✅ Extra DB connected (legacy): {legacy_name}")
        except Exception as e:
            logging.getLogger(__name__).error(f"❌ Failed to connect legacy DB: {e}")

    # Numbered DBs — scan up to a high index so gaps are tolerated
    for i in range(1, 51):  # supports up to 50 extra DBs
        uri  = os.getenv(f"MONGODB_URI_{i}", "")
        name = os.getenv(f"DATABASE_NAME_{i}", "")
        if not uri or not name or name in seen_names:
            continue
        try:
            client = AsyncIOMotorClient(uri)
            extra_clients.append(client)
            extra_dbs.append({"name": name, "db": client[name], "client": client})
            seen_names.add(name)
            logging.getLogger(__name__).info(f"✅ Extra DB #{i} connected: {name}")
        except Exception as e:
            logging.getLogger(__name__).error(f"❌ Failed to connect DB #{i} ({name}): {e}")

_load_extra_databases()
logging.getLogger(__name__).info(f"📊 Total extra databases connected: {len(extra_dbs)}")

# Backward-compat shims — purana code bina change ke chalta rahega
db_2 = extra_dbs[0]["db"] if extra_dbs else None
mongo_client_2 = extra_clients[0] if extra_clients else None
MONGODB_URI_2 = os.getenv("MONGODB_URI_2", "")
DATABASE_NAME_2 = os.getenv("DATABASE_NAME_2", "")

def get_extra_db_by_name(name: str):
    """Naam se koi bhi extra DB access karo."""
    for entry in extra_dbs:
        if entry["name"] == name:
            return entry["db"]
    return None

def list_extra_db_names() -> list:
    """Saare connected extra DBs ke naam wapas karta hai."""
    return [e["name"] for e in extra_dbs]

# ─────────────────────────────────────────────────────────────
# 🎯 SHARDING — Storage Distribution Logic
# Har project ka backup sirf 1 DB mein jata hai (hash-based).
# Same project hamesha same DB pe land karega → predictable.
# Total storage = sum of all DBs (multiplies your free tier!)
# ─────────────────────────────────────────────────────────────

def all_backup_cols() -> list:
    """Primary + saare extra DBs ki backup collections ki list."""
    return [backups_col] + [e["db"]["backups"] for e in extra_dbs]

def all_db_names() -> list:
    """Primary + extra DB names (collection list ke same order mein)."""
    return [DATABASE_NAME] + [e["name"] for e in extra_dbs]

def pick_backup_col(user_id: int, project_name: str):
    """
    Hash-based sharding: ek project hamesha same DB pe jata hai.
    Returns: (db_name, backup_collection)
    """
    cols  = all_backup_cols()
    names = all_db_names()
    if len(cols) == 1:
        return (names[0], cols[0])
    # Stable hash — Python's hash() randomizes per process, so use a
    # deterministic one based on string bytes.
    import hashlib
    key = f"{user_id}:{project_name}".encode("utf-8")
    h = int(hashlib.md5(key).hexdigest(), 16)
    idx = h % len(cols)
    return (names[idx], cols[idx])

BOT_START_TIME = time.time()

# Global bot reference for notifications (set in post_init)
notification_bot = None

# ─────────────────────────────────────────────────────────────
# Conversation states
# ─────────────────────────────────────────────────────────────
(
    NEW_PROJECT_NAME,
    NEW_PROJECT_FILES,
    EDIT_RUN_CMD,
    ADMIN_GIVE_PREMIUM_ID,
    ADMIN_REMOVE_PREMIUM_ID,
    ADMIN_TEMP_PREMIUM_ID,
    ADMIN_TEMP_PREMIUM_DUR,
    ADMIN_BAN_ID,
    ADMIN_UNBAN_ID,
    ADMIN_BROADCAST_MSG,
    ADMIN_SEND_USER_ID,
    ADMIN_SEND_USER_MSG,
    ENV_ADD_KEY,
    ENV_ADD_VALUE,
    ENV_EDIT_VALUE,
    ADMIN_ADD_ADMIN_ID,
    ADMIN_REMOVE_ADMIN_ID,
) = range(17)

FREE_LIMIT    = 1
PREMIUM_LIMIT = 9999  # Premium = effectively unlimited

PROJECTS_ROOT = os.path.join(os.path.dirname(__file__), "projects")
os.makedirs(PROJECTS_ROOT, exist_ok=True)

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def project_dir(user_id: int, project_name: str) -> str:
    return os.path.join(PROJECTS_ROOT, str(user_id), project_name)

def fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m}m {sec}s" if h else (f"{m}m {sec}s" if m else f"{sec}s")

def fmt_duration(total_seconds: float) -> str:
    return fmt_uptime(total_seconds)

async def safe_edit(query, text: str, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        logger.warning(f"safe_edit BadRequest: {e}")
        # Retry without parse_mode as fallback
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"safe_edit error: {e}")

async def ensure_user(user):
    """Upsert user document."""
    await users_col.update_one(
        {"user_id": user.id},
        {"$setOnInsert": {
            "user_id":       user.id,
            "username":      user.username or "",
            "first_name":    user.first_name or "",
            "is_premium":    False,
            "premium_expiry": None,
            "is_banned":     False,
            "is_admin":      False,
            "joined_date":   datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    await users_col.update_one(
        {"user_id": user.id},
        {"$set": {
            "username":   user.username or "",
            "first_name": user.first_name or "",
        }},
    )

async def check_premium_expiry(user_id: int):
    """Strip premium if expired."""
    doc = await users_col.find_one({"user_id": user_id})
    if doc and doc.get("premium_expiry"):
        expiry = doc["premium_expiry"]
        # MongoDB returns naive datetimes — treat them as UTC so the
        # comparison below doesn't blow up with a TypeError.
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry < datetime.now(timezone.utc):
            await users_col.update_one(
                {"user_id": user_id},
                {"$set": {"is_premium": False, "premium_expiry": None}},
            )

async def get_user(user_id: int):
    return await users_col.find_one({"user_id": user_id})

async def is_banned(user_id: int) -> bool:
    doc = await get_user(user_id)
    return bool(doc and doc.get("is_banned"))

async def is_premium(user_id: int) -> bool:
    await check_premium_expiry(user_id)
    doc = await get_user(user_id)
    return bool(doc and doc.get("is_premium"))

async def is_admin(user_id: int) -> bool:
    doc = await get_user(user_id)
    return bool(doc and doc.get("is_admin"))

async def is_owner_or_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or await is_admin(user_id)

def owner_only(func):
    import functools
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if uid != OWNER_ID:
            if update.callback_query:
                await update.callback_query.answer("⛔ Owner only", show_alert=True)
            return
        return await func(update, context)
    return wrapper

def admin_or_owner(func):
    import functools
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not await is_owner_or_admin(uid):
            if update.callback_query:
                await update.callback_query.answer("⛔ Admin only", show_alert=True)
            return
        return await func(update, context)
    return wrapper

async def project_count(user_id: int) -> int:
    return await projects_col.count_documents({"user_id": user_id})

async def get_project(user_id: int, name: str):
    return await projects_col.find_one({"user_id": user_id, "name": name})

async def running_project_count() -> int:
    return await projects_col.count_documents({"status": "running"})

async def check_ban_and_premium(update: Update):
    """Return (is_banned, doc). Updates premium expiry."""
    user = update.effective_user
    await ensure_user(user)
    await check_premium_expiry(user.id)
    banned = await is_banned(user.id)
    return banned

# ─────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)
    await check_premium_expiry(user.id)

    if await is_banned(user.id):
        await update.message.reply_text("🚫 You are banned. Contact owner.", parse_mode=ParseMode.MARKDOWN)
        return

    doc      = await get_user(user.id)
    premium  = doc.get("is_premium", False)
    count    = await project_count(user.id)
    plan_lbl = "Premium ✨" if premium else "Free"
    limit_lbl = "∞" if premium else str(FREE_LIMIT)

    text = (
        f"🌟 *Welcome to God Hosting Bot!*\n\n"
        f"👋 Hello {user.first_name}!\n\n"
        f"🚀 *What I can do:*\n"
        f"• Host python / node.js projects 24/7\n"
        f"• Web File Manager — Edit files in browser\n"
        f"• Auto-install requirements.txt/package.json\n"
        f"• Real-time logs & monitoring\n"
        f"• Free: 1 project | Premium: Unlimited\n\n"
        f"📊 *Your Status:*\n"
        f"👤 ID: `{user.id}`\n"
        f"💎 Plan: {plan_lbl}\n"
        f"📁 Projects: {count}/{limit_lbl}\n\n"
        f"Choose an option below:"
    )

    kb = [
        [
            InlineKeyboardButton("🆕 New Project",   callback_data="new_project"),
            InlineKeyboardButton("📂 My Projects",   callback_data="my_projects"),
        ],
        [
            InlineKeyboardButton("💎 Premium",        callback_data="premium"),
            InlineKeyboardButton("📊 My Status",      callback_data="my_status"),
        ],
    ]
    if user.id == OWNER_ID or await is_admin(user.id):
        kb.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# Back-to-start helper via callback
# ─────────────────────────────────────────────────────────────

async def cb_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    await ensure_user(user)
    await check_premium_expiry(user.id)

    if await is_banned(user.id):
        await safe_edit(query, "🚫 You are banned. Contact owner.", parse_mode=ParseMode.MARKDOWN)
        return

    doc      = await get_user(user.id)
    premium  = doc.get("is_premium", False)
    count    = await project_count(user.id)
    plan_lbl = "Premium ✨" if premium else "Free"
    limit_lbl = "∞" if premium else str(FREE_LIMIT)

    text = (
        f"🌟 *Welcome to God Madara Hosting Bot!*\n\n"
        f"👋 Hello {user.first_name}!\n\n"
        f"🚀 *What I can do:*\n"
        f"• Host python / node.js projects\n"
        f"• Web File Manager — Edit files in browser\n"
        f"• Auto-install requirements.txt/package.json\n"
        f"• Real-time logs & monitoring\n"
        f"• Free: 1 project | Premium: Unlimited\n\n"
        f"📊 *Your Status:*\n"
        f"👤 ID: `{user.id}`\n"
        f"💎 Plan: {plan_lbl}\n"
        f"📁 Projects: {count}/{limit_lbl}\n\n"
        f"Choose an option below:"
    )

    kb = [
        [
            InlineKeyboardButton("🆕 New Project",   callback_data="new_project"),
            InlineKeyboardButton("📂 My Projects",   callback_data="my_projects"),
        ],
        [
            InlineKeyboardButton("💎 Premium",        callback_data="premium"),
            InlineKeyboardButton("📊 My Status",      callback_data="my_status"),
        ],
    ]
    if user.id == OWNER_ID or await is_admin(user.id):
        kb.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])

    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# 📊 Bot Status — Sirf Owner Dekh Sakta Hai (Admin Panel se)
# ─────────────────────────────────────────────────────────────

@admin_or_owner
async def cb_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        # Measure DB ping safely
        db_ping = 0
        try:
            t0 = time.time()
            await db.command("ping")
            db_ping = int((time.time() - t0) * 1000)
        except Exception:
            db_ping = -1

        # Measure bot API ping safely
        api_ping = 0
        try:
            t1 = time.time()
            await context.bot.get_me()
            api_ping = int((time.time() - t1) * 1000)
        except Exception:
            api_ping = -1

        total_users = await users_col.count_documents({})
        premium_users = await users_col.count_documents({"is_premium": True})
        total_proj = await projects_col.count_documents({})
        running_proj = await running_project_count()

        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        uptime = fmt_uptime(time.time() - BOT_START_TIME)
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

        # Backup info
        backup_line = "💾 Last Backup: `Never`\n"
        try:
            meta = await backups_col.find_one({"type": "backup_meta"})
            if meta:
                backup_time = meta["backed_up_at"].strftime("%Y-%m-%d %H:%M UTC")
                backup_size = fmt_bytes(meta.get("total_size", 0))
                backup_files = meta.get("total_files", 0)
                backup_line = (
                    f"💾 Last Backup: `{backup_time}`\n"
                    f"📦 Backup: `{backup_files}` files, `{backup_size}`\n"
                )
        except Exception:
            pass

        # Multi-DB status — ping + storage usage per DB (sharded data)
        extra_db_lines = ""
        extra_online = 0
        per_db_stats = []  # list of (name, online, project_count)

        # Primary DB stats
        try:
            primary_proj_count = await backups_col.count_documents({"type": "file_backup"})
        except Exception:
            primary_proj_count = 0
        per_db_stats.append((DATABASE_NAME, db_ping >= 0, primary_proj_count))

        for entry in extra_dbs:
            online = False
            count = 0
            try:
                await entry["db"].command("ping")
                online = True
                extra_online += 1
                try:
                    count = await entry["db"]["backups"].count_documents({"type": "file_backup"})
                except Exception:
                    count = 0
            except Exception:
                pass
            per_db_stats.append((entry["name"], online, count))

        total_dbs = 1 + len(extra_dbs)  # primary + extras
        total_online = (1 if db_ping >= 0 else 0) + extra_online

        # Storage distribution display
        if extra_dbs:
            extra_db_lines = f"\n*🗄 Storage Distribution:*\n"
            for name, online, count in per_db_stats:
                icon = "🟢" if online else "🔴"
                extra_db_lines += f"   {icon} `{name}`: `{count}` projects\n"

        # Format ping display
        db_ping_str = f"{db_ping}ms" if db_ping >= 0 else "Error"
        api_ping_str = f"{api_ping}ms" if api_ping >= 0 else "Error"

        text = (
            f"📊 *Bot Dashboard*\n\n"
            f"👥 Total Users: `{total_users}`\n"
            f"💎 Premium Users: `{premium_users}`\n"
            f"📁 Total Projects: `{total_proj}`\n"
            f"🟢 Running Projects: `{running_proj}`\n"
            f"💾 Database: MongoDB ✅\n"
            f"🔗 Connected DBs: `{total_online}/{total_dbs}` "
            f"(1 primary + {len(extra_dbs)} extra)\n"
            f"{extra_db_lines}"
            f"🐍 Python: `{py_ver}`\n\n"
            f"💻 *System:*\n"
            f"├ CPU: `{cpu}%`\n"
            f"├ RAM: `{fmt_bytes(ram.used)}/{fmt_bytes(ram.total)}` (`{ram.percent}%`)\n"
            f"└ Disk: `{fmt_bytes(disk.used)}/{fmt_bytes(disk.total)}` (`{disk.percent}%`)\n\n"
            f"🏓 Bot Ping: `{api_ping_str}`\n"
            f"💾 DB Ping: `{db_ping_str}`\n"
            f"⏰ Uptime: `{uptime}`\n\n"
            f"*Backup Status:*\n"
            f"{backup_line}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔃 Refresh", callback_data="bot_status"),
             InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")],
        ])
        await safe_edit(query, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"bot_status error: {e}")
        await safe_edit(
            query,
            f"📊 *Bot Dashboard*\n\n⚠️ Error loading stats: {str(e)[:200]}\n\nBot is online and working!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔃 Retry", callback_data="bot_status"),
                 InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")],
            ]),
            parse_mode=ParseMode.MARKDOWN,
        )

# ─────────────────────────────────────────────────────────────
# 💎 Premium page
# ─────────────────────────────────────────────────────────────

async def cb_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    premium = await is_premium(uid)

    features = (
        f"*Free Plan:*\n"
        f"• 1 Project only\n"
        f"• File Manager (10 min)\n\n"
        f"*Premium Plan:*\n"
        f"• ✅ Unlimited projects\n"
        f"• ✅ Priority support\n"
        f"• ✅ Extended file manager\n"
        f"• ✅ Advanced monitoring\n\n"
    )

    if premium:
        text = (
            f"💎 *Premium Membership*\n\n"
            f"✨ *You are Premium!* ✨\n\n"
            + features +
            f"🌟 Premium is active!"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_start")]])
    else:
        text = (
            f"💎 *Premium Membership*\n\n"
            + features +
            f"To get Premium, contact the owner!"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📩 Contact Owner", url=f"https://t.me/{OWNER_USERNAME}")],
            [InlineKeyboardButton("🔙 Back",          callback_data="back_start")],
        ])

    await safe_edit(query, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# 📂 My Projects
# ─────────────────────────────────────────────────────────────

async def cb_my_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    projects = await projects_col.find({"user_id": uid}).to_list(length=100)
    if not projects:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_start")]])
        await safe_edit(query, "📂 *My Projects*\n\nYou have no projects yet.", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    kb_rows = []
    for p in projects:
        icon = "🟢" if p.get("status") == "running" else "🔴"
        kb_rows.append([InlineKeyboardButton(f"{icon} {p['name']}", callback_data=f"proj:{p['name']}")])
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="back_start")])

    await safe_edit(query, "📂 *My Projects*\n\nSelect a project:", reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# 📊 My Status — User ke saare projects ka detailed status
# ─────────────────────────────────────────────────────────────

async def cb_my_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    projects = await projects_col.find({"user_id": uid}).to_list(length=100)
    doc      = await get_user(uid)
    premium  = doc.get("is_premium", False)
    count    = len(projects)
    limit_lbl = "∞" if premium else str(FREE_LIMIT)
    plan_lbl  = "💎 Premium" if premium else "🆓 Free"

    if not projects:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🆕 New Project", callback_data="new_project"),
                                    InlineKeyboardButton("🔙 Back", callback_data="back_start")]])
        await safe_edit(
            query,
            f"📊 *My Status*\n\n{plan_lbl} | 📁 0/{limit_lbl} projects\n\nKoi project nahi hai abhi.\nPehle ek project banao!",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [f"📊 *My Projects Status*\n"]
    lines.append(f"{plan_lbl}  •  📁 {count}/{limit_lbl} projects\n")

    for i, p in enumerate(projects, 1):
        name   = p.get("name", "?")
        status = p.get("status", "stopped")
        cmd    = p.get("run_command") or "Not set"
        ar     = p.get("auto_restart", True)

        # Uptime calculate
        uptime_str = "—"
        if status == "running" and p.get("started_at"):
            try:
                started = p["started_at"]
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                secs = (datetime.now(timezone.utc) - started).total_seconds()
                uptime_str = fmt_uptime(max(0, secs))
            except Exception:
                uptime_str = "—"

        # Exit code
        exit_code = p.get("exit_code")

        if status == "running":
            status_line = f"🟢 Running"
            extra_line  = f"   ├ ⏱ Uptime: `{uptime_str}`"
        elif exit_code is not None and exit_code != 0:
            status_line = f"🔴 Crashed"
            extra_line  = f"   ├ ⚠️ Exit Code: `{exit_code}`"
        else:
            status_line = f"🔴 Stopped"
            extra_line  = f"   ├ ⏱ Uptime: `—`"

        ar_line = "ON ✅" if ar else "OFF ❌"

        lines.append(
            f"{i}️⃣  *{escape_md(name)}*\n"
            f"   ├ {status_line}\n"
            f"{extra_line}\n"
            f"   ├ 🔁 Auto\\-Restart: {ar_line}\n"
            f"   └ 🖥 `{escape_md(cmd)}`\n"
        )

    text = "\n".join(lines)

    # Telegram 4096 char limit
    if len(text) > 3800:
        text = text[:3800] + "\n\n_...aur projects hain, /start se dekho_"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔃 Refresh",    callback_data="my_status"),
         InlineKeyboardButton("📂 Projects",   callback_data="my_projects")],
        [InlineKeyboardButton("🔙 Back",       callback_data="back_start")],
    ])
    await safe_edit(query, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# Project Dashboard
# ─────────────────────────────────────────────────────────────

def escape_md(text: str) -> str:
    """Escape Markdown v1 special characters."""
    for ch in ('_', '*', '`', '['):
        text = str(text).replace(ch, f'\\{ch}')
    return text

def project_dashboard_text(p: dict) -> str:
    status  = p.get("status", "stopped")
    icon    = "🟢 Running" if status == "running" else "🔴 Stopped"
    pid     = str(p.get("pid")) if p.get("pid") else "N/A"
    uptime  = "N/A"
    if status == "running" and p.get("started_at"):
        try:
            started = p["started_at"]
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            uptime  = fmt_uptime(max(0, elapsed))
        except Exception:
            uptime = "N/A"
    last_run = "Never"
    if p.get("last_run"):
        try:
            last_run = p["last_run"].strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            last_run = str(p["last_run"])
    exit_code = str(p.get("exit_code")) if p.get("exit_code") is not None else "None"
    run_cmd   = p.get("run_command") or "Not set"
    created   = "N/A"
    if p.get("created_date"):
        try:
            created = p["created_date"].strftime("%Y-%m-%d")
        except Exception:
            created = str(p["created_date"])

    ar_status = "✅ ON" if p.get("auto_restart", True) else "❌ OFF"

    return (
        f"📊 Project: *{p['name']}*\n\n"
        f"🔹 Status: {icon}\n"
        f"🔹 PID: `{pid}`\n"
        f"🔹 Uptime: `{uptime}`\n"
        f"🔹 Last Run: `{last_run}`\n"
        f"🔹 Exit Code: `{exit_code}`\n"
        f"🔹 Run Command: `{run_cmd}`\n"
        f"🔹 Auto-Restart: {ar_status}\n"
        f"📅 Created: `{created}`"
    )

def project_dashboard_kb(user_id: int, project_name: str, auto_restart: bool = True, is_running: bool = False) -> InlineKeyboardMarkup:
    pn = project_name
    ar_label = "⏰ Auto-Restart: ✅" if auto_restart else "⏰ Auto-Restart: ❌"

    if is_running:
        row1 = [
            InlineKeyboardButton("⏹ Stop",      callback_data=f"stop:{pn}"),
            InlineKeyboardButton("🔄 Restart",   callback_data=f"restart:{pn}"),
            InlineKeyboardButton("📋 Logs",      callback_data=f"logs:{pn}"),
        ]
    else:
        row1 = [
            InlineKeyboardButton("▶️ Run",       callback_data=f"run:{pn}"),
            InlineKeyboardButton("🔄 Restart",   callback_data=f"restart:{pn}"),
            InlineKeyboardButton("📋 Logs",      callback_data=f"logs:{pn}"),
        ]

    return InlineKeyboardMarkup([
        row1,
        [
            InlineKeyboardButton("🔃 Refresh",   callback_data=f"proj:{pn}"),
            InlineKeyboardButton("✏️ Edit CMD",  callback_data=f"editcmd:{pn}"),
            InlineKeyboardButton("📁 Files",     callback_data=f"filemgr:{pn}"),
        ],
        [
            InlineKeyboardButton(ar_label,        callback_data=f"toggle_ar:{pn}"),
            InlineKeyboardButton("🔐 Env Vars",  callback_data=f"envvars:{pn}"),
        ],
        [
            InlineKeyboardButton("📦 Reinstall Requirements", callback_data=f"reinstall_reqs:{pn}"),
        ],
        [
            InlineKeyboardButton("🗑 Delete",    callback_data=f"delete:{pn}"),
            InlineKeyboardButton("🔙 Back",      callback_data="my_projects"),
        ],
    ])

async def cb_project_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    p = await get_project(uid, name)
    if not p:
        await safe_edit(query, "❌ Project not found.", parse_mode=ParseMode.MARKDOWN)
        return

    await safe_edit(query, project_dashboard_text(p), reply_markup=project_dashboard_kb(uid, name, p.get("auto_restart", True), p.get("status") == "running"), parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# 📦 Reinstall Requirements
# ─────────────────────────────────────────────────────────────

async def cb_reinstall_reqs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    p = await get_project(uid, name)
    if not p:
        await safe_edit(query, "❌ Project not found.", parse_mode=ParseMode.MARKDOWN)
        return

    pdir     = project_dir(uid, name)
    req_path = os.path.join(pdir, "requirements.txt")
    pkg_json = os.path.join(pdir, "package.json")
    venv_dir = os.path.join(pdir, "venv")
    pip_path = os.path.join(venv_dir, "bin", "pip")

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"proj:{name}")]])

    # Node.js project — run `npm install` and exit early
    if os.path.exists(pkg_json) and not os.path.exists(req_path):
        progress = LiveProgress(query.message,
                                title=f"Installing npm packages — {name}")
        await progress.start("npm install starting...")
        progress.run_in_background(estimated_seconds=90,
                                   status="npm install (downloading + linking)")
        try:
            proc_n = await asyncio.wait_for(
                create_subprocess_exec(
                    "npm", "install", "--no-audit", "--no-fund",
                    stdout=PIPE, stderr=PIPE, cwd=pdir,
                ),
                timeout=600,
            )
            stdout_n, stderr_n = await asyncio.wait_for(proc_n.communicate(), timeout=600)
            if proc_n.returncode == 0:
                await progress.stop(success=True,
                                    final_text=f"npm packages reinstalled for {name}")
            else:
                err = (stderr_n or b"").decode()[:400]
                await progress.stop(success=False, final_text=f"```\n{err}\n```")
        except asyncio.TimeoutError:
            await progress.stop(success=False, final_text="npm install timed out")
        except FileNotFoundError:
            await progress.stop(success=False, final_text="npm not installed on host.")
        except Exception as e:
            await progress.stop(success=False, final_text=f"npm error: {escape_md(str(e))}")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Restart Project", callback_data=f"restart:{name}")],
            [InlineKeyboardButton("🔙 Back", callback_data=f"proj:{name}")],
        ])
        await query.message.reply_text("Choose next:", reply_markup=kb)
        return

    if not os.path.exists(req_path):
        await safe_edit(
            query,
            f"⚠️ *No requirements.txt or package.json found* in `{name}`.\n\nUpload one via 📁 Files first.",
            reply_markup=back_kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    results = []

    # Ensure venv exists — with live animated progress
    if not os.path.exists(pip_path):
        progress = LiveProgress(query.message,
                                title=f"Creating venv — {name}")
        await progress.start("python -m venv ...")
        progress.run_in_background(estimated_seconds=20,
                                   status="Building virtual environment")
        try:
            proc = await asyncio.wait_for(
                create_subprocess_exec(sys.executable, "-m", "venv", venv_dir,
                                       stdout=PIPE, stderr=PIPE),
                timeout=120,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode == 0:
                await progress.stop(success=True,
                                    final_text="Virtual environment created")
                results.append("✅ Virtual environment created")
            else:
                err = stderr.decode()[:200]
                await progress.stop(success=False, final_text=err)
                results.append(f"❌ venv failed: {err}")
                await query.message.reply_text(
                    f"📦 *Reinstall failed*\n\n" + "\n".join(results),
                    reply_markup=back_kb,
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
        except Exception as e:
            await progress.stop(success=False, final_text=str(e))
            results.append(f"❌ venv error: {e}")
            await query.message.reply_text(
                f"📦 *Reinstall failed*\n\n" + "\n".join(results),
                reply_markup=back_kb,
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    # Upgrade pip first (best effort) — with live animated progress
    pip_progress = LiveProgress(query.message,
                                title=f"Upgrading pip — {name}")
    await pip_progress.start("pip install --upgrade pip")
    pip_progress.run_in_background(estimated_seconds=15,
                                   status="Fetching latest pip")
    try:
        proc = await asyncio.wait_for(
            create_subprocess_exec(pip_path, "install", "--upgrade", "pip",
                                   stdout=PIPE, stderr=PIPE, cwd=pdir),
            timeout=120,
        )
        await asyncio.wait_for(proc.communicate(), timeout=120)
        await pip_progress.stop(success=True, final_text="pip upgraded")
        results.append("✅ pip upgraded")
    except Exception:
        await pip_progress.stop(success=False, final_text="pip upgrade skipped")
        results.append("⚠️ pip upgrade skipped")

    # Install requirements — with live animated progress (nonstop until done)
    req_progress = LiveProgress(query.message,
                                title=f"Installing requirements — {name}")
    await req_progress.start("pip install -r requirements.txt")
    req_progress.run_in_background(estimated_seconds=120,
                                   status="Resolving + downloading wheels")
    try:
        proc = await asyncio.wait_for(
            create_subprocess_exec(
                pip_path, "install", "-r", req_path, "--upgrade",
                stdout=PIPE, stderr=PIPE, cwd=pdir,
            ),
            timeout=600,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        if proc.returncode == 0:
            await req_progress.stop(success=True,
                                    final_text="Requirements installed")
            results.append("✅ Requirements installed successfully")
        else:
            err = stderr.decode()[:400] if stderr else "unknown error"
            await req_progress.stop(success=False, final_text=f"```\n{err}\n```")
            results.append(f"❌ pip install failed:\n```\n{err}\n```")
            await query.message.reply_text(
                f"📦 *Reinstall failed for {name}*\n\n" + "\n".join(results),
                reply_markup=back_kb,
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    except asyncio.TimeoutError:
        await req_progress.stop(success=False, final_text="pip install timed out")
        results.append("❌ pip install timed out")
        await query.message.reply_text(
            f"📦 *Reinstall failed for {name}*\n\n" + "\n".join(results),
            reply_markup=back_kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except Exception as e:
        await req_progress.stop(success=False, final_text=str(e))
        results.append(f"❌ pip error: {e}")
        await query.message.reply_text(
            f"📦 *Reinstall failed for {name}*\n\n" + "\n".join(results),
            reply_markup=back_kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Verify
    try:
        proc2 = await asyncio.wait_for(
            create_subprocess_exec(pip_path, "list", stdout=PIPE, stderr=PIPE),
            timeout=30,
        )
        out2, _ = await asyncio.wait_for(proc2.communicate(), timeout=30)
        pkg_count = max(len(out2.decode().strip().splitlines()) - 2, 0)
        results.append(f"✅ {pkg_count} packages available")
    except Exception:
        results.append("⚠️ Could not verify packages")

    is_running = p.get("status") == "running"
    note = ""
    if is_running:
        note = "\n\nℹ️ Project abhi running hai. Naye packages use karne ke liye 🔄 *Restart* dabaiye."

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Restart Project", callback_data=f"restart:{name}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"proj:{name}")],
    ])

    await safe_edit(
        query,
        f"🎉 *Requirements reinstalled for {name}!*\n\n" + "\n".join(results) + note,
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )

# ─────────────────────────────────────────────────────────────
# ▶️ Run project
# ─────────────────────────────────────────────────────────────

async def start_project_process(uid: int, name: str) -> dict:
    """Start project subprocess. Returns updated project dict."""
    p   = await get_project(uid, name)
    pdir = project_dir(uid, name)
    cmd  = p.get("run_command") or "python main.py"

    log_path = os.path.join(pdir, "output.log")

    venv_python = os.path.join(pdir, "venv", "bin", "python")
    if not os.path.exists(venv_python):
        venv_python = sys.executable

    import shlex
    parts = shlex.split(cmd)
    if parts and parts[0] in ("python", "python3"):
        parts[0] = venv_python

    logger.info(f"Starting process: {' '.join(parts)} in {pdir}")

    # Load .env file and merge into subprocess environment
    import copy
    proc_env = copy.copy(os.environ)
    env_path = os.path.join(pdir, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as ef:
            for eline in ef:
                eline = eline.strip()
                if eline and not eline.startswith("#") and "=" in eline:
                    ekey, _, evalue = eline.partition("=")
                    proc_env[ekey.strip()] = evalue.strip()
        logger.info(f"Loaded .env for project {name}")

    # Use file descriptor for log output
    log_fd = open(log_path, "a")

    proc = await create_subprocess_exec(
        *parts,
        stdout=log_fd,
        stderr=log_fd,
        cwd=pdir,
        env=proc_env,
        start_new_session=True,  # Bot restart pe ye process band nahi hoga
    )
    # Close in parent process — subprocess already has its own copy
    log_fd.close()

    logger.info(f"Process started with PID {proc.pid}")

    now = datetime.now(timezone.utc)
    await projects_col.update_one(
        {"user_id": uid, "name": name},
        {"$set": {
            "status":       "running",
            "pid":          proc.pid,
            "started_at":   now,
            "last_run":     now,
            "exit_code":    None,
            "admin_stopped": False,
        }},
    )
    # Store proc object in memory for monitoring
    context_store[f"{uid}:{name}"] = proc

    # Verify it was saved
    updated = await get_project(uid, name)
    logger.info(f"DB updated - status: {updated.get('status')}, pid: {updated.get('pid')}")
    return updated

context_store: dict = {}

async def cb_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    p = await get_project(uid, name)
    if not p:
        await safe_edit(query, "❌ Project not found.", parse_mode=ParseMode.MARKDOWN)
        return

    if p.get("admin_stopped"):
        await safe_edit(
            query,
            "⚠️ Your project was stopped by admin. Contact owner.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"proj:{name}")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if p.get("status") == "running" and p.get("pid"):
        if psutil.pid_exists(p["pid"]):
            await safe_edit(query, "▶️ Project is already running.", parse_mode=ParseMode.MARKDOWN)
            return

    if not p.get("run_command"):
        await safe_edit(
            query,
            "❌ No run command set. Use ✏️ Edit CMD first.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"proj:{name}")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await safe_edit(query, f"▶️ Starting {name}...")

    try:
        updated = await start_project_process(uid, name)
        logger.info(f"Started project {name} for user {uid}, PID: {updated.get('pid')}")
        await safe_edit(
            query,
            project_dashboard_text(updated),
            reply_markup=project_dashboard_kb(uid, name, updated.get("auto_restart", True), updated.get("status") == "running"),
        )
    except Exception as e:
        logger.error(f"Failed to start project {name}: {e}")
        await safe_edit(query, f"❌ Failed to start: {str(e)[:300]}")

# ─────────────────────────────────────────────────────────────
# ⏹ Stop project
# ─────────────────────────────────────────────────────────────

async def cb_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    p = await get_project(uid, name)
    if not p:
        await safe_edit(query, "❌ Project not found.")
        return

    if p.get("status") != "running":
        await safe_edit(query, "⏹ Project is not running.", parse_mode=ParseMode.MARKDOWN)
        return

    await safe_edit(query, f"⏹ Stopping {name}...")
    await kill_project(uid, name)

    p = await get_project(uid, name)
    await safe_edit(
        query,
        project_dashboard_text(p),
        reply_markup=project_dashboard_kb(uid, name, p.get("auto_restart", True), p.get("status") == "running"),
    )

# ─────────────────────────────────────────────────────────────
# 🔄 Restart
# ─────────────────────────────────────────────────────────────

async def kill_project(uid: int, name: str):
    p = await get_project(uid, name)
    if p and p.get("pid"):
        try:
            proc = psutil.Process(p["pid"])
            for child in proc.children(recursive=True):
                child.kill()
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    await projects_col.update_one(
        {"user_id": uid, "name": name},
        {"$set": {"status": "stopped", "pid": None}},
    )

async def cb_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    p = await get_project(uid, name)
    if not p:
        await safe_edit(query, "❌ Project not found.", parse_mode=ParseMode.MARKDOWN)
        return

    if p.get("admin_stopped"):
        await safe_edit(query, "⚠️ Your project was stopped by admin. Contact owner.", parse_mode=ParseMode.MARKDOWN)
        return

    await safe_edit(query, f"🔄 Restarting *{escape_md(name)}*...", parse_mode=ParseMode.MARKDOWN)
    await kill_project(uid, name)
    await asyncio.sleep(1)

    try:
        updated = await start_project_process(uid, name)
        await safe_edit(
            query,
            project_dashboard_text(updated),
            reply_markup=project_dashboard_kb(uid, name, updated.get("auto_restart", True), updated.get("status") == "running"),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await safe_edit(query, f"❌ Restart failed: {escape_md(str(e))}", parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# 📋 Logs
# ─────────────────────────────────────────────────────────────

async def cb_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    log_path = os.path.join(project_dir(uid, name), "output.log")
    if not os.path.exists(log_path):
        lines = "No logs yet."
    else:
        with open(log_path, "r", errors="replace") as f:
            all_lines = f.readlines()
        lines = "".join(all_lines[-50:]) or "Log file is empty."

    # Truncate to Telegram's 4096 char limit
    if len(lines) > 3500:
        lines = "...(truncated)...\n" + lines[-3500:]

    text = f"📋 *Logs — {escape_md(name)}*\n\n```\n{escape_md(lines)}\n```"
    kb   = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"proj:{name}")]])
    await safe_edit(query, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# ✏️ Edit Run CMD — ConversationHandler
# ─────────────────────────────────────────────────────────────

async def cb_editcmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    context.user_data["editcmd_project"] = name
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"proj:{name}")]])
    await safe_edit(
        query,
        f"✏️ *Edit Run Command for {escape_md(name)}*\n\nSend the new run command.\nExample: `python main.py`",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return EDIT_RUN_CMD

async def editcmd_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    cmd  = update.message.text.strip()
    name = context.user_data.get("editcmd_project")

    await projects_col.update_one(
        {"user_id": uid, "name": name},
        {"$set": {"run_command": cmd}},
    )
    p   = await get_project(uid, name)
    kb  = project_dashboard_kb(uid, name, p.get("auto_restart", True), p.get("status") == "running")
    await update.message.reply_text(
        f"✅ Run command updated!\n\n" + project_dashboard_text(p),
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────
# 📁 File Manager
# ─────────────────────────────────────────────────────────────

async def cb_filemgr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    token    = secrets.token_urlsafe(24)
    now      = datetime.now(timezone.utc)
    expires  = now.timestamp() + 600  # 10 minutes

    # Store in memory for Flask
    from file_manager import token_store
    token_store[token] = {
        "user_id":      uid,
        "project_name": name,
        "project_dir":  project_dir(uid, name),
        "expires_at":   expires,
    }
    # Store in MongoDB
    await tokens_col.insert_one({
        "token":        token,
        "user_id":      uid,
        "project_name": name,
        "created_at":   now,
        "expires_at":   datetime.fromtimestamp(expires, tz=timezone.utc),
    })

    url = f"{BASE_URL}/fm/{token}/"
    kb  = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Open File Manager", url=url)],
        [InlineKeyboardButton("🔙 Back",              callback_data=f"proj:{name}")],
    ])
    await safe_edit(
        query,
        f"📁 *File Manager*\n\nYour session link (valid 10 min):\n`{escape_md(url)}`",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )

# ─────────────────────────────────────────────────────────────
# 🗑 Delete project
# ─────────────────────────────────────────────────────────────

async def cb_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    kb   = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"delete_yes:{name}"),
            InlineKeyboardButton("❌ Cancel",       callback_data=f"proj:{name}"),
        ],
    ])
    await safe_edit(
        query,
        f"🗑 *Delete {escape_md(name)}?*\n\nThis cannot be undone.",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )

async def cb_delete_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    name = query.data.split(":", 1)[1]

    await kill_project(uid, name)
    pdir = project_dir(uid, name)
    if os.path.exists(pdir):
        shutil.rmtree(pdir, ignore_errors=True)
    await projects_col.delete_one({"user_id": uid, "name": name})
    # Also remove any backups for this project from ALL DBs (sharded storage)
    for col in all_backup_cols():
        try:
            await col.delete_many({"type": "file_backup", "user_id": uid, "project_name": name})
        except Exception as e:
            logger.warning(f"Backup cleanup failed on one DB: {e}")

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 My Projects", callback_data="my_projects")]])
    await safe_edit(query, f"✅ Project *{escape_md(name)}* deleted.", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# 🆕 New Project — ConversationHandler
# ─────────────────────────────────────────────────────────────

async def cb_new_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return ConversationHandler.END

    premium = await is_premium(uid)
    count   = await project_count(uid)
    limit   = PREMIUM_LIMIT if premium else FREE_LIMIT

    if count >= limit:
        lbl = "∞" if premium else str(FREE_LIMIT)
        await safe_edit(
            query,
            f"❌ Project limit reached ({count}/{lbl}).\nUpgrade to Premium for more!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="back_start")]])
    await safe_edit(
        query,
        "📝 *New Project*\n\nEnter a project name:\n(alphanumeric + underscore, max 20 chars)",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return NEW_PROJECT_NAME

async def new_project_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.message.text.strip()

    if not re.match(r"^[a-zA-Z0-9_]{1,20}$", name):
        await update.message.reply_text(
            "❌ Invalid name. Use only letters, numbers, underscore (max 20). Try again:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return NEW_PROJECT_NAME

    existing = await get_project(uid, name)
    if existing:
        await update.message.reply_text(
            f"❌ You already have a project named *{escape_md(name)}*. Choose another:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return NEW_PROJECT_NAME

    context.user_data["new_project_name"]  = name
    context.user_data["new_project_files"] = []

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done Uploading", callback_data="upload_done")]])
    await update.message.reply_text(
        f"📁 *Project: {escape_md(name)}*\n\n"
        f"Now send your files one by one, or a single `.zip` file.\n"
        f"When done, click *Done Uploading* or send /done.",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return NEW_PROJECT_FILES

async def new_project_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = context.user_data.get("new_project_name")
    pdir = project_dir(uid, name)
    os.makedirs(pdir, exist_ok=True)

    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please send a file document.", parse_mode=ParseMode.MARKDOWN)
        return NEW_PROJECT_FILES

    file_obj  = await doc.get_file()
    file_name = doc.file_name or "file"
    dest      = os.path.join(pdir, file_name)
    await file_obj.download_to_drive(dest)

    context.user_data["new_project_files"].append(file_name)

    # Auto-extract zip (case-insensitive)
    if file_name.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(dest, "r") as zf:
                names = [n for n in zf.namelist() if not n.startswith("__MACOSX")]
                zf.extractall(pdir, members=names)
            os.remove(dest)

            # Auto-flatten: if zip had a single root folder, move its contents up
            top_levels = {n.split("/", 1)[0] for n in names if n and not n.startswith("/")}
            top_levels.discard("")
            if len(top_levels) == 1:
                only_root = next(iter(top_levels))
                root_path = os.path.join(pdir, only_root)
                if os.path.isdir(root_path):
                    for item in os.listdir(root_path):
                        src = os.path.join(root_path, item)
                        dst = os.path.join(pdir, item)
                        if os.path.exists(dst):
                            if os.path.isdir(dst):
                                shutil.rmtree(dst)
                            else:
                                os.remove(dst)
                        shutil.move(src, dst)
                    try:
                        shutil.rmtree(root_path)
                    except Exception:
                        pass

            extracted_count = len([n for n in names if not n.endswith("/")])
            await update.message.reply_text(
                f"📦 `{escape_md(file_name)}` extracted ({extracted_count} files).\n"
                f"Send more files or click *Done Uploading*.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except zipfile.BadZipFile:
            try: os.remove(dest)
            except Exception: pass
            await update.message.reply_text(
                f"❌ `{escape_md(file_name)}` corrupt zip file hai. Dobara upload karein.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error(f"Zip extract error for {file_name}: {e}")
            await update.message.reply_text(
                f"❌ Extract failed: `{escape_md(str(e))[:200]}`",
                parse_mode=ParseMode.MARKDOWN,
            )
    else:
        await update.message.reply_text(
            f"✅ `{escape_md(file_name)}` uploaded. Send more or click Done.",
            parse_mode=ParseMode.MARKDOWN,
        )

    return NEW_PROJECT_FILES

async def new_project_done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _finalize_new_project(update, context, via_message=True)

async def new_project_done_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await _finalize_new_project(update, context, via_message=False)

# ─────────────────────────────────────────────────────────────
# ISSUE 3 FIX: Animated setup progress in _finalize_new_project
# ─────────────────────────────────────────────────────────────

async def _finalize_new_project(update: Update, context: ContextTypes.DEFAULT_TYPE, via_message: bool):
    uid  = update.effective_user.id
    name = context.user_data.get("new_project_name")
    pdir = project_dir(uid, name)

    status_msg = await (update.message or update.callback_query.message).reply_text(
        f"⚙️ *Setting up {escape_md(name)}*\n\n"
        f"⏳ Initializing project...",
        parse_mode=ParseMode.MARKDOWN,
    )

    results = []

    # Step 1: Create venv — with live animated progress (nonstop until done)
    venv_progress = LiveProgress(status_msg,
                                 title=f"Setup — {name} (venv)")
    await venv_progress.start("python -m venv venv")
    venv_progress.run_in_background(estimated_seconds=20,
                                    status="Creating virtual environment")
    try:
        proc = await asyncio.wait_for(
            create_subprocess_exec(sys.executable, "-m", "venv", os.path.join(pdir, "venv"),
                                   stdout=PIPE, stderr=PIPE),
            timeout=60,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0:
            await venv_progress.stop(success=True, final_text="Virtual environment created")
            results.append("✅ Virtual environment created")
        else:
            err = stderr.decode()[:200]
            await venv_progress.stop(success=False, final_text=err)
            results.append(f"❌ venv failed: {err}")
    except asyncio.TimeoutError:
        await venv_progress.stop(success=False, final_text="venv timed out")
        results.append("❌ venv timed out")
    except Exception as e:
        await venv_progress.stop(success=False, final_text=str(e))
        results.append(f"❌ venv error: {e}")

    # Step 2a: Install Node.js dependencies if package.json exists
    pkg_json_path = os.path.join(pdir, "package.json")
    if os.path.exists(pkg_json_path):
        npm_progress = LiveProgress(status_msg,
                                    title=f"Setup — {name} (npm)")
        await npm_progress.start("npm install starting...")
        npm_progress.run_in_background(estimated_seconds=90,
                                       status="Installing npm packages")
        try:
            proc_n = await asyncio.wait_for(
                create_subprocess_exec(
                    "npm", "install", "--no-audit", "--no-fund",
                    stdout=PIPE, stderr=PIPE, cwd=pdir,
                ),
                timeout=600,
            )
            _, stderr_n = await asyncio.wait_for(proc_n.communicate(), timeout=600)
            if proc_n.returncode == 0:
                await npm_progress.stop(success=True, final_text="npm packages installed")
                results.append("✅ npm packages installed")
            else:
                err = stderr_n.decode()[:300]
                await npm_progress.stop(success=False, final_text=err)
                results.append(f"❌ npm install failed: {err}")
        except asyncio.TimeoutError:
            await npm_progress.stop(success=False, final_text="npm install timed out")
            results.append("❌ npm install timed out")
        except FileNotFoundError:
            await npm_progress.stop(success=False, final_text="npm not found on host")
            results.append("❌ npm not found on host")
        except Exception as e:
            await npm_progress.stop(success=False, final_text=str(e))
            results.append(f"❌ npm error: {e}")

    # Step 2b: Install Python requirements if present
    req_path = os.path.join(pdir, "requirements.txt")
    pip_path = os.path.join(pdir, "venv", "bin", "pip")
    if os.path.exists(req_path) and os.path.exists(pip_path):
        req_progress = LiveProgress(status_msg,
                                    title=f"Setup — {name} (requirements)")
        await req_progress.start("pip install -r requirements.txt")
        req_progress.run_in_background(estimated_seconds=120,
                                       status="Resolving + downloading wheels")
        try:
            proc = await asyncio.wait_for(
                create_subprocess_exec(
                    pip_path, "install", "-r", req_path,
                    stdout=PIPE, stderr=PIPE, cwd=pdir,
                ),
                timeout=300,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            if proc.returncode == 0:
                await req_progress.stop(success=True, final_text="Requirements installed")
                results.append("✅ Requirements installed")
            else:
                err = stderr.decode()[:300]
                await req_progress.stop(success=False, final_text=err)
                results.append(f"❌ pip install failed: {err}")
        except asyncio.TimeoutError:
            await req_progress.stop(success=False, final_text="pip install timed out")
            results.append("❌ pip install timed out")
        except Exception as e:
            await req_progress.stop(success=False, final_text=str(e))
            results.append(f"❌ pip error: {e}")

        if os.path.exists(pip_path):
            try:
                proc2 = await asyncio.wait_for(
                    create_subprocess_exec(pip_path, "list", stdout=PIPE, stderr=PIPE),
                    timeout=30,
                )
                out2, _ = await asyncio.wait_for(proc2.communicate(), timeout=30)
                pkg_count = len(out2.decode().strip().splitlines()) - 2
                results.append(f"✅ {pkg_count} packages verified")
            except Exception:
                results.append("⚠️ Could not verify packages")
    else:
        results.append("ℹ️ No requirements.txt found")

    # Determine default run command (Python first, then Node.js)
    py_candidates   = ["main.py", "bot.py", "app.py", "index.py", "run.py"]
    node_candidates = ["index.js", "bot.js", "app.js", "main.js", "server.js"]
    default_cmd = None

    # If package.json has a "start" script, prefer `npm start`
    if os.path.exists(pkg_json_path):
        try:
            import json as _json
            with open(pkg_json_path, "r", encoding="utf-8") as _pf:
                _pkg = _json.load(_pf)
            if isinstance(_pkg, dict) and isinstance(_pkg.get("scripts"), dict) and _pkg["scripts"].get("start"):
                default_cmd = "npm start"
            elif isinstance(_pkg, dict) and _pkg.get("main") and os.path.exists(os.path.join(pdir, _pkg["main"])):
                default_cmd = f"node {_pkg['main']}"
        except Exception as _e:
            logger.warning(f"package.json parse failed for {name}: {_e}")

    if not default_cmd:
        for c in py_candidates:
            if os.path.exists(os.path.join(pdir, c)):
                default_cmd = f"python {c}"
                break
    if not default_cmd:
        for c in node_candidates:
            if os.path.exists(os.path.join(pdir, c)):
                default_cmd = f"node {c}"
                break

    # Save to DB
    await projects_col.insert_one({
        "user_id":      uid,
        "name":         name,
        "run_command":  default_cmd,
        "created_date": datetime.now(timezone.utc),
        "last_run":     None,
        "exit_code":    None,
        "status":       "stopped",
        "pid":          None,
        "admin_stopped": False,
        "auto_restart":  True,
        "restart_count": 0,
        "last_restart_at": None,
    })

    result_text = "\n".join(results)
    if default_cmd:
        result_text += f"\n\n🚀 Default run cmd: `{escape_md(default_cmd)}`"
    else:
        result_text += "\n\n⚠️ No main file detected. Set run command manually."

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Open Dashboard", callback_data=f"proj:{name}")],
        [InlineKeyboardButton("🔙 My Projects",    callback_data="my_projects")],
    ])
    await status_msg.edit_text(
        f"🎉 *Project {escape_md(name)} ready!*\n\n{result_text}\n\n[████████████] ✅ Complete!",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data.clear()
    return ConversationHandler.END

async def new_project_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_start")]])
    msg = update.effective_message
    await msg.reply_text("❌ Cancelled.", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────
# ⚙️ Admin Panel
# ─────────────────────────────────────────────────────────────

@admin_or_owner
async def cb_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    total_users   = await users_col.count_documents({})
    premium_count = await users_col.count_documents({"is_premium": True})
    banned_count  = await users_col.count_documents({"is_banned": True})
    admin_count   = await users_col.count_documents({"is_admin": True})
    total_proj    = await projects_col.count_documents({})
    running_proj  = await running_project_count()

    # Backup status
    meta = await backups_col.find_one({"type": "backup_meta"})
    if meta:
        backup_time = escape_md(meta["backed_up_at"].strftime("%Y-%m-%d %H:%M UTC"))
        backup_info = f"\n💾 Last Backup: `{backup_time}`"
    else:
        backup_info = "\n💾 Last Backup: `Never`"

    # DB count line
    db_count_line = f"\n🗄 Databases: `{1 + len(extra_dbs)}` (1 primary + {len(extra_dbs)} extra)"

    role_label = "👑 Owner" if uid == OWNER_ID else "🛡 Admin"

    text = (
        f"⚙️ *Admin Panel* ({role_label})\n\n"
        f"👥 Total Users: `{total_users}`\n"
        f"💎 Premium: `{premium_count}`\n"
        f"🛡 Admins: `{admin_count}`\n"
        f"🚫 Banned: `{banned_count}`\n"
        f"📁 Projects: `{total_proj}`\n"
        f"🟢 Running: `{running_proj}`"
        f"{db_count_line}"
        f"{backup_info}"
    )

    kb_rows = [
        [InlineKeyboardButton("👥 User List",        callback_data="admin:user_list:0"),
         InlineKeyboardButton("🟢 Running Scripts",  callback_data="admin:running")],
        [InlineKeyboardButton("💎 Give Premium",     callback_data="admin:give_premium"),
         InlineKeyboardButton("❌ Remove Premium",   callback_data="admin:remove_premium")],
        [InlineKeyboardButton("⏰ Temp Premium",     callback_data="admin:temp_premium"),
         InlineKeyboardButton("🚫 Ban User",         callback_data="admin:ban")],
        [InlineKeyboardButton("✅ Unban User",       callback_data="admin:unban"),
         InlineKeyboardButton("📢 Broadcast",        callback_data="admin:broadcast_menu")],
        [InlineKeyboardButton("💾 Backup Now",       callback_data="admin:backup_now"),
         InlineKeyboardButton("🗑 Delete All Backup", callback_data="admin:del_backups")],
        [InlineKeyboardButton("📊 Bot Status",       callback_data="bot_status")],
    ]
    # Owner-only buttons
    if uid == OWNER_ID:
        kb_rows.append([
            InlineKeyboardButton("➕ Add Admin",    callback_data="admin:add_admin"),
            InlineKeyboardButton("➖ Remove Admin", callback_data="admin:remove_admin"),
        ])
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="back_start")])

    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.MARKDOWN)

@admin_or_owner
async def cb_admin_backup_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger an immediate backup from admin panel."""
    query = update.callback_query
    await query.answer("⏳ Running backup...", show_alert=False)

    await safe_edit(
        query,
        "💾 *Backup in progress...*\n\nThis may take a moment.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        all_projects = await projects_col.find({}).to_list(length=10000)
        total_files = 0
        total_size = 0
        db_distribution = {}  # tracks how many projects went to each DB

        for proj in all_projects:
            uid  = proj["user_id"]
            name = proj["name"]
            pdir = project_dir(uid, name)

            if not os.path.exists(pdir):
                continue

            files_data = []
            for root, dirs, files in os.walk(pdir):
                dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", ".git", "node_modules")]
                for fname in files:
                    if fname in ("output.log",) or fname.endswith(".pyc"):
                        continue
                    fpath   = os.path.join(root, fname)
                    rel_path = os.path.relpath(fpath, pdir)
                    try:
                        file_size = os.path.getsize(fpath)
                        if file_size > 15 * 1024 * 1024:
                            continue
                        try:
                            with open(fpath, "r", encoding="utf-8") as f:
                                content = f.read()
                            content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
                            is_binary = False
                        except (UnicodeDecodeError, ValueError):
                            with open(fpath, "rb") as f:
                                content_bytes = f.read()
                            content_b64 = base64.b64encode(content_bytes).decode("ascii")
                            is_binary = True
                        files_data.append({
                            "path":        rel_path,
                            "content_b64": content_b64,
                            "size":        file_size,
                            "is_binary":   is_binary,
                        })
                        total_files += 1
                        total_size  += file_size
                    except Exception:
                        continue

            if files_data:
                # SHARDING: project ko uske assigned DB mein hi save karo
                target_db_name, target_col = pick_backup_col(uid, name)
                doc = {
                    "type":         "file_backup",
                    "user_id":      uid,
                    "project_name": name,
                    "files":        files_data,
                    "backed_up_at": datetime.now(timezone.utc),
                    "stored_in":    target_db_name,
                }
                try:
                    # Pehle saare DBs se purana backup remove (cleanup any old duplicates)
                    for col in all_backup_cols():
                        try:
                            await col.delete_many({
                                "type":         "file_backup",
                                "user_id":      uid,
                                "project_name": name,
                            })
                        except Exception:
                            pass
                    # Phir sirf target DB mein insert
                    await target_col.insert_one(doc)
                    # Track distribution stats
                    db_distribution[target_db_name] = db_distribution.get(target_db_name, 0) + 1
                except Exception as e:
                    logger.warning(f"Backup write failed for {name} on {target_db_name}: {e}")

        # Backup_meta sirf primary DB mein (summary) — sab DBs aggregate hota hai yahan
        now = datetime.now(timezone.utc)
        try:
            await backups_col.delete_many({"type": "backup_meta"})
            await backups_col.insert_one({
                "type":           "backup_meta",
                "total_projects": len(all_projects),
                "total_files":    total_files,
                "total_size":     total_size,
                "backed_up_at":   now,
                "distribution":   db_distribution,
            })
        except Exception as e:
            logger.warning(f"Backup meta write failed: {e}")

        total_db_count = 1 + len(extra_dbs)
        logger.info(f"Sharded backup complete: {len(all_projects)} projects, {total_files} files, {total_size} bytes — distributed across {total_db_count} DB(s): {db_distribution}")

        backup_time = escape_md(now.strftime("%Y-%m-%d %H:%M UTC"))

        # Build distribution display
        dist_lines = ""
        if db_distribution:
            dist_lines = "\n*📊 Storage Distribution:*\n"
            for db_name, count in sorted(db_distribution.items()):
                dist_lines += f"   • `{escape_md(db_name)}`: `{count}` projects\n"

        result_text = (
            f"✅ *Backup Complete!*\n\n"
            f"📁 Projects: `{len(all_projects)}`\n"
            f"📄 Files: `{total_files}`\n"
            f"📦 Size: `{escape_md(fmt_bytes(total_size))}`\n"
            f"🗄 Distributed across: `{total_db_count}` database(s)\n"
            f"🕐 Time: `{backup_time}`"
            f"{dist_lines}"
        )
    except Exception as e:
        logger.error(f"Manual backup failed: {e}")
        result_text = f"❌ *Backup Failed!*\n\n`{escape_md(str(e))}`"

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]])
    await safe_edit(query, result_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# 🗑 Delete All Backups (admin)
# ─────────────────────────────────────────────────────────────

@owner_only
async def cb_admin_delete_backups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show confirmation before wiping every backup from both DBs."""
    query = update.callback_query
    await query.answer()

    # Count backups in primary + all extras
    primary_count = await backups_col.count_documents({})
    extra_counts = []
    total_extra = 0
    for entry in extra_dbs:
        try:
            c = await entry["db"]["backups"].count_documents({})
        except Exception as e:
            logger.warning(f"Backup count failed for {entry['name']}: {e}")
            c = 0
        extra_counts.append((entry["name"], c))
        total_extra += c

    lines = [
        "⚠️ *Delete ALL Backups?*\n",
        f"📂 Primary DB (`{DATABASE_NAME}`): `{primary_count}` docs",
    ]
    if extra_counts:
        lines.append(f"\n📂 *Extra DBs ({len(extra_counts)}):*")
        for name, c in extra_counts:
            lines.append(f"   • `{name}`: `{c}` docs")
        lines.append(f"\n📊 *Total to delete:* `{primary_count + total_extra}` documents")
    else:
        lines.append("\nℹ️ No extra DBs configured.")
    lines.append("\nYe action *permanent* hai aur *undo nahi* ho sakta.")
    lines.append("Project files chhediyaa nahi jayengi — sirf MongoDB backups delete honge.")

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Yes, Delete All", callback_data="admin:del_backups_confirm")],
        [InlineKeyboardButton("🔙 Cancel",          callback_data="admin_panel")],
    ])
    await safe_edit(query, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@owner_only
async def cb_admin_delete_backups_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Actually delete every backup document from both databases."""
    query = update.callback_query
    await query.answer("⏳ Deleting backups...", show_alert=False)

    await safe_edit(
        query,
        "🗑 *Deleting all backups...*\n\nPlease wait.",
        reply_markup=None,
        parse_mode=ParseMode.MARKDOWN,
    )

    primary_deleted = 0
    extra_results = []  # list of (name, deleted_count, error_or_None)
    errors = []

    # Primary DB — wipe entire backups collection
    try:
        res = await backups_col.delete_many({})
        primary_deleted = res.deleted_count
    except Exception as e:
        errors.append(f"Primary DB error: {e}")
        logger.error(f"Delete all backups (primary) failed: {e}")

    # Wipe all extra DBs' backups collections
    for entry in extra_dbs:
        name = entry["name"]
        try:
            res_x = await entry["db"]["backups"].delete_many({})
            extra_results.append((name, res_x.deleted_count, None))
        except Exception as e:
            extra_results.append((name, 0, str(e)))
            errors.append(f"DB '{name}' error: {e}")
            logger.error(f"Delete all backups ({name}) failed: {e}")

    total_deleted = primary_deleted + sum(c for _, c, _ in extra_results)
    logger.info(
        f"Admin wiped backups: primary={primary_deleted}, extras={[(n, c) for n, c, _ in extra_results]}, total={total_deleted}"
    )

    lines = [
        "✅ *All Backups Deleted!*\n",
        f"📂 Primary (`{DATABASE_NAME}`): `{primary_deleted}` removed",
    ]
    if extra_results:
        lines.append(f"\n📂 *Extra DBs ({len(extra_results)}):*")
        for name, count, err in extra_results:
            if err:
                lines.append(f"   • `{name}`: ❌ failed")
            else:
                lines.append(f"   • `{name}`: `{count}` removed")
        lines.append(f"\n📊 *Total deleted:* `{total_deleted}` documents")
    else:
        lines.append("ℹ️ No extra DBs configured — skipped")

    if errors:
        lines.append("\n⚠️ *Some errors occurred:*")
        for err in errors[:5]:  # limit to 5 errors
            lines.append(f"`{escape_md(err)}`")

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]])
    await safe_edit(query, "\n".join(lines), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@admin_or_owner
async def cb_admin_user_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[-1])
    per_page = 10

    total = await users_col.count_documents({})
    users = await users_col.find({}).skip(page * per_page).limit(per_page).to_list(length=per_page)

    lines = [f"👥 *User List* (page {page+1})\n"]
    for u in users:
        badges = ""
        if u.get("is_admin"):
            badges += " 🛡"
        if u.get("is_premium"):
            badges += " 💎"
        if u.get("is_banned"):
            badges += " 🚫"
        uname = f"@{u['username']}" if u.get("username") else "no-username"
        lines.append(f"`{u['user_id']}` {escape_md(uname)}{badges}")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin:user_list:{page-1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"admin:user_list:{page+1}"))

    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])

    await safe_edit(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# ISSUE 1 + 2 FIX: Enhanced cb_admin_running with timezone fix
# ─────────────────────────────────────────────────────────────

@admin_or_owner
async def cb_admin_running(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        running = await projects_col.find({"status": "running"}).to_list(length=100)
        if not running:
            await safe_edit(
                query,
                "🟢 *Running Scripts*\n\nNo projects running.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        lines = ["🟢 *Running Scripts*\n"]
        kb_rows = []

        for p in running:
            user_doc = await get_user(p["user_id"])
            fname = user_doc.get("first_name", "Unknown") if user_doc else "Unknown"
            uname = f"@{user_doc['username']}" if user_doc and user_doc.get("username") else "no-username"
            pid = p.get("pid", "N/A")
            # ISSUE 1 FIX: timezone-safe uptime calculation
            uptime = "N/A"
            if p.get("started_at"):
                try:
                    started = p["started_at"]
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    uptime = fmt_uptime(max(0, elapsed))
                except Exception:
                    uptime = "N/A"

            lines.append(
                f"- - - - - - - - - - -\n"
                f"👤 {fname} ({uname})\n"
                f"📁 Project: {p['name']}\n"
                f"🔹 PID: {pid} | Uptime: {uptime}"
            )
            row_btns = [InlineKeyboardButton(f"⏹ Stop {p['name']}", callback_data=f"admin_stop:{p['user_id']}:{p['name']}")]
            if query.from_user.id == OWNER_ID:
                row_btns.append(InlineKeyboardButton(f"📥 Download", callback_data=f"admin_dl:{p['user_id']}:{p['name']}"))
            kb_rows.append(row_btns)

        kb_rows.append([InlineKeyboardButton("👥 All Users & Projects", callback_data="admin:all_projects:0")])
        kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])

        full_text = "\n".join(lines)
        if len(full_text) > 4000:
            full_text = full_text[:3900] + "\n...(truncated)"

        await safe_edit(query, full_text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"cb_admin_running error: {e}")
        await safe_edit(
            query,
            f"❌ Error loading running scripts: {str(e)[:200]}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
        )

# ─────────────────────────────────────────────────────────────
# ISSUE 2 NEW HANDLERS: All Users & Projects, Admin Run, Admin Download
# ─────────────────────────────────────────────────────────────

@admin_or_owner
async def cb_admin_all_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all users with their projects for admin management."""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[-1])
    per_page = 5

    # Get all users who have projects
    all_projects = await projects_col.find({}).to_list(length=10000)

    # Group by user
    user_projects = {}
    for p in all_projects:
        uid = p["user_id"]
        if uid not in user_projects:
            user_projects[uid] = []
        user_projects[uid].append(p)

    user_ids = list(user_projects.keys())
    total = len(user_ids)
    start = page * per_page
    end = min(start + per_page, total)
    page_user_ids = user_ids[start:end]

    if not page_user_ids:
        await safe_edit(
            query,
            "👥 *All Users & Projects*\n\nNo projects found.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin:running")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [f"👥 *All Users & Projects* (page {page+1})\n"]
    kb_rows = []

    for uid in page_user_ids:
        user_doc = await get_user(uid)
        fname = user_doc.get("first_name", "Unknown") if user_doc else "Unknown"
        uname = f"@{user_doc['username']}" if user_doc and user_doc.get("username") else ""

        projects = user_projects[uid]
        proj_lines = []
        for p in projects:
            status_icon = "🟢" if p.get("status") == "running" else "🔴"
            proj_lines.append(f"  {status_icon} {p['name']}")

            # Add control buttons for each project
            is_caller_owner = query.from_user.id == OWNER_ID
            if p.get("status") == "running":
                row = [InlineKeyboardButton(f"⏹ Stop {p['name']}", callback_data=f"admin_stop:{uid}:{p['name']}")]
                if is_caller_owner:
                    row.append(InlineKeyboardButton(f"📥 DL {p['name']}", callback_data=f"admin_dl:{uid}:{p['name']}"))
                kb_rows.append(row)
            else:
                row = [InlineKeyboardButton(f"▶️ Run {p['name']}", callback_data=f"admin_run:{uid}:{p['name']}")]
                if is_caller_owner:
                    row.append(InlineKeyboardButton(f"📥 DL {p['name']}", callback_data=f"admin_dl:{uid}:{p['name']}"))
                kb_rows.append(row)

        lines.append(
            f"- - - - - - - - - - -\n"
            f"👤 {fname} {uname} (`{uid}`)\n"
            f"📁 {len(projects)} project(s):\n" + "\n".join(proj_lines)
        )

    # Pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin:all_projects:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"admin:all_projects:{page+1}"))
    if nav:
        kb_rows.append(nav)

    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin:running")])

    full_text = "\n".join(lines)
    if len(full_text) > 4000:
        full_text = full_text[:3900] + "\n...(truncated)"

    await safe_edit(query, full_text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.MARKDOWN)


@admin_or_owner
async def cb_admin_run_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, uid_str, name = query.data.split(":", 2)
    uid = int(uid_str)

    p = await get_project(uid, name)
    if not p:
        await safe_edit(
            query,
            "❌ Project not found.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin:all_projects:0")]]),
        )
        return

    if not p.get("run_command"):
        await safe_edit(
            query,
            f"❌ No run command set for *{escape_md(name)}*.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin:all_projects:0")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Already running? Don't double-start.
    if p.get("status") == "running" and p.get("pid") and psutil.pid_exists(p["pid"]):
        await safe_edit(
            query,
            f"▶️ Project *{escape_md(name)}* is already running.\nPID: `{p.get('pid')}`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin:all_projects:0")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        updated = await start_project_process(uid, name)
        # Notify project owner that admin started their project
        try:
            if notification_bot:
                await notification_bot.send_message(
                    uid,
                    f"▶️ Your project *{escape_md(name)}* was started by admin.",
                    parse_mode=ParseMode.MARKDOWN,
                )
        except Exception as _ne:
            logger.warning(f"admin_run notify failed for {uid}: {_ne}")

        await safe_edit(
            query,
            f"✅ Project *{escape_md(name)}* started by admin.\nPID: `{updated.get('pid', 'N/A')}`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 All Projects", callback_data="admin:all_projects:0")],
                [InlineKeyboardButton("🟢 Running Scripts", callback_data="admin:running")],
            ]),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Admin run project failed for {uid}/{name}: {e}")
        await safe_edit(
            query,
            f"❌ Failed to start *{escape_md(name)}*: `{escape_md(str(e))[:250]}`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin:all_projects:0")]]),
            parse_mode=ParseMode.MARKDOWN,
        )


@owner_only
async def cb_admin_download_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("📥 Creating zip...", show_alert=False)
    _, uid_str, name = query.data.split(":", 2)
    uid = int(uid_str)

    pdir = project_dir(uid, name)
    if not os.path.exists(pdir):
        await query.answer("❌ Project directory not found!", show_alert=True)
        return

    # Create zip file
    zip_path = os.path.join(PROJECTS_ROOT, f"{uid}_{name}.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(pdir):
                # Skip venv and __pycache__
                dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", ".git", "node_modules")]
                for fname_file in files:
                    if fname_file in ("output.log",) or fname_file.endswith(".pyc"):
                        continue
                    fpath = os.path.join(root, fname_file)
                    arcname = os.path.relpath(fpath, pdir)
                    zf.write(fpath, arcname)

        # Send the zip file
        with open(zip_path, "rb") as f:
            await query.message.reply_document(
                document=f,
                filename=f"{name}.zip",
                caption=f"📥 Project: {name}\nUser ID: {uid}",
            )
    except Exception as e:
        logger.error(f"Admin download failed: {e}")
        await query.answer(f"❌ Download failed: {str(e)[:100]}", show_alert=True)
    finally:
        # Clean up zip
        if os.path.exists(zip_path):
            os.remove(zip_path)


@admin_or_owner
async def cb_admin_stop_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, uid_str, name = query.data.split(":", 2)
    uid = int(uid_str)

    p = await get_project(uid, name)
    if not p:
        await safe_edit(
            query,
            "❌ Project not found.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin:running")]]),
        )
        return

    try:
        await kill_project(uid, name)
        await projects_col.update_one(
            {"user_id": uid, "name": name},
            {"$set": {"admin_stopped": True}},
        )
        # Notify project owner that admin stopped their project
        try:
            if notification_bot:
                await notification_bot.send_message(
                    uid,
                    f"⏹ Your project *{escape_md(name)}* was stopped by admin.\nContact owner to resume.",
                    parse_mode=ParseMode.MARKDOWN,
                )
        except Exception as _ne:
            logger.warning(f"admin_stop notify failed for {uid}: {_ne}")

        await safe_edit(
            query,
            f"✅ Project *{escape_md(name)}* stopped (admin).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🟢 Running Scripts", callback_data="admin:running")],
                [InlineKeyboardButton("👥 All Projects", callback_data="admin:all_projects:0")],
            ]),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Admin stop project failed for {uid}/{name}: {e}")
        await safe_edit(
            query,
            f"❌ Failed to stop *{escape_md(name)}*: `{escape_md(str(e))[:250]}`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin:running")]]),
            parse_mode=ParseMode.MARKDOWN,
        )

# Admin give/remove/temp premium, ban/unban, broadcast — ConversationHandler

@admin_or_owner
async def cb_admin_give_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["admin_action"] = "give_premium"
    await safe_edit(query, "💎 *Give Premium*\n\nSend the user ID:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_GIVE_PREMIUM_ID

async def admin_give_premium_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID. Send a numeric user ID:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_GIVE_PREMIUM_ID

    await users_col.update_one(
        {"user_id": uid},
        {"$set": {"is_premium": True, "premium_expiry": None}},
    )
    try:
        await update.get_bot().send_message(uid, "🎉 You have been granted *Premium*! Enjoy unlimited projects!", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
    await update.message.reply_text(f"✅ Premium granted to `{uid}`.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

@admin_or_owner
async def cb_admin_remove_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "❌ *Remove Premium*\n\nSend the user ID:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_REMOVE_PREMIUM_ID

async def admin_remove_premium_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_REMOVE_PREMIUM_ID

    await users_col.update_one({"user_id": uid}, {"$set": {"is_premium": False, "premium_expiry": None}})
    await update.message.reply_text(f"✅ Premium removed from `{uid}`.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

@admin_or_owner
async def cb_admin_temp_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "⏰ *Temp Premium*\n\nSend the user ID:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_TEMP_PREMIUM_ID

async def admin_temp_premium_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_TEMP_PREMIUM_ID
    context.user_data["temp_premium_uid"] = uid
    await update.message.reply_text(
        "⏰ Send duration (e.g. `24h` or `7d`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_TEMP_PREMIUM_DUR

async def admin_temp_premium_dur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid  = context.user_data.get("temp_premium_uid")
    m = re.match(r"^(\d+)([hd])$", text)
    if not m:
        await update.message.reply_text("❌ Invalid format. Use `24h` or `7d`:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_TEMP_PREMIUM_DUR

    amount, unit = int(m.group(1)), m.group(2)
    seconds = amount * 3600 if unit == "h" else amount * 86400
    expiry  = datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc)

    await users_col.update_one(
        {"user_id": uid},
        {"$set": {"is_premium": True, "premium_expiry": expiry}},
    )
    try:
        await update.get_bot().send_message(uid, f"🎉 You received *Temp Premium* for {escape_md(text)}!", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
    await update.message.reply_text(f"✅ Temp premium set for `{uid}` — expires {escape_md(expiry.strftime('%Y-%m-%d %H:%M UTC'))}.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

@admin_or_owner
async def cb_admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "🚫 *Ban User*\n\nSend the user ID:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_BAN_ID

async def admin_ban_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_BAN_ID

    await users_col.update_one({"user_id": uid}, {"$set": {"is_banned": True}})
    # Stop all their projects
    user_projects = await projects_col.find({"user_id": uid, "status": "running"}).to_list(length=100)
    for p in user_projects:
        await kill_project(uid, p["name"])
    await update.message.reply_text(f"✅ User `{uid}` banned and all projects stopped.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

@admin_or_owner
async def cb_admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "✅ *Unban User*\n\nSend the user ID:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_UNBAN_ID

async def admin_unban_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_UNBAN_ID

    await users_col.update_one({"user_id": uid}, {"$set": {"is_banned": False}})
    try:
        await update.get_bot().send_message(uid, "✅ You have been unbanned! You can use the bot again.", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
    await update.message.reply_text(f"✅ User `{uid}` unbanned.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

@admin_or_owner
async def cb_admin_broadcast_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Broadcast All",  callback_data="admin:broadcast_all")],
        [InlineKeyboardButton("📩 Send to User",   callback_data="admin:send_to_user")],
        [InlineKeyboardButton("🔙 Back",           callback_data="admin_panel")],
    ])
    await safe_edit(query, "📢 *Broadcast Menu*", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@admin_or_owner
async def cb_admin_broadcast_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["broadcast_type"] = "all"
    await safe_edit(query, "📢 *Broadcast All*\n\nSend the message:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_BROADCAST_MSG

@admin_or_owner
async def cb_admin_send_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["broadcast_type"] = "user"
    await safe_edit(query, "📩 *Send to User*\n\nSend the target user ID:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_SEND_USER_ID

async def admin_send_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_SEND_USER_ID
    context.user_data["broadcast_target"] = uid
    await update.message.reply_text("Send the message:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_SEND_USER_MSG

async def admin_send_user_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = context.user_data.get("broadcast_target")
    msg = update.message.text
    try:
        await update.get_bot().send_message(uid, msg)
        await update.message.reply_text(f"✅ Sent to `{uid}`.", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {escape_md(str(e))}", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def admin_broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message.text
    bot  = update.get_bot()
    all_users = await users_col.find({}).to_list(length=10000)
    sent = failed = 0
    for u in all_users:
        try:
            await bot.send_message(u["user_id"], msg)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"📢 Broadcast complete!\n✅ Sent: `{sent}`\n❌ Failed: `{failed}`",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END

async def admin_conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
    await (update.effective_message).reply_text("❌ Cancelled.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────
# 🛡 Add / Remove Admin (Owner only)
# ─────────────────────────────────────────────────────────────

@owner_only
async def cb_admin_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_edit(
        query,
        "🛡 *Add Admin*\n\nSend the user ID jise admin banana hai:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_ADD_ADMIN_ID

async def admin_add_admin_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID. Numeric user ID bhejo:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_ADD_ADMIN_ID

    if uid == OWNER_ID:
        await update.message.reply_text("⚠️ Owner pehle se hi sabse upar hai!", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    await users_col.update_one(
        {"user_id": uid},
        {"$set": {"is_admin": True}},
        upsert=False,
    )
    try:
        await update.get_bot().send_message(
            uid,
            "🎉 Aapko *Admin* bana diya gaya hai! Ab aap Admin Panel access kar sakte ho.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ User `{uid}` ko Admin bana diya gaya.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END

@owner_only
async def cb_admin_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_edit(
        query,
        "➖ *Remove Admin*\n\nSend the user ID jiska admin remove karna hai:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_REMOVE_ADMIN_ID

async def admin_remove_admin_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid ID. Numeric user ID bhejo:", parse_mode=ParseMode.MARKDOWN)
        return ADMIN_REMOVE_ADMIN_ID

    await users_col.update_one(
        {"user_id": uid},
        {"$set": {"is_admin": False}},
    )
    try:
        await update.get_bot().send_message(
            uid,
            "⚠️ Aapka *Admin* access hata diya gaya hai.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ User `{uid}` ka Admin access hata diya gaya.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────
# ⏰ Feature 1: Auto-Restart Toggle Handler
# ─────────────────────────────────────────────────────────────

async def cb_toggle_auto_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    name = query.data.split(":", 1)[1]

    p = await get_project(uid, name)
    if not p:
        await query.answer("❌ Project not found.", show_alert=True)
        return

    current = p.get("auto_restart", True)
    new_val = not current

    await projects_col.update_one(
        {"user_id": uid, "name": name},
        {"$set": {"auto_restart": new_val}},
    )

    status = "✅ ON" if new_val else "❌ OFF"
    await query.answer(f"Auto-Restart: {status}", show_alert=True)

    # Refresh dashboard
    p = await get_project(uid, name)
    await safe_edit(
        query,
        project_dashboard_text(p),
        reply_markup=project_dashboard_kb(uid, name, p.get("auto_restart", True), p.get("status") == "running"),
    )

# ─────────────────────────────────────────────────────────────
# 🔐 Feature 3: Environment Variables Manager
# ─────────────────────────────────────────────────────────────

async def cb_envvars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show environment variables for a project."""
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    name = query.data.split(":", 1)[1]

    if await is_banned(uid):
        await safe_edit(query, "🚫 You are banned. Contact owner.")
        return

    pdir = project_dir(uid, name)
    env_path = os.path.join(pdir, ".env")

    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    env_vars[key.strip()] = value.strip()

    if not env_vars:
        text = f"🔐 *Environment Variables — {escape_md(name)}*\n\nNo variables set yet.\n\n_Tip: Click Add Variable and send like:_\n`BOT_TOKEN=your_value`"
    else:
        lines = [f"🔐 *Environment Variables — {escape_md(name)}*\n"]
        for key, value in env_vars.items():
            # Mask value: show first 3 chars + ***
            masked = value[:3] + "***" if len(value) > 3 else "***"
            lines.append(f"• `{key}` = `{masked}`")
        text = "\n".join(lines)

    kb_rows = []
    # Show edit/delete button for each var
    for key in env_vars:
        kb_rows.append([
            InlineKeyboardButton(f"✏️ {key}", callback_data=f"env_edit:{name}:{key}"),
            InlineKeyboardButton(f"🗑 {key}", callback_data=f"env_del:{name}:{key}"),
        ])
    kb_rows.append([InlineKeyboardButton("➕ Add Variable", callback_data=f"env_add:{name}")])
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"proj:{name}")])

    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.MARKDOWN)


async def cb_env_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    context.user_data["env_project"] = name

    await safe_edit(
        query,
        "➕ *Add Environment Variables*\n\n"
        "Send your variables in any format:\n\n"
        "1️⃣ *Single variable:*\n"
        "`API_KEY=your_value`\n\n"
        "2️⃣ *Multiple at once (one per line):*\n"
        "`TOKEN=abc123`\n"
        "`DB_URI=mongodb://...`\n"
        "`OWNER_ID=12345`\n\n"
        "3️⃣ *Just key name:*\n"
        "`API_KEY`\n"
        "_(bot will ask for value next)_\n\n"
        "💡 Spaces around `=` are fine!",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"envvars:{name}")]]),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ENV_ADD_KEY


async def env_add_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    name = context.user_data.get("env_project")
    uid = update.effective_user.id
    pdir = project_dir(uid, name)
    env_path = os.path.join(pdir, ".env")

    # Check if multi-line or contains = (KEY=VALUE format)
    lines = text.strip().split("\n")
    pairs_to_save = []

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key:
                pairs_to_save.append((key, value))

    if pairs_to_save:
        # Read existing env file
        existing = {}
        existing_order = []
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for eline in f:
                    eline_stripped = eline.strip()
                    if eline_stripped and not eline_stripped.startswith("#") and "=" in eline_stripped:
                        ekey, _, evalue = eline_stripped.partition("=")
                        ekey = ekey.strip()
                        existing[ekey] = evalue.strip()
                        existing_order.append(ekey)
                    elif eline_stripped:
                        # Keep comments and other lines
                        pass

        # Update/add new pairs
        for key, value in pairs_to_save:
            existing[key] = value
            if key not in existing_order:
                existing_order.append(key)

        # Write all vars back
        with open(env_path, "w") as f:
            for key in existing_order:
                f.write(f"{key}={existing[key]}\n")
            # Write any new keys not in order
            for key, value in pairs_to_save:
                if key not in existing_order:
                    f.write(f"{key}={value}\n")

        saved_keys = [k for k, v in pairs_to_save]
        saved_list = "\n".join([f"• `{k}` ✅" for k in saved_keys])

        await update.message.reply_text(
            f"✅ *{len(pairs_to_save)} variable(s) saved!*\n\n"
            f"{saved_list}\n\n"
            f"_Restart your project for changes to take effect._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add More", callback_data=f"env_add:{name}")],
                [InlineKeyboardButton("🔙 Back to Env Vars", callback_data=f"envvars:{name}")],
            ]),
        )
        context.user_data.pop("env_key", None)
        context.user_data.pop("env_project", None)
        return ConversationHandler.END

    # No = found — treat as single KEY name
    key = text.strip().split()[0] if text.strip() else ""
    if not key or len(key) > 100:
        await update.message.reply_text(
            "❌ Could not parse variables.\n\n"
            "Send in one of these formats:\n\n"
            "1️⃣ Single: `API_KEY=your_value`\n\n"
            "2️⃣ Multiple (one per line):\n"
            "`TOKEN=abc123`\n"
            "`DB_URI=mongodb://...`\n"
            "`PORT=8080`\n\n"
            "3️⃣ Just key name: `API_KEY`\n"
            "_(bot will ask for value next)_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ENV_ADD_KEY

    context.user_data["env_key"] = key
    await update.message.reply_text(
        f"Now send the value for `{key}`:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ENV_ADD_VALUE


async def env_add_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    name = context.user_data.get("env_project")
    key = context.user_data.get("env_key")
    uid = update.effective_user.id

    pdir = project_dir(uid, name)
    env_path = os.path.join(pdir, ".env")

    # Read existing env vars
    env_lines = []
    key_found = False
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if line.strip().startswith(f"{key}="):
                    env_lines.append(f"{key}={value}\n")
                    key_found = True
                else:
                    env_lines.append(line)

    if not key_found:
        env_lines.append(f"{key}={value}\n")

    with open(env_path, "w") as f:
        f.writelines(env_lines)

    await update.message.reply_text(
        f"✅ Variable `{key}` saved!\n\n_Restart your project for changes to take effect._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Env Vars", callback_data=f"envvars:{name}")]]),
    )
    context.user_data.pop("env_key", None)
    context.user_data.pop("env_project", None)
    return ConversationHandler.END


async def cb_env_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 2)
    name = parts[1]
    key = parts[2]
    context.user_data["env_project"] = name
    context.user_data["env_key"] = key

    await safe_edit(
        query,
        f"✏️ *Edit `{key}`*\n\nSend the new value:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"envvars:{name}")]]),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ENV_EDIT_VALUE


async def env_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Same as env_add_value — it overwrites the key
    return await env_add_value(update, context)


async def cb_env_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":", 2)
    name = parts[1]
    key = parts[2]
    uid = query.from_user.id

    pdir = project_dir(uid, name)
    env_path = os.path.join(pdir, ".env")

    deleted = False
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
        with open(env_path, "w") as f:
            for line in lines:
                if line.strip().startswith(f"{key}="):
                    deleted = True
                    continue
                f.write(line)

    if deleted:
        await query.answer(f"🗑 {key} deleted!", show_alert=True)
    else:
        await query.answer(f"⚠️ {key} not found.", show_alert=True)

    # Refresh the env vars screen inline (no recursive handler call)
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env_vars[k.strip()] = v.strip()

    if not env_vars:
        text = (
            f"🔐 *Environment Variables — {escape_md(name)}*\n\n"
            f"No variables set yet.\n\n"
            f"_Tip: Click Add Variable and send like:_\n`BOT_TOKEN=your_value`"
        )
    else:
        lines_out = [f"🔐 *Environment Variables — {escape_md(name)}*\n"]
        for k, v in env_vars.items():
            masked = v[:3] + "***" if len(v) > 3 else "***"
            lines_out.append(f"• `{k}` = `{masked}`")
        text = "\n".join(lines_out)

    kb_rows = []
    for k in env_vars:
        kb_rows.append([
            InlineKeyboardButton(f"✏️ {k}", callback_data=f"env_edit:{name}:{k}"),
            InlineKeyboardButton(f"🗑 {k}", callback_data=f"env_del:{name}:{k}"),
        ])
    kb_rows.append([InlineKeyboardButton("➕ Add Variable", callback_data=f"env_add:{name}")])
    kb_rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"proj:{name}")])

    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────
# Background: process monitor (Feature 1 & 2 integrated)
# ─────────────────────────────────────────────────────────────

async def process_monitor():
    while True:
        await asyncio.sleep(30)
        try:
            running = await projects_col.find({"status": "running"}).to_list(length=1000)
            for p in running:
                pid = p.get("pid")
                if pid and not psutil.pid_exists(pid):
                    # Try to get exit code
                    key  = f"{p['user_id']}:{p['name']}"
                    proc = context_store.get(key)
                    code = None
                    if proc:
                        code = proc.returncode

                    await projects_col.update_one(
                        {"user_id": p["user_id"], "name": p["name"]},
                        {"$set": {"status": "stopped", "pid": None, "exit_code": code}},
                    )

                    logger.info(f"Process {key} exited with code {code}")

                    # Feature 1: Auto-restart logic
                    if p.get("auto_restart", True) and code != 0 and not p.get("admin_stopped"):
                        # Check restart limit: max 3 restarts in 5 minutes
                        now = datetime.now(timezone.utc)
                        last_restart = p.get("last_restart_at")
                        restart_count = p.get("restart_count", 0)

                        # Reset counter if last restart was more than 5 minutes ago
                        if last_restart:
                            if last_restart.tzinfo is None:
                                last_restart = last_restart.replace(tzinfo=timezone.utc)
                            if (now - last_restart).total_seconds() > 300:
                                restart_count = 0

                        if restart_count < 3:
                            try:
                                logger.info(f"Auto-restarting {key} (attempt {restart_count + 1}/3)")
                                await asyncio.sleep(3)  # Brief delay before restart
                                await start_project_process(p["user_id"], p["name"])
                                await projects_col.update_one(
                                    {"user_id": p["user_id"], "name": p["name"]},
                                    {"$set": {"restart_count": restart_count + 1, "last_restart_at": now}},
                                )

                                # Feature 2: Send auto-restart notification
                                if notification_bot:
                                    msg_text = (
                                        f"🔄 *Auto-Restart*\n\n"
                                        f"Project `{p['name']}` crashed (exit code: {code}).\n"
                                        f"Auto-restarted successfully! ({restart_count + 1}/3)"
                                    )
                                    try:
                                        await notification_bot.send_message(
                                            chat_id=p["user_id"],
                                            text=msg_text,
                                            parse_mode=ParseMode.MARKDOWN,
                                        )
                                    except Exception:
                                        pass

                            except Exception as e:
                                logger.error(f"Auto-restart failed for {key}: {e}")
                        else:
                            # Too many restarts — notify user
                            logger.warning(f"Auto-restart limit reached for {key}")
                            if notification_bot:
                                msg_text = (
                                    f"⚠️ *Auto-Restart Limit Reached*\n\n"
                                    f"Project `{p['name']}` crashed {restart_count} times in 5 minutes.\n"
                                    f"Auto-restart disabled temporarily.\n\n"
                                    f"Check your logs and fix the issue, then restart manually."
                                )
                                try:
                                    await notification_bot.send_message(
                                        chat_id=p["user_id"],
                                        text=msg_text,
                                        parse_mode=ParseMode.MARKDOWN,
                                    )
                                except Exception:
                                    pass

                    # Feature 2: Crash notification (auto_restart is OFF)
                    elif code != 0 and not p.get("admin_stopped"):
                        if notification_bot:
                            try:
                                log_path = os.path.join(project_dir(p["user_id"], p["name"]), "output.log")
                                error_lines = ""
                                if os.path.exists(log_path):
                                    with open(log_path, "r", errors="replace") as f:
                                        lines_list = f.readlines()
                                    error_lines = "".join(lines_list[-10:]).strip()
                                    if len(error_lines) > 500:
                                        error_lines = "..." + error_lines[-500:]

                                msg_text = (
                                    f"❌ *Project Crashed*\n\n"
                                    f"Project: `{p['name']}`\n"
                                    f"Exit Code: `{code}`\n"
                                    f"Auto-Restart: OFF\n\n"
                                    f"📋 *Last Log Lines:*\n```\n{error_lines}\n```"
                                )
                                if len(msg_text) > 4000:
                                    msg_text = msg_text[:4000] + "..."

                                await notification_bot.send_message(
                                    chat_id=p["user_id"],
                                    text=msg_text,
                                    parse_mode=ParseMode.MARKDOWN,
                                )
                            except Exception:
                                pass

        except Exception as e:
            logger.warning(f"Monitor error: {e}")

# ─────────────────────────────────────────────────────────────
# 💾 Auto Backup Task (runs every 5 minutes)
# ─────────────────────────────────────────────────────────────

async def backup_task():
    """Runs every 5 minutes. Backs up all project files to MongoDB."""
    while True:
        await asyncio.sleep(300)  # 5 minutes
        try:
            all_projects = await projects_col.find({}).to_list(length=10000)

            db_distribution = {}  # tracks how many projects went to each DB
            total_files = 0
            total_size  = 0

            for proj in all_projects:
                uid  = proj["user_id"]
                name = proj["name"]
                pdir = project_dir(uid, name)

                if not os.path.exists(pdir):
                    continue

                files_data = []
                # Walk through project directory, SKIP venv/, __pycache__/, output.log
                for root, dirs, files in os.walk(pdir):
                    # Skip venv and __pycache__ directories
                    dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", ".git", "node_modules")]

                    for fname in files:
                        if fname in ("output.log",) or fname.endswith(".pyc"):
                            continue

                        fpath    = os.path.join(root, fname)
                        rel_path = os.path.relpath(fpath, pdir)

                        try:
                            # Read file (try text first, fall back to binary)
                            try:
                                with open(fpath, "r", encoding="utf-8") as f:
                                    content = f.read()
                                content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
                                is_binary = False
                            except (UnicodeDecodeError, ValueError):
                                with open(fpath, "rb") as f:
                                    content_bytes = f.read()
                                content_b64 = base64.b64encode(content_bytes).decode("ascii")
                                is_binary = True

                            file_size = os.path.getsize(fpath)
                            # Skip files > 15MB (MongoDB document limit is 16MB)
                            if file_size > 15 * 1024 * 1024:
                                continue

                            files_data.append({
                                "path":        rel_path,
                                "content_b64": content_b64,
                                "size":        file_size,
                                "is_binary":   is_binary,
                            })
                            total_files += 1
                            total_size  += file_size
                        except Exception:
                            continue

                if files_data:
                    # SHARDING: project ko uske assigned DB mein hi save
                    target_db_name, target_col = pick_backup_col(uid, name)
                    # Cleanup old copies from all DBs first
                    for col in all_backup_cols():
                        try:
                            await col.delete_many({
                                "type":         "file_backup",
                                "user_id":      uid,
                                "project_name": name,
                            })
                        except Exception:
                            pass
                    await target_col.insert_one({
                        "type":         "file_backup",
                        "user_id":      uid,
                        "project_name": name,
                        "files":        files_data,
                        "backed_up_at": datetime.now(timezone.utc),
                        "stored_in":    target_db_name,
                    })
                    db_distribution[target_db_name] = db_distribution.get(target_db_name, 0) + 1

            # Update backup metadata in primary DB only (summary)
            await backups_col.delete_many({"type": "backup_meta"})
            await backups_col.insert_one({
                "type":           "backup_meta",
                "total_projects": len(all_projects),
                "total_files":    total_files,
                "total_size":     total_size,
                "backed_up_at":   datetime.now(timezone.utc),
                "distribution":   db_distribution,
            })

            logger.info(f"Auto backup complete: {len(all_projects)} projects, {total_files} files, {total_size} bytes — distribution: {db_distribution}")

        except Exception as e:
            logger.error(f"Backup failed: {e}")

# ─────────────────────────────────────────────────────────────
# 🔄 Keep-Alive Task (prevents Render free plan from sleeping)
# ─────────────────────────────────────────────────────────────

async def keep_alive_task():
    """Ping own health endpoint every 10 minutes to prevent Render free plan from sleeping.
    Also cleans up expired file-manager tokens from MongoDB every cycle."""
    import urllib.request
    health_url = f"{BASE_URL}/health"
    logger.info(f"Keep-alive task started. Pinging {health_url} every 10 minutes.")

    while True:
        await asyncio.sleep(600)  # 10 minutes

        # Clean expired file-manager tokens from MongoDB
        try:
            result = await tokens_col.delete_many(
                {"expires_at": {"$lt": datetime.now(timezone.utc)}}
            )
            if result.deleted_count:
                logger.info(f"Cleaned {result.deleted_count} expired file-manager tokens from DB")
        except Exception as e:
            logger.warning(f"Token cleanup failed: {e}")

        # Keep-alive ping
        try:
            # Run in executor to not block event loop
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: urllib.request.urlopen(health_url, timeout=30).status
            )
            logger.info(f"Keep-alive ping OK ({resp})")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")

# ─────────────────────────────────────────────────────────────
# 🔄 Auto Restore (runs ONCE at startup in post_init)
# ─────────────────────────────────────────────────────────────

async def restore_from_backup():
    """Restore all project files from MongoDB backup. Called once at startup."""
    try:
        logger.info("Checking for backups to restore...")

        meta = await backups_col.find_one({"type": "backup_meta"})
        if not meta:
            logger.info("No backup found. Fresh start.")
            return

        logger.info(
            f"Found backup from {meta['backed_up_at']} — "
            f"{meta['total_projects']} projects, {meta['total_files']} files"
        )
        if meta.get("distribution"):
            logger.info(f"Backup distribution: {meta['distribution']}")

        # SHARDING: Read from ALL DBs (data is split across them)
        # Dedupe by (user_id, project_name) — if same project exists in
        # multiple DBs (legacy), keep the latest one.
        seen = {}  # key: (uid, name), value: backup doc
        for col in all_backup_cols():
            try:
                async for backup in col.find({"type": "file_backup"}):
                    key = (backup["user_id"], backup["project_name"])
                    existing = seen.get(key)
                    if (existing is None
                            or backup.get("backed_up_at", datetime.min.replace(tzinfo=timezone.utc))
                                > existing.get("backed_up_at", datetime.min.replace(tzinfo=timezone.utc))):
                        seen[key] = backup
            except Exception as e:
                logger.warning(f"Restore read failed on one DB: {e}")

        restored_projects = 0
        restored_files    = 0

        for backup in seen.values():
            uid  = backup["user_id"]
            name = backup["project_name"]
            pdir = project_dir(uid, name)

            os.makedirs(pdir, exist_ok=True)

            for file_data in backup.get("files", []):
                rel_path    = file_data["path"]
                content_b64 = file_data["content_b64"]
                is_binary   = file_data.get("is_binary", False)

                file_path = os.path.join(pdir, rel_path)
                parent_dir = os.path.dirname(file_path)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)

                try:
                    decoded = base64.b64decode(content_b64)
                    if is_binary:
                        with open(file_path, "wb") as f:
                            f.write(decoded)
                    else:
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(decoded.decode("utf-8"))
                    restored_files += 1
                except Exception as e:
                    logger.warning(f"Failed to restore {rel_path}: {e}")

            restored_projects += 1

        logger.info(f"Files restored: {restored_projects} projects, {restored_files} files")

        # Setup venvs in background (don't block startup)
        asyncio.create_task(setup_venvs_background())

        # Auto-restart all projects that were running before bot stopped
        asyncio.create_task(auto_restart_on_startup())

    except Exception as e:
        logger.error(f"Restore failed (non-fatal): {e}")


async def _install_requirements_for_project(uid: int, name: str) -> tuple:
    """
    Project ke requirements install karo (pip ya npm).
    Returns: (success: bool, message: str)
    """
    pdir     = project_dir(uid, name)
    req_path = os.path.join(pdir, "requirements.txt")
    pkg_json = os.path.join(pdir, "package.json")
    venv_dir = os.path.join(pdir, "venv")
    pip_path = os.path.join(venv_dir, "bin", "pip")

    # Node.js project
    if os.path.exists(pkg_json) and not os.path.exists(req_path):
        try:
            proc = await asyncio.wait_for(
                create_subprocess_exec(
                    "npm", "install", "--no-audit", "--no-fund",
                    stdout=PIPE, stderr=PIPE, cwd=pdir,
                ),
                timeout=300,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            if proc.returncode == 0:
                return (True, "npm install success")
            else:
                return (False, f"npm install failed: {stderr.decode()[:200]}")
        except asyncio.TimeoutError:
            return (False, "npm install timed out")
        except Exception as e:
            return (False, f"npm error: {e}")

    # Python project
    if not os.path.exists(req_path):
        return (True, "koi requirements file nahi mili, skip")

    # Venv banana agar nahi hai
    if not os.path.exists(pip_path):
        try:
            proc = await asyncio.wait_for(
                create_subprocess_exec(
                    sys.executable, "-m", "venv", venv_dir,
                    stdout=PIPE, stderr=PIPE,
                ),
                timeout=120,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                return (False, f"venv create failed: {stderr.decode()[:200]}")
        except Exception as e:
            return (False, f"venv error: {e}")

    # pip install -r requirements.txt
    try:
        proc = await asyncio.wait_for(
            create_subprocess_exec(
                pip_path, "install", "-r", req_path,
                stdout=PIPE, stderr=PIPE, cwd=pdir,
            ),
            timeout=300,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode == 0:
            return (True, "pip install success")
        else:
            return (False, f"pip install failed: {stderr.decode()[:300]}")
    except asyncio.TimeoutError:
        return (False, "pip install timed out")
    except Exception as e:
        return (False, f"pip error: {e}")


async def auto_restart_on_startup():
    """
    Bot restart hone ke baad — jo projects 'running' the database mein,
    pehle unke requirements install karo, phir unhe start karo.
    30 second wait karta hai taaki files pehle restore ho jayein.
    """
    await asyncio.sleep(30)  # restore aur venv setup ko thoda time do
    try:
        running_projects = await projects_col.find({
            "status": "running",
            "admin_stopped": {"$ne": True},
        }).to_list(length=10000)

        if not running_projects:
            logger.info("Auto-restart on startup: koi running project nahi mila.")
            return

        logger.info(f"Auto-restart on startup: {len(running_projects)} projects process ho rahe hain...")

        for proj in running_projects:
            uid  = proj["user_id"]
            name = proj["name"]
            try:
                # Stale PID saaf karo
                await projects_col.update_one(
                    {"user_id": uid, "name": name},
                    {"$set": {"status": "stopped", "pid": None}},
                )

                # User ko batao — requirements install ho raha hai
                if notification_bot:
                    try:
                        await notification_bot.send_message(
                            chat_id=uid,
                            text=(
                                f"🔄 *Bot Restarted*\n\n"
                                f"Project `{name}` requirements are currently being installing…\n"
                                f"⏳ your project will start automatically in a few moments."
                            ),
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass

                # Requirements install karo pehle
                logger.info(f"Installing requirements for {uid}:{name} before startup...")
                success, msg = await _install_requirements_for_project(uid, name)
                logger.info(f"Requirements for {uid}:{name}: {msg}")

                # Ab project start karo
                await asyncio.sleep(1)
                updated = await start_project_process(uid, name)
                logger.info(f"Auto-restarted on startup: {uid}:{name} PID={updated.get('pid')}")

                # User ko final status batao
                if notification_bot:
                    try:
                        req_status = "✅ Requirements installed" if success else f"⚠️ Requirements issue: {msg[:100]}"
                        await notification_bot.send_message(
                            chat_id=uid,
                            text=(
                                f"✅ *Project Started*\n\n"
                                f"Project: `{name}`\n"
                                f"{req_status}\n"
                                f"🟢 your project running now"
                            ),
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"Auto-restart on startup failed for {uid}:{name}: {e}")
                await projects_col.update_one(
                    {"user_id": uid, "name": name},
                    {"$set": {"status": "stopped", "pid": None}},
                )
                if notification_bot:
                    try:
                        await notification_bot.send_message(
                            chat_id=uid,
                            text=(
                                f"❌ *Project Start Failed*\n\n"
                                f"Project `{name}` bot restart ke baad start nahi ho saka.\n"
                                f"Error: `{str(e)[:200]}`\n\n"
                                f"Manually start karo bot se."
                            ),
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass

        logger.info("Auto-restart on startup complete.")
    except Exception as e:
        logger.error(f"auto_restart_on_startup failed: {e}")


async def setup_venvs_background():
    """Setup virtualenvs for all restored projects in background."""
    try:
        all_projects = await projects_col.find({}).to_list(length=10000)
        for proj in all_projects:
            uid  = proj["user_id"]
            name = proj["name"]
            pdir = project_dir(uid, name)
            venv_dir = os.path.join(pdir, "venv")

            if os.path.exists(pdir) and not os.path.exists(venv_dir):
                try:
                    proc = await create_subprocess_exec(
                        sys.executable, "-m", "venv", venv_dir,
                        stdout=PIPE, stderr=PIPE
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=120)

                    req_file = os.path.join(pdir, "requirements.txt")
                    pip_path = os.path.join(pdir, "venv", "bin", "pip")
                    if os.path.exists(req_file) and os.path.exists(pip_path):
                        proc2 = await create_subprocess_exec(
                            pip_path, "install", "-r", req_file, "--quiet",
                            stdout=PIPE, stderr=PIPE, cwd=pdir
                        )
                        await asyncio.wait_for(proc2.communicate(), timeout=300)
                    logger.info(f"Venv setup complete for {name}")
                except Exception as e:
                    logger.warning(f"Failed to setup venv for {name}: {e}")
    except Exception as e:
        logger.error(f"Background venv setup failed: {e}")

# ─────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────

def build_application() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    # New project conversation
    new_proj_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_new_project, pattern="^new_project$")],
        states={
            NEW_PROJECT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_project_name),
                CallbackQueryHandler(new_project_cancel, pattern="^back_start$"),
            ],
            NEW_PROJECT_FILES: [
                MessageHandler(filters.Document.ALL, new_project_file),
                CommandHandler("done", new_project_done_cmd),
                CallbackQueryHandler(new_project_done_cb, pattern="^upload_done$"),
                CallbackQueryHandler(new_project_cancel, pattern="^back_start$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", new_project_cancel),
            CommandHandler("start", new_project_cancel),
        ],
        per_chat=True,
    )

    # Edit run command conversation
    editcmd_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_editcmd_start, pattern=r"^editcmd:")],
        states={
            EDIT_RUN_CMD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, editcmd_receive),
                CallbackQueryHandler(admin_conv_cancel, pattern=r"^proj:"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin_conv_cancel),
            CommandHandler("start", admin_conv_cancel),
        ],
        per_chat=True,
    )

    # Env vars conversation (Feature 3)
    env_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_env_add_start,  pattern=r"^env_add:"),
            CallbackQueryHandler(cb_env_edit_start, pattern=r"^env_edit:"),
        ],
        states={
            ENV_ADD_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, env_add_key),
            ],
            ENV_ADD_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, env_add_value),
            ],
            ENV_EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, env_edit_value),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin_conv_cancel),
            CommandHandler("start", admin_conv_cancel),
        ],
        per_chat=True,
    )

    # Admin conversations (combined into one handler with different states)
    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_admin_give_premium,   pattern="^admin:give_premium$"),
            CallbackQueryHandler(cb_admin_remove_premium, pattern="^admin:remove_premium$"),
            CallbackQueryHandler(cb_admin_temp_premium,   pattern="^admin:temp_premium$"),
            CallbackQueryHandler(cb_admin_ban,            pattern="^admin:ban$"),
            CallbackQueryHandler(cb_admin_unban,          pattern="^admin:unban$"),
            CallbackQueryHandler(cb_admin_broadcast_all,  pattern="^admin:broadcast_all$"),
            CallbackQueryHandler(cb_admin_send_to_user,   pattern="^admin:send_to_user$"),
            CallbackQueryHandler(cb_admin_add_admin,      pattern="^admin:add_admin$"),
            CallbackQueryHandler(cb_admin_remove_admin,   pattern="^admin:remove_admin$"),
        ],
        states={
            ADMIN_GIVE_PREMIUM_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_give_premium_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_REMOVE_PREMIUM_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_remove_premium_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_TEMP_PREMIUM_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_temp_premium_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_TEMP_PREMIUM_DUR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_temp_premium_dur),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_BAN_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ban_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_UNBAN_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_unban_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_BROADCAST_MSG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_msg),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_SEND_USER_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_send_user_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_SEND_USER_MSG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_send_user_msg),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_ADD_ADMIN_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_admin_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
            ADMIN_REMOVE_ADMIN_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_remove_admin_id),
                CallbackQueryHandler(admin_conv_cancel, pattern="^admin_panel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin_conv_cancel),
            CommandHandler("start", admin_conv_cancel),
        ],
        per_chat=True,
    )

    # Register handlers (conversations first!)
    app.add_handler(new_proj_conv)
    app.add_handler(editcmd_conv)
    app.add_handler(env_conv)
    app.add_handler(admin_conv)

    app.add_handler(CommandHandler("start", start))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(cb_start,             pattern="^back_start$"))
    app.add_handler(CallbackQueryHandler(cb_my_projects,       pattern="^my_projects$"))
    app.add_handler(CallbackQueryHandler(cb_my_status,         pattern="^my_status$"))
    app.add_handler(CallbackQueryHandler(cb_bot_status,        pattern="^bot_status$"))
    app.add_handler(CallbackQueryHandler(cb_premium,           pattern="^premium$"))
    app.add_handler(CallbackQueryHandler(cb_admin_panel,       pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(cb_admin_user_list,   pattern=r"^admin:user_list:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_admin_running,     pattern="^admin:running$"))
    app.add_handler(CallbackQueryHandler(cb_admin_stop_project,pattern=r"^admin_stop:"))
    app.add_handler(CallbackQueryHandler(cb_admin_broadcast_menu, pattern="^admin:broadcast_menu$"))
    app.add_handler(CallbackQueryHandler(cb_admin_backup_now,  pattern="^admin:backup_now$"))
    app.add_handler(CallbackQueryHandler(cb_admin_delete_backups,         pattern="^admin:del_backups$"))
    app.add_handler(CallbackQueryHandler(cb_admin_delete_backups_confirm, pattern="^admin:del_backups_confirm$"))

    # ISSUE 2 NEW HANDLERS: All Projects, Run Project, Download Project
    app.add_handler(CallbackQueryHandler(cb_admin_all_projects,     pattern=r"^admin:all_projects:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_admin_run_project,      pattern=r"^admin_run:"))
    app.add_handler(CallbackQueryHandler(cb_admin_download_project, pattern=r"^admin_dl:"))

    app.add_handler(CallbackQueryHandler(cb_project_dashboard, pattern=r"^proj:"))
    app.add_handler(CallbackQueryHandler(cb_run,               pattern=r"^run:"))
    app.add_handler(CallbackQueryHandler(cb_stop,              pattern=r"^stop:"))
    app.add_handler(CallbackQueryHandler(cb_restart,           pattern=r"^restart:"))
    app.add_handler(CallbackQueryHandler(cb_logs,              pattern=r"^logs:"))
    app.add_handler(CallbackQueryHandler(cb_filemgr,           pattern=r"^filemgr:"))
    app.add_handler(CallbackQueryHandler(cb_delete_confirm,    pattern=r"^delete:[a-zA-Z0-9_]+$"))
    app.add_handler(CallbackQueryHandler(cb_delete_yes,        pattern=r"^delete_yes:"))

    # Feature 1 & 3 standalone handlers
    app.add_handler(CallbackQueryHandler(cb_toggle_auto_restart, pattern=r"^toggle_ar:"))
    app.add_handler(CallbackQueryHandler(cb_envvars,             pattern=r"^envvars:"))
    app.add_handler(CallbackQueryHandler(cb_env_delete,          pattern=r"^env_del:"))
    app.add_handler(CallbackQueryHandler(cb_reinstall_reqs,      pattern=r"^reinstall_reqs:"))

    return app


async def post_init(app: Application):
    global notification_bot
    notification_bot = app.bot

    await app.bot.set_my_commands([
        BotCommand("start",  "Start the bot"),
        BotCommand("done",   "Finish file upload"),
        BotCommand("cancel", "Cancel current action"),
    ])
    # Restore from backup first (before bot starts accepting updates)
    await restore_from_backup()
    # Start background tasks
    asyncio.create_task(process_monitor())
    asyncio.create_task(backup_task())
    asyncio.create_task(keep_alive_task())


def main():
    # Start Flask file manager in daemon thread
    from file_manager import start_flask
    import threading
    t = threading.Thread(target=start_flask, args=(PORT,), daemon=True)
    t.start()
    logger.info(f"Flask file manager started on port {PORT}")

    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
