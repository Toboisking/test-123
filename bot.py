import asyncio
import logging
import os
import re
import sys
import time

from datetime import date
import httpx
from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ghidra-bot")

SCRIPT_VERSION = "v4-gh"
log.info("ghidra-bot %s starting (GitHub Actions worker)", SCRIPT_VERSION)

import json
from pathlib import Path

env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT = int(os.environ.get("PORT", "8080"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "100"))
MAX_CONCURRENT_JOBS = 4
MAX_DAILY_FILES = 30
ADMIN_IDS = ["6684870256", "7251749429"]
ALLOWED_USERS = [u.strip() for u in os.environ.get("ALLOWED_USER_IDS", "").split(",") if u.strip()]

PENDING_REQUESTS = set()
ADMIN_STATE = {}  # {user_id: state_str}
ADMIN_TEMP_DATA = {}

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Saini920/Bottestgidra")
GITHUB_EVENT = os.environ.get("GITHUB_EVENT", "decompile-job")

from database import RepoDB
db = RepoDB(GITHUB_TOKEN, GITHUB_REPO)

def record_user_name(user):
    uid = str(user.id)
    name = user.first_name
    if user.username:
        name += f" (@{user.username})"
    if db.get_name(uid) != name:
        db.data["names"][uid] = name
        db.save()



def is_allowed(user_id: int) -> bool:
    uid = str(user_id)
    if uid in db.data["banned"]:
        return False
    return True

job_queue = asyncio.Queue()
PENDING_JOBS = {}
ACTIVE_JOBS = {}
active_jobs_timestamps = []
CANCELLED_JOBS = set()

from datetime import date, timedelta


def check_daily_limit(user_id: int) -> str | None:
    return None


async def enqueue_or_dispatch(msg, status, file_url: str = "", filename: str = "", tg_file_path: str = "", engine: str = "ghidra", file_id: str = "", is_premium: bool = False):
    user_id = str(msg.from_user.id) if msg and msg.from_user else ""
    now = time.time()
    active_jobs_timestamps[:] = [t for t in active_jobs_timestamps if now - t < 600]

    is_admin = user_id in ADMIN_IDS
    is_priority = is_admin or len(active_jobs_timestamps) < MAX_CONCURRENT_JOBS
    is_premium = True

    if is_priority or len(active_jobs_timestamps) < MAX_CONCURRENT_JOBS:
        active_jobs_timestamps.append(now)
        await send_to_job(msg, status, file_url, filename, tg_file_path, is_admin, engine, file_id, is_premium)
    else:
        pos = job_queue.qsize() + 1
        priority_label = "⚡ <b>Priority Fast-Lane Slot Granted!</b>\n" if is_priority else ""
        await status.edit_text(
            f"⏳ <b>Server Busy! Task Queued (#Position {pos})</b>\n"
            f"{priority_label}"
            f"All active worker slots ({MAX_CONCURRENT_JOBS}/{MAX_CONCURRENT_JOBS}) are occupied.\n"
            "Decompilation will start automatically as soon as a slot opens.",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop Processing", callback_data=f"stop_{status.message_id}")]])
        )
        await job_queue.put((msg, status, file_url, filename, tg_file_path, is_admin, engine, file_id, is_premium))


async def queue_worker_loop():
    while True:
        try:
            item = await job_queue.get()
            is_premium = False
            if len(item) == 9:
                msg, status, file_url, filename, tg_file_path, is_admin, engine, file_id, is_premium = item
            elif len(item) == 8:
                msg, status, file_url, filename, tg_file_path, is_admin, engine, file_id = item
            elif len(item) == 7:
                msg, status, file_url, filename, tg_file_path, is_admin, engine = item
                file_id = ""
            elif len(item) == 5:
                msg, status, file_url, filename, tg_file_path = item
                is_admin = False
                engine = "ghidra"
                file_id = ""
            else:
                raise ValueError("Invalid item in job queue")

            if status.message_id in CANCELLED_JOBS:
                CANCELLED_JOBS.remove(status.message_id)
                job_queue.task_done()
                continue
            
            now = time.time()
            active_jobs_timestamps[:] = [t for t in active_jobs_timestamps if now - t < 600]
            
            while len(active_jobs_timestamps) >= MAX_CONCURRENT_JOBS:
                await asyncio.sleep(5)
                now = time.time()
                active_jobs_timestamps[:] = [t for t in active_jobs_timestamps if now - t < 600]
            
            active_jobs_timestamps.append(now)
            await send_to_job(msg, status, file_url, filename, tg_file_path, is_admin, engine, file_id, is_premium)
            job_queue.task_done()
        except Exception as e:
            log.exception("Queue worker error", exc_info=e)
            await asyncio.sleep(1)


OVER_LIMIT_MSG = (
    "⚠️ <b>File size limit exceeded!</b>\n"
    "File is {size:.1f} MB — this exceeds the current limit.\n\n"
    "Limits:\n"
    "  • .so/.dex — Free 30 MB | Premium 100 MB\n"
    "  • APK/ZIP — Free 200 MB | Premium 500 MB\n\n"
    "Powered By @Ghostofhackers"
)


ACCESS_DENIED_MSG = (
    "🔒 <b>Access Denied</b>\n\n"
    "This bot is private and restricted to approved users only.\n"
    "Contact an Admin or click the button below to request access.\n\n"
    "👥 <b>Admins:</b> @Ghostofhackers"
)



FORCE_CHANNELS = []

async def check_force_join(update, context) -> bool:
    return True



async def reply_denied(msg, user_id: int = None) -> None:
    uid = str(user_id) if user_id else ""
    if uid and uid in PENDING_REQUESTS:
        text = (
            "⏳ <b>Access Request Pending</b>\n\n"
            "Your access request has been submitted to the Admins (@Ghostofhackers).\n"
            "Please wait for an Admin to review and approve your request."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Contact Admin", url="https://t.me/Ghostofhackers")]
        ])
    else:
        text = ACCESS_DENIED_MSG
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📩 Request Access", callback_data="req_access"),
                InlineKeyboardButton("👤 Contact Admin", url="https://t.me/Ghostofhackers"),
            ]
        ])
    await msg.reply_text(text, parse_mode=constants.ParseMode.HTML, reply_markup=keyboard)



async def handle_engine_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    parts = data.split("_")
    if len(parts) != 3: return
    engine = parts[1]
    job_id = parts[2]
    
    if job_id not in PENDING_JOBS:
        await query.edit_message_text("❌ This request has expired or is invalid.")
        return
        
    job = PENDING_JOBS.pop(job_id)
    await query.edit_message_text(f"🚀 Job submitted for {engine.capitalize()} engine! Sending to server...")
    await enqueue_or_dispatch(job["msg"], job["status"], job["file_url"], job["filename"], job["tg_file_path"], engine, job.get("file_id", ""))

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    record_user_name(user)
    data = query.data

    if data.startswith("stoprun_"):
        await query.answer("🛑 Stopping cloud job...", show_alert=False)
        try:
            run_id = int(data.split("_")[1])
            asyncio.create_task(cancel_github_run(run_id))
            await query.edit_message_text("❌ <b>Cloud job stopped.</b>", parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.warning("stoprun failed: %s", e)
        return

    if data.startswith("stop_"):
        await query.answer("🛑 Stopping job...", show_alert=False)
        try:
            msg_id = int(data.split("_")[1])
            chat_id = query.message.chat_id
            job_name = f"job-{chat_id}-{msg_id}"
            
            CANCELLED_JOBS.add(msg_id)
            asyncio.create_task(cancel_github_job(job_name))
            
            await query.edit_message_text("❌ <b>Job Cancelled by User.</b>", parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.warning("Cancel failed: %s", e)
        return

    if data == "buy_sub":
        await query.answer("🎉 Totally FREE — no subscription needed!", show_alert=False)
        sub_details = (
            "🎉 <b>GHIDRA DECOMPILER — 100% FREE FOREVER</b>\n"
            "═══════════════════════════════════\n"
            "💳 <b>PRICE:</b> <b>₹0 — COMPLETELY FREE</b>\n\n"
            "⚡ <b>EVERYONE GETS FULL ACCESS:</b>\n"
            "• 🚀 <b>Unlimited Files / Day</b>\n"
            "• 📦 <b>Max File Limits:</b> .so/.dex up to <b>100 MB</b> & APK/ZIP up to <b>500 MB</b>\n"
            "• ⭐ <b>No Subscription, No Payments, No Limits</b>\n"
            "• 📱 <b>APK Engines:</b> JADX (Java Source), dex2jar (JAR+Java), Apktool (XML/Smali) & Compilation Support\n"
            "• 🛠️ <b>Free Support</b>\n\n"
            "═══════════════════════════════════\n"
            "👑 <b>Powered By @Ghostofhackers</b>"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚀 Start Using Now", url="https://t.me/Ghostofhackers"),
            ]
        ])
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=sub_details,
                parse_mode=constants.ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception as e:
            log.warning("Could not send buy_sub message to user %s: %s", user.id, e)
        return

    if data == "req_access":
        uid = str(user.id)
        if uid in PENDING_REQUESTS:
            await query.answer("⏳ Your access request is already pending Admin approval!", show_alert=True)
            return
        PENDING_REQUESTS.add(uid)
        await query.answer("📩 Access request sent to Admins!", show_alert=True)
        try:
            await query.edit_message_text(
                "⏳ <b>Access Request Pending</b>\n\n"
                "Your access request has been submitted to the Admins (@Ghostofhackers).\n"
                "You will receive a notification as soon as an Admin approves your request.",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👤 Contact Admin", url="https://t.me/Ghostofhackers")]
                ])
            )
        except Exception:
            pass

        admin_text = (
            "🔔 <b>NEW ACCESS REQUEST</b>\n"
            "═══════════════════════\n"
            f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
            f"👤 <b>Name:</b> {user.full_name}\n"
            f"🌐 <b>Username:</b> @{user.username if user.username else 'N/A'}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"app_{user.id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"deny_{user.id}"),
            ]
        ])
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=int(admin_id), text=admin_text, parse_mode=constants.ParseMode.HTML, reply_markup=keyboard
                )
            except Exception as e:
                log.warning("Failed to send admin notification to %s: %s", admin_id, e)
        return

    if str(user.id) not in ADMIN_IDS:
        await query.answer("❌ Only Admins can perform this action!", show_alert=True)
        return

    if data.startswith("app_"):
        target_id = data.split("app_")[1]
        db.add_approved(target_id)
        PENDING_REQUESTS.discard(target_id)
        
        await query.answer("✅ User Approved!", show_alert=True)
        admin_name = f"@{user.username}" if user.username else user.first_name
        await query.edit_message_text(
            f"✅ <b>Approved User <code>{target_id}</code></b>\nApproved by {admin_name}",
            parse_mode=constants.ParseMode.HTML,
        )
        try:
            user_msg = (
                "🎉 <b>Access Approved!</b>\n\n"
                "Your request for bot access has been approved by the Admin.\n"
                "You can now send files or commands to start decompiling!"
            )
            await context.bot.send_message(chat_id=int(target_id), text=user_msg, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.warning("Could not notify user %s: %s", target_id, e)

    elif data.startswith("deny_"):
        target_id = data.split("deny_")[1]
        PENDING_REQUESTS.discard(target_id)
        await query.answer("❌ Request Declined!", show_alert=True)
        admin_name = f"@{user.username}" if user.username else user.first_name
        await query.edit_message_text(
            f"❌ <b>Declined User <code>{target_id}</code></b>\nDeclined by {admin_name}",
            parse_mode=constants.ParseMode.HTML,
        )
        try:
            user_msg = (
                "❌ <b>Access Denied</b>\n\n"
                "Your request for bot access was declined by the Admin."
            )
            await context.bot.send_message(chat_id=int(target_id), text=user_msg, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.warning("Could not notify user %s: %s", target_id, e)



async def cmd_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS: return
    cmd = update.message.text.split()[0].lower()
    
    if cmd == "/approved_users":
        users = db.data["approved"]
        title = "👥 Approved Users"
    elif cmd == "/unapproved_users":
        users = [] # Need to fetch from bot history or PENDING_REQUESTS? We only have pending.
        users = list(PENDING_REQUESTS)
        title = "⏳ Pending Users"
    elif cmd == "/ban_users" or cmd == "/banned_users":
        users = db.data["banned"]
        title = "🚫 Banned Users"
    elif cmd == "/premium_users":
        users = list(db.data["subscriptions"].keys())
        title = "⭐ Premium Users"
    else: return
    
    text = f"<b>{title} ({len(users)}):</b>\n"
    for u in users:
        name = db.get_name(u)
        if name == "Unknown":
            name = ""
        else:
            name = f" - {name}"
        text += f"• <code>{u}</code>{name}\n"
    if not users: text += "None found."
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context): return
    if not is_allowed(update.effective_user.id):
        await reply_denied(update.message, update.effective_user.id)
        return
    await update.message.reply_text(
        "🤖 Welcome to Ghidra Decompiler Bot!\n\n"
        "🔬 This bot runs <b>4 engines</b> on <b>Ghost'S Server</b> — 100% FREE, no size limits!\n\n"
        "⚙️ <b>Engines</b> (file bhejo → button pick karo):\n"
        "  • ☕️ <b>JADX</b> — APK ka Java source code nikalta hai 📄\n"
        "  • 📱 <b>Apktool</b> — APK ka Smali/XML nikalta hai 🧩\n"
        "  • 🔨 <b>Compile APK</b> — smali/XML ZIP se signed APK banata hai 📦\n"
        "  • ⚙️ <b>Ghidra</b> — native binaries ka C Code (NSA's RE framework) 🧠\n\n"
        "🎯 <b>Force one engine:</b> file caption ya /link mein pehle naam likho\n"
        "  → caption: <code>/jadx</code> or <code>/apktool</code> or <code>/apkbuild</code> or <code>/ghidra</code>\n"
        "  → link: <code>/link jadx https://x.com/app.apk</code>\n\n"
        "📦 <b>What you get back:</b>\n"
        "  • decompiled.c — full C code of every function 🧠\n"
        "  • info.txt — strings & file details 📊\n"
        "  • functions.txt — every function with address & size 📋\n"
        "  • imports.txt — imported/API functions 🔌\n"
        "  • symbols.txt — all symbols 🏷️\n"
        "  • Delivered as one neat ZIP file 📂\n\n"
        "═══════════════════════\n"
        "📤 <b>Method 1: Direct upload</b>\n"
        "Just send the file directly:\n"
        "  • .exe / .dll / .so / .elf / .apk / .zip\n"
        "  🚀 MTProto mode: unlimited size — send any big file directly!\n\n"
        "📤 <b>Method 2: Link method</b> (no size limit!)\n"
        "  Step 1: Upload file to Google Drive / MediaFire / Dropbox / GitHub / any host\n"
        "  Step 2: Copy the shareable link\n"
        "  Step 3: Send: <code>/link &lt;url&gt;</code>\n\n"
        "⚡️ <b>Features:</b>\n"
        "  • 4 engines — JADX (Java) + Apktool (smali) + Compile APK + Ghidra (C code)\n"
        "  • Simple engine picker — file/link bhejo, button se engine chuno\n"
        "  • Function-by-function C reconstruction\n"
        "  • ELF / PE / Mach-O / Android APK support\n"
        "  • Live progress animation (0-100%)\n"
        "  • 🐞 <b>GDB Debugger:</b> <code>/gdb info functions</code>, <code>/gdb disassemble main</code>\n\n"
        "🚀 Send a file or a link now! Powered By @Ghostofhackers",
        parse_mode=constants.ParseMode.HTML
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_allowed(update.effective_user.id):
        await reply_denied(update.message, update.effective_user.id)
        return

    admin_section = ""
    if user_id in ADMIN_IDS:
        admin_section = (
            "\n\n👑 <b>ADMIN COMMANDS:</b>\n"
            "• <code>/approve</code> — Approve user access (interactive or <code>/approve &lt;id&gt;</code>)\n"
            "• <code>/unapprove</code> — Revoke user access (interactive or <code>/unapprove &lt;id&gt;</code>)\n"
            "• <code>/ban</code> — Ban user from bot (interactive or <code>/ban &lt;id&gt;</code>)\n"
            "• <code>/unban</code> — Unban user (interactive or <code>/unban &lt;id&gt;</code>)\n"
            "• <code>/free</code> — Enable FREE mode (no approval needed for new users)\n"
            "• <code>/unfree</code> — Disable FREE mode (requires approval again)\n"
            "• <code>/setlimit</code> — Set custom limit & days (interactive or <code>/setlimit &lt;id&gt; &lt;limit&gt; &lt;days&gt;</code>)\n"
            "• <code>/broadcast</code> — Broadcast message to all users (interactive or <code>/broadcast &lt;msg&gt;</code>)\n"
            "• <code>/stats</code> — View complete admin system statistics\n"
            "• <code>/active</code> — View active cloud jobs (user details + stop button)\n"
            "\n👥 <b>USER LISTS:</b>\n"
            "• <code>/approved_users</code> — List all approved users\n"
            "• <code>/unapproved_users</code> — List pending approval requests\n"
            "• <code>/ban_users</code> — List all banned users\n"
            "• <code>/premium_users</code> — List premium subscribers\n"
        )

    help_text = (
        "🤖 <b>GHIDRA DECOMPILER BOT — HELP & COMMANDS</b>\n"
        "═══════════════════════════════════\n"
        "<b>Description:</b>\n"
        "This bot decompiles binary executables (.exe, .dll, .so, .elf, .apk, .zip) using dual Cloud Engines: NSA's <b>Ghidra Engine</b> (for C/C++ logic) and <b>Apktool</b> (for Android resources/Smali).\n\n"
        "📌 <b>USER COMMANDS:</b>\n"
        "• <code>/start</code> — Welcome guide and basic usage.\n"
        "• <code>/help</code> — View all commands and bot description.\n"
        "• <code>/profile</code> — View your profile and server stats.\n"
        "• <code>/myid</code> — Display your Telegram User ID.\n"
        "• <code>/gdb &lt;cmd&gt;</code> — real GDB debugger commands 🐞\n"
        f"{admin_section}\n\n"
        "🎉 <b>100% FREE — NO SUBSCRIPTION NEEDED:</b>\n"
        "• 🆓 <b>Unlimited files / day</b> — no daily quota\n"
        "• 📦 <b>Max File Limits:</b> .so/.dex ≤100 MB, APK/ZIP ≤500 MB\n"
        "• 🚀 <b>Batch Decompiler:</b> max 5 .so/.dex + 2 .apk per ZIP\n"
        "• 📱 <b>Apktool Engine:</b> Full APK Decompilation & Compilation Support\n"
        "• 🐞 <b>GDB Debugger:</b> live commands — info functions, disassemble & more\n\n"
        "📤 <b>DIRECT UPLOAD:</b>\n"
        "• Send any binary file directly in chat (.so/.dex ≤100 MB, APK/ZIP ≤500 MB).\n\n"
        "📊 <b>BOT LIMITS & RULES:</b>\n"
        "• <b>Upload Limits:</b> .so/.dex — 100 MB | APK/ZIP — 500 MB\n"
        "• <b>ZIP Content Rules:</b> max 5 .so/.dex & 2 .apk inside\n"
        "• <b>Server Concurrency:</b> Max 4 active jobs at a time\n\n"
        "🐞 <b>GDB EXAMPLES:</b>\n"
        "  → <code>/gdb info functions</code>\n"
        "  → <code>/gdb disassemble main</code>\n"
        "  → <code>/gdb info files;info functions</code>\n"
        "  → <code>/gdb info registers</code>\n"
        "  → <code>/gdb disassemble main &lt;url&gt;</code>\n\n"
        "🚀 <b>Send a file or a link now!</b>\n"
        "⚡ <i>Powered By @Ghostofhackers</i>"
    )
    await update.message.reply_text(help_text, parse_mode=constants.ParseMode.HTML)


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Your Telegram User ID:\n<code>{update.effective_user.id}</code>", parse_mode=constants.ParseMode.HTML)


async def cmd_gdb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await reply_denied(update.message, update.effective_user.id)
        return
    msg = update.message
    if not context.args:
        await msg.reply_text(
            "🐞 <b>GDB Debugger</b> — run real GDB commands on the Cloud Server!\n\n"
            "<b>Usage:</b> reply to a file (or add a URL at the end)\n"
            "→ <code>/gdb info functions</code>\n"
            "→ <code>/gdb disassemble main</code>\n"
            "→ <code>/gdb info files;info functions</code>\n"
            "→ <code>/gdb disassemble main https://site.com/app.bin</code>\n\n"
            "Supports GDB batch commands: <code>info … disassemble … x/… p … bt …</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    script = " ".join(context.args[0:])
    doc = getattr(msg, "document", None)
    if doc is None and msg.reply_to_message is not None:
        doc = msg.reply_to_message.document
    url = ""
    for a in context.args:
        if a.startswith(("http://", "https://")):
            url = a
            script = script.replace(a, "").strip()
            break
    if doc is None and not url:
        await msg.reply_text("❌ Reply to a file or add a URL: <code>/gdb disassemble main &lt;url&gt;</code>", parse_mode=constants.ParseMode.HTML)
        return
    is_admin = str(update.effective_user.id) in ADMIN_IDS
    if doc is not None:
        size_mb = (doc.file_size or 0) / (1024 * 1024)
        if size_mb > MAX_FILE_MB:
            await msg.reply_text(OVER_LIMIT_MSG.format(size=size_mb), parse_mode=constants.ParseMode.HTML)
            return
        status = await msg.reply_text("🐞 Running GDB… Connecting to Cloud Server.", parse_mode=constants.ParseMode.HTML)
        try:
            tg_file = await doc.get_file()
            tg_file_path = tg_file.file_path
            if not tg_file_path:
                await status.edit_text("❌ Could not get file path from Telegram. Try URL mode.")
                return
            await send_to_job(msg, status, filename=doc.file_name, tg_file_path=tg_file_path, is_admin=is_admin, engine="gdb", file_id=doc.file_id, gdb_script=script)
        except Exception as e:
            await status.edit_text("❌ File processing failed: " + str(e))
    else:
        filename = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "download"
        status = await msg.reply_text("🐞 Running GDB…")
        await send_to_job(msg, status, file_url=url, filename=str(filename), is_admin=is_admin, engine="gdb", gdb_script=script)


async def cancel_github_job(job_name: str):
    if not GITHUB_TOKEN: return
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ghidra-bot"
    }
    # Run names: job-/jadx-/dex2jar-/apktool-/build-{chat_id}-{message_id}
    chat_msg = job_name.rsplit("-", 2)
    if len(chat_msg) == 3:
        chat_id, msg_id = chat_msg[1], chat_msg[2]
        prefixes = ["job", "jadx", "dex2jar", "apktool", "build"]
    else:
        prefixes = [job_name]
    async with httpx.AsyncClient(timeout=30.0) as client:
        for status in ["in_progress", "queued"]:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?status={status}"
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    runs = resp.json().get("workflow_runs", [])
                    for run in runs:
                        rname = run.get("name", "")
                        for p in prefixes:
                            if rname.startswith(p + "-" + chat_id + "-") or rname == p + "-" + chat_id + "-" + msg_id:
                                run_id = run["id"]
                                await client.post(f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{run_id}/cancel", headers=headers)
                                log.info("Cancelled Github run %s for %s", run_id, job_name)
                                return
            except Exception as e:
                log.warning("Failed to cancel github job %s: %s", job_name, e)


async def cancel_github_run(run_id: int):
    if not GITHUB_TOKEN: return
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ghidra-bot"
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{run_id}/cancel", headers=headers)
        log.info("Cancelled Github run %s", run_id)
    except Exception as e:
        log.warning("Failed to cancel run %s: %s", run_id, e)


def get_report_url() -> str:
    base = (WEBHOOK_URL or "").strip().rstrip("/")
    if not base:
        rp = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if rp:
            base = ("https://" + rp) if not rp.startswith(("http://", "https://")) else rp.rstrip("/")
    return (base + "/internal/count") if base else ""


async def trigger_github(file_url: str, chat_id: int, message_id: int, filename: str, tg_file_path: str = "", is_admin: bool = False, event_type: str = GITHUB_EVENT, file_id: str = "", original_msg_id: int = 0, is_premium: bool = False, gdb_script: str = ""):
    if not GITHUB_TOKEN:
        return False, 0, "GITHUB_TOKEN env missing"
    client_payload = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "original_message_id": str(original_msg_id),
        "filename": filename,
        "bot_token": BOT_TOKEN,
        "is_admin": str(is_admin),
        "is_premium": str(is_premium),
        "file_id": file_id,
        "report_url": get_report_url(),
    }
    if gdb_script:
        client_payload["gdb_script"] = gdb_script
    if tg_file_path:
        client_payload["tg_file_path"] = tg_file_path
    else:
        client_payload["file_url"] = file_url
    payload = {"event_type": event_type, "client_payload": client_payload}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"https://api.github.com/repos/{GITHUB_REPO}/dispatches",
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "ghidra-bot",
                },
                json=payload,
            )
    except Exception as e:
        log.error("dispatch network error: %s", e)
        return False, 0, f"network error: {e}"
    log.info("dispatch repo=%s event=%s status=%s body=%s", GITHUB_REPO, event_type, resp.status_code, resp.text[:300])
    return resp.status_code in (204, 200), resp.status_code, resp.text[:300]


async def send_to_job(msg, status, file_url: str = "", filename: str = "", tg_file_path: str = "", is_admin: bool = False, engine: str = "ghidra", file_id: str = "", is_premium: bool = False, gdb_script: str = ""):
    if status.message_id in CANCELLED_JOBS:
        CANCELLED_JOBS.remove(status.message_id)
        return
        
    if not GITHUB_TOKEN:
        await status.edit_text(
            "❌ GitHub trigger failed: <b>GITHUB_TOKEN env missing</b> on Railway.\n"
            "Set it in Railway Dashboard → Variables, then Redeploy.\n"
            "Powered By @Ghostofhackers",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    if engine == "jadx":
        event_type = "decompile-jadx"
    elif engine == "dex2jar":
        event_type = "decompile-dex2jar"
    elif engine == "apktool":
        event_type = "decompile-apktool"
    elif engine == "apktool-build":
        event_type = "compile-apktool"
    elif engine == "gdb":
        event_type = "decompile-job"
    else:
        event_type = "decompile-job"
        
    user_id = str(msg.from_user.id) if msg and msg.from_user else ""
    ok, code, body = await trigger_github(file_url, msg.chat_id, status.message_id, filename, tg_file_path, is_admin, event_type, file_id, msg.message_id, is_premium, gdb_script)
    if not ok:
        await status.edit_text(
            "❌ GitHub trigger failed (HTTP <code>{code}</code>).\n"
            "Repo: <code>{repo}</code>\n"
            "Response: <code>{body}</code>\n\n"
            "Fix: Railway → Variables → check <code>GITHUB_TOKEN</code> (repo scope) "
            "and <code>GITHUB_REPO</code> (should be <code>Toboisking/test-123</code>), then Redeploy.".format(
                code=code, repo=GITHUB_REPO, body=body
            ),
            parse_mode=constants.ParseMode.HTML,
        )
        return
    u = msg.from_user if msg and msg.from_user else None
    ACTIVE_JOBS[status.message_id] = {
        "user_id": user_id,
        "username": (u.username or "") if u else "",
        "name": (u.full_name or "") if u else "",
        "chat_id": str(msg.chat_id) if msg else "",
        "message_id": status.message_id,
        "filename": filename,
        "engine": engine,
        "started": time.time(),
    }
    await status.edit_text(
        "Job sent to server!\n"
        "⏱️ Expected: 2-10 minutes.\n"
        "▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱ 0.00 %",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop Processing", callback_data=f"stop_{status.message_id}")]])
    )


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context): return
    if not is_allowed(update.effective_user.id):
        await reply_denied(update.message, update.effective_user.id)
        return

    msg = update.message
    doc = msg.document
    if doc is None:
        await msg.reply_text("📄 Send a file (document) — EXE, DLL, SO, ELF, APK etc.")
        return

    err = check_daily_limit(update.effective_user.id)
    if err:
        await msg.reply_text(err, parse_mode=constants.ParseMode.HTML)
        return

    user_id = str(update.effective_user.id)
    is_premium = True
    
    fname_l = (doc.file_name or "").lower()
    is_small_type = fname_l.endswith((".so", ".dex"))
    user_max_mb = 100 if is_small_type else 500

    size_mb = (doc.file_size or 0) / (1024 * 1024)
    if size_mb > user_max_mb:
        await msg.reply_text(OVER_LIMIT_MSG.format(size=size_mb), parse_mode=constants.ParseMode.HTML)
        return

    status = await msg.reply_text("🚀 File received! Sending to server...")

    try:
        file_id = doc.file_id
        tg_file_path = ""
        try:
            tg_file = await doc.get_file()
            tg_file_path = tg_file.file_path
        except Exception as e:
            if "too big" in str(e).lower():
                log.info(f"File {file_id} is too big for HTTP API. Using MTProto fallback.")
            else:
                await status.edit_text("❌ Could not get file from Telegram.")
                return

        user_id = str(update.effective_user.id)
        is_premium = True

        import uuid
        job_id = str(uuid.uuid4())[:8]
        PENDING_JOBS[job_id] = {"msg": msg, "status": status, "filename": doc.file_name, "tg_file_path": tg_file_path, "file_url": "", "file_id": file_id}
        
        if doc.file_name and doc.file_name.lower().endswith(".smali"):
            btn_jadx = InlineKeyboardButton("☕ Smali → Java", callback_data=f"engine_jadx_{job_id}")
            await status.edit_text(
                "☕ <b>Smali File Detected!</b>\nConvert Smali to readable Java source?\n\n"
                "• ☕ <b>Smali → Java (JADX):</b> Decompile Smali to Java",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[btn_jadx]])
            )
        elif doc.file_name and doc.file_name.lower().endswith(".dex"):
            btn_jadx = InlineKeyboardButton("☕ Decompile (Java)", callback_data=f"engine_jadx_{job_id}")
            btn_d2j = InlineKeyboardButton("🧬 Decompile + Java", callback_data=f"engine_dex2jar_{job_id}")
            await status.edit_text(
                "🧬 <b>DEX File Detected!</b>\nChoose how to process:\n\n"
                "• ☕ <b>Decompile:</b> classes.dex → Java Source (JADX)\n"
                "• 🧬 <b>Decompile + Java:</b> classes.dex → JAR + Java Source (dex2jar + CFR)",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [btn_jadx],
                    [btn_d2j]
                ])
            )
        elif doc.file_name and doc.file_name.lower().endswith(".apk"):
            btn_jadx = InlineKeyboardButton("☕ JADX (Java Source)", callback_data=f"engine_jadx_{job_id}")
            btn_dex2jar = InlineKeyboardButton("🧬 dex2jar (JAR+Java)", callback_data=f"engine_dex2jar_{job_id}")
            btn_apktool = InlineKeyboardButton("📱 Apktool (XML/Smali)", callback_data=f"engine_apktool_{job_id}")

            await status.edit_text(
                "🤖 <b>APK Detected!</b>\nChoose your processing engine:\n\n"
                "• ☕ <b>JADX:</b> APK → Java Source\n"
                "• 🧬 <b>dex2jar:</b> APK → JAR + Java Source\n"
                "• 📱 <b>Apktool:</b> Decompile APKs\n"
                "• ⚙️ <b>Ghidra:</b> Decompile binaries & ZIPs",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [btn_jadx, btn_dex2jar],
                    [btn_apktool],
                    [InlineKeyboardButton("⚙️ Ghidra (C Code)", callback_data=f"engine_ghidra_{job_id}")]
                ])
            )
        elif doc.file_name and doc.file_name.lower().endswith(".zip"):
            btn_build = InlineKeyboardButton("🔨 Compile APK (Apktool Build)", callback_data=f"engine_apktool-build_{job_id}")
            btn_jadx = InlineKeyboardButton("☕ JADX (Java/Smali)", callback_data=f"engine_jadx_{job_id}")
            btn_d2j = InlineKeyboardButton("🧬 dex2jar (JAR+Java)", callback_data=f"engine_dex2jar_{job_id}")

            await status.edit_text(
                "🤖 <b>ZIP Archive Detected!</b>\nChoose processing engine:\n\n"
                "• ⚙️ <b>Ghidra:</b> Decompile binaries inside ZIP\n"
                "• ☕ <b>JADX:</b> Decompile Java/Smali to source\n"
                "• 🧬 <b>dex2jar:</b> DEX → JAR + Java Source\n"
                "• 🔨 <b>Compile APK:</b> Build APK from decompiled ZIP",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚙️ Ghidra (Decompile binaries)", callback_data=f"engine_ghidra_{job_id}")],
                    [btn_jadx, btn_d2j],
                    [btn_build]
                ])
            )
        else:
            await status.edit_text("📥 <b>Downloading to Cloud Server...</b>\n⏳ Processing with Ghidra Engine...", parse_mode="HTML")
            await enqueue_or_dispatch(msg, status, filename=doc.file_name, tg_file_path=tg_file_path, engine="ghidra", file_id=file_id)
    except Exception as e:
        await status.edit_text("❌ File processing failed: " + str(e))


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await reply_denied(update.message, user.id)
        return

    today = date.today()
    uid_str = str(user.id)
    sub = db.data["subscriptions"].get(uid_str)

    daily_max = "Unlimited"
    sub_info = "🎉 <b>Plan:</b> 100% Free — No Subscription Needed\n"

    used_today = record["count"] if ((record := db.data['daily_usage'].get(uid_str)) and record["date"] == today.isoformat()) else 0
    remaining = "Unlimited"
    used_display = f"{used_today} / Unlimited"
    upload_display = "100 MB (.so/.dex) | 500 MB (APK/ZIP)"

    now = time.time()
    active_now = len([t for t in active_jobs_timestamps if now - t < 600])
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
                r1 = await client.get(f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?status=in_progress", headers=headers)
                r2 = await client.get(f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?status=queued", headers=headers)
                if r1.status_code == 200 and r2.status_code == 200:
                    active_now = r1.json().get("total_count", 0) + r2.json().get("total_count", 0)
        except Exception:
            pass

    profile_text = (
        "👤 <b>USER PROFILE</b>\n"
        "═══════════════════════════════════\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"👤 <b>Name:</b> {user.full_name}\n"
        f"🌐 <b>Username:</b> @{user.username if user.username else 'N/A'}\n"
        f"✅ <b>Status:</b> Free User — Total Access\n"
        f"{sub_info}\n"
        "📊 <b>USAGE & LIMITS</b>\n"
        "───────────────────────\n"
        f"📅 <b>Today's Files Used:</b> {used_display}\n"
        f"🔄 <b>Remaining Today:</b> {remaining}\n"
        f"⚡ <b>Max Direct Upload:</b> {upload_display}\n"
        f"⚙️ <b>Server Active Jobs:</b> {active_now} / {MAX_CONCURRENT_JOBS}\n"
        f"⏳ <b>Queued Jobs:</b> {job_queue.qsize()}\n\n"
        "⚡ <i>Powered By @Ghostofhackers</i>"
    )
    await update.message.reply_text(
        profile_text,
        parse_mode=constants.ParseMode.HTML
    )


async def handle_admin_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in ADMIN_IDS or user_id not in ADMIN_STATE:
        return

    state = ADMIN_STATE.pop(user_id)
    text = update.message.text.strip()

    if state == "AWAITING_APPROVE":
        target_id = text
        db.add_approved(target_id)
        PENDING_REQUESTS.discard(target_id)
        
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been approved.", parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(chat_id=int(target_id), text="🎉 <b>Access Approved!</b>\nYour request for bot access has been approved by the Admin.", parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass

    elif state == "AWAITING_UNAPPROVE":
        target_id = text
        db.remove_approved(target_id)
        
        await update.message.reply_text(f"❌ User <code>{target_id}</code> has been unapproved/revoked.", parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(chat_id=int(target_id), text="🔒 <b>Access Revoked</b>\nYour access to the bot has been revoked by the Admin.", parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass

    elif state == "AWAITING_BAN":
        target_id = text
        db.ban(target_id)
        db.remove_approved(target_id)
        
        await update.message.reply_text(f"🚫 User <code>{target_id}</code> has been banned.", parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(chat_id=int(target_id), text="🚫 <b>Account Banned</b>\nYou have been banned from using this bot.", parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass

    elif state == "AWAITING_UNBAN":
        target_id = text
        db.unban(target_id)
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been unbanned.", parse_mode=constants.ParseMode.HTML)

    elif state == "AWAITING_BROADCAST":
        broadcast_msg = text
        target_users = set(db.data["approved"] + list(db.data['daily_usage'].keys()))
        sent, failed = 0, 0
        status_msg = await update.message.reply_text("📢 Broadcasting message...")
        for uid in target_users:
            try:
                await context.bot.send_message(
                    chat_id=int(uid),
                    text=f"📢 <b>ANNOUNCEMENT:</b>\n\n{broadcast_msg}",
                    parse_mode=constants.ParseMode.HTML,
                )
                sent += 1
            except Exception:
                failed += 1
        await status_msg.edit_text(
            f"✅ <b>Broadcast Complete!</b>\n"
            f"📤 Sent: {sent}\n"
            f"❌ Failed: {failed}",
            parse_mode=constants.ParseMode.HTML,
        )

    elif state == "AWAITING_SETLIMIT_USERID":
        ADMIN_TEMP_DATA[user_id] = {"target_id": text}
        ADMIN_STATE[user_id] = "AWAITING_SETLIMIT_LIMIT"
        await update.message.reply_text("📊 Please send the <b>Daily File Limit</b> (e.g. 50):", parse_mode=constants.ParseMode.HTML)

    elif state == "AWAITING_SETLIMIT_LIMIT":
        try:
            limit_val = int(text)
            ADMIN_TEMP_DATA[user_id]["daily_limit"] = limit_val
            ADMIN_STATE[user_id] = "AWAITING_SETLIMIT_DAYS"
            await update.message.reply_text("📅 Please send the <b>Validity Period in Days</b> (e.g. 30):", parse_mode=constants.ParseMode.HTML)
        except ValueError:
            await update.message.reply_text("❌ Invalid limit number! Please enter a valid number (e.g. 50):")
            ADMIN_STATE[user_id] = "AWAITING_SETLIMIT_LIMIT"

    elif state == "AWAITING_SETLIMIT_DAYS":
        try:
            days_val = int(text)
            temp = ADMIN_TEMP_DATA.pop(user_id, {})
            target_id = temp.get("target_id")
            daily_limit = temp.get("daily_limit", MAX_DAILY_FILES)
            exp_date = (date.today() + timedelta(days=days_val)).isoformat()

            db.set_sub(target_id, exp_date, daily_limit)
            db.add_approved(target_id)

            await update.message.reply_text(
                f"✅ <b>Custom Limit Set Successfully!</b>\n\n"
                f"👤 <b>User ID:</b> <code>{target_id}</code>\n"
                f"📊 <b>Daily Quota:</b> {daily_limit} files/day\n"
                f"📅 <b>Validity:</b> {days_val} days\n"
                f"⏳ <b>Expires On:</b> {exp_date}",
                parse_mode=constants.ParseMode.HTML,
            )
            try:
                await context.bot.send_message(
                    chat_id=int(target_id),
                    text=(
                        "🎉 <b>YOUR SUBSCRIPTION HAS BEEN UPDATED!</b>\n"
                        "═══════════════════════════════════\n"
                        f"🆔 <b>User ID:</b> <code>{target_id}</code>\n"
                        f"📊 <b>Daily Quota:</b> <b>{daily_limit} files / day</b>\n"
                        f"📅 <b>Validity Duration:</b> <b>{days_val} Days</b>\n"
                        f"⏳ <b>Expires On:</b> <b>{exp_date}</b>\n\n"
                        "🚀 Enjoy full access to Ghidra Reverse Engineering Engine!\n"
                        "👥 <b>Support Admins:</b> @Ghostofhackers"
                    ),
                    parse_mode=constants.ParseMode.HTML,
                )
            except Exception:
                pass
        except ValueError:
            await update.message.reply_text("❌ Invalid days number! Please enter a valid number of days (e.g. 30):")
            ADMIN_STATE[user_id] = "AWAITING_SETLIMIT_DAYS"



async def cmd_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        return
    db.data["free_mode"] = True
    db.save()
    await update.message.reply_text("✅ <b>Bot is now in FREE mode!</b>\nAll users can now use the bot without needing approval.", parse_mode="HTML")

async def cmd_unfree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        return
    db.data["free_mode"] = False
    db.save()
    await update.message.reply_text("❌ <b>Bot is NO LONGER in free mode.</b>\nNew users will need to request approval again. Previously approved users will continue working fine.", parse_mode="HTML")

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if context.args:
        target_id = context.args[0].strip()
        db.add_approved(target_id)
        PENDING_REQUESTS.discard(target_id)
        
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been approved.", parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(chat_id=int(target_id), text="🎉 <b>Access Approved!</b>\nYour request for bot access has been approved by the Admin.", parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass
    else:
        ADMIN_STATE[uid] = "AWAITING_APPROVE"
        await update.message.reply_text("📝 Please send the <b>User ID</b> you want to approve:", parse_mode=constants.ParseMode.HTML)


async def cmd_unapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if context.args:
        target_id = context.args[0].strip()
        db.remove_approved(target_id)
        
        await update.message.reply_text(f"❌ User <code>{target_id}</code> has been unapproved/revoked.", parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(chat_id=int(target_id), text="🔒 <b>Access Revoked</b>\nYour access to the bot has been revoked by the Admin.", parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass
    else:
        ADMIN_STATE[uid] = "AWAITING_UNAPPROVE"
        await update.message.reply_text("📝 Please send the <b>User ID</b> you want to unapprove:", parse_mode=constants.ParseMode.HTML)


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if context.args:
        target_id = context.args[0].strip()
        db.ban(target_id)
        db.remove_approved(target_id)
        
        await update.message.reply_text(f"🚫 User <code>{target_id}</code> has been banned.", parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(chat_id=int(target_id), text="🚫 <b>Account Banned</b>\nYou have been banned from using this bot.", parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass
    else:
        ADMIN_STATE[uid] = "AWAITING_BAN"
        await update.message.reply_text("📝 Please send the <b>User ID</b> you want to ban:", parse_mode=constants.ParseMode.HTML)


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if context.args:
        target_id = context.args[0].strip()
        db.unban(target_id)
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been unbanned.", parse_mode=constants.ParseMode.HTML)
    else:
        ADMIN_STATE[uid] = "AWAITING_UNBAN"
        await update.message.reply_text("📝 Please send the <b>User ID</b> you want to unban:", parse_mode=constants.ParseMode.HTML)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if context.args:
        broadcast_msg = update.message.text.split(None, 1)[1]
        target_users = set(db.data["approved"] + list(db.data['daily_usage'].keys()))
        sent, failed = 0, 0
        status_msg = await update.message.reply_text("📢 Broadcasting message...")
        for tu in target_users:
            try:
                await context.bot.send_message(
                    chat_id=int(tu),
                    text=f"📢 <b>ANNOUNCEMENT:</b>\n\n{broadcast_msg}",
                    parse_mode=constants.ParseMode.HTML,
                )
                sent += 1
            except Exception:
                failed += 1
        await status_msg.edit_text(
            f"✅ <b>Broadcast Complete!</b>\n"
            f"📤 Sent: {sent}\n"
            f"❌ Failed: {failed}",
            parse_mode=constants.ParseMode.HTML,
        )
    else:
        ADMIN_STATE[uid] = "AWAITING_BROADCAST"
        await update.message.reply_text("📢 Please send the <b>Broadcast message text</b> you want to send to all users:", parse_mode=constants.ParseMode.HTML)


async def cmd_setlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return

    if len(context.args) >= 3:
        target_id = context.args[0].strip()
        try:
            daily_limit = int(context.args[1].strip())
            days_val = int(context.args[2].strip())
            exp_date = (date.today() + timedelta(days=days_val)).isoformat()

            db.set_sub(target_id, exp_date, daily_limit)
            db.add_approved(target_id)

            await update.message.reply_text(
                f"✅ <b>Custom Limit Set Successfully!</b>\n\n"
                f"👤 <b>User ID:</b> <code>{target_id}</code>\n"
                f"📊 <b>Daily Quota:</b> {daily_limit} files/day\n"
                f"📅 <b>Validity:</b> {days_val} days\n"
                f"⏳ <b>Expires On:</b> {exp_date}",
                parse_mode=constants.ParseMode.HTML,
            )
            try:
                await context.bot.send_message(
                    chat_id=int(target_id),
                    text=(
                        "🎉 <b>YOUR SUBSCRIPTION HAS BEEN UPDATED!</b>\n"
                        "═══════════════════════════════════\n"
                        f"🆔 <b>User ID:</b> <code>{target_id}</code>\n"
                        f"📊 <b>Daily Quota:</b> <b>{daily_limit} files / day</b>\n"
                        f"📅 <b>Validity Duration:</b> <b>{days_val} Days</b>\n"
                        f"⏳ <b>Expires On:</b> <b>{exp_date}</b>\n\n"
                        "🚀 Enjoy full access to Ghidra Reverse Engineering Engine!\n"
                        "👥 <b>Support Admins:</b> @Ghostofhackers"
                    ),
                    parse_mode=constants.ParseMode.HTML,
                )
            except Exception:
                pass
        except ValueError:
            await update.message.reply_text("❌ Invalid parameters! Usage: /setlimit <user_id> <daily_limit> <days>")
    else:
        ADMIN_STATE[uid] = "AWAITING_SETLIMIT_USERID"
        await update.message.reply_text("📝 Please send the <b>User ID</b> you want to set custom limits for:", parse_mode=constants.ParseMode.HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    now = time.time()
    active_now = len([t for t in active_jobs_timestamps if now - t < 600])
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
                r1 = await client.get(f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?status=in_progress", headers=headers)
                r2 = await client.get(f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?status=queued", headers=headers)
                if r1.status_code == 200 and r2.status_code == 200:
                    active_now = r1.json().get("total_count", 0) + r2.json().get("total_count", 0)
        except Exception:
            pass
    today_iso = date.today().isoformat()
    today_files = sum(rec["count"] for rec in db.data['daily_usage'].values() if rec.get("date") == today_iso)
    stats_text = (
        "📊 <b>ADMIN SYSTEM STATS</b>\n"
        "═══════════════════════\n"
        f"🌍 <b>Total Users (Ever):</b> {len(db.data['names'])}\n"
        f"👥 <b>Approved Users:</b> {len(db.data['approved'])}\n"
        f"🚫 <b>Banned Users:</b> {len(db.data['banned'])}\n"
        f"⭐ <b>Custom Subscriptions:</b> {len(db.data['subscriptions'])}\n"
        f"📅 <b>Total Files Processed:</b> {db.data.get('total_files', 0)}\n"
        f"📅 <b>Total Files Processed Today:</b> {today_files}\n"
        f"⚙️ <b>Active Cloud Jobs:</b> {active_now} / {MAX_CONCURRENT_JOBS}\n"
        f"⏳ <b>Queued Jobs:</b> {job_queue.qsize()}\n\n"
        "⚡ <i>Powered By @Ghostofhackers</i>"
    )
    await update.message.reply_text(stats_text, parse_mode=constants.ParseMode.HTML)


def parse_run_name(run_name: str):
    import re as _re
    m = _re.match(r"^(job|jadx|dex2jar|apktool|build)-(\d+)-(\d+)$", run_name or "")
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


ENGINE_LABELS = {"job": "🐉 Ghidra", "jadx": "☕ JADX", "dex2jar": "🧬 dex2jar", "apktool": "📱 Apktool", "build": "⚒️ Apktool Build"}
TASK_LABELS = {
    "ghidra": "Reverse Engineering / Decompile Binary (Ghidra)",
    "jadx": "Decompile to Java Source (JADX)",
    "dex2jar": "Decompile to JAR + Java (dex2jar + CFR)",
    "apktool": "APK Decompile - XML/Smali (Apktool)",
    "apktool-build": "APK Compile / Build (Apktool)",
}


async def cmd_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return

    now = time.time()
    for mid in list(ACTIVE_JOBS.keys()):
        if now - ACTIVE_JOBS[mid].get("started", 0) > 3600:
            del ACTIVE_JOBS[mid]

    runs = []
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
            async with httpx.AsyncClient(timeout=10) as client:
                for status in ["in_progress", "queued"]:
                    r = await client.get(f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?status={status}", headers=headers)
                    if r.status_code == 200:
                        for run in r.json().get("workflow_runs", []):
                            runs.append((run["id"], run.get("name", ""), status))
        except Exception as e:
            log.warning("cmd_active github query failed: %s", e)

    if not runs:
        await update.message.reply_text("⚙️ <b>Active Cloud Jobs</b>\n══════════════\n\n✅ No active jobs right now.", parse_mode=constants.ParseMode.HTML)
        return

    lines = ["⚙️ <b>ACTIVE CLOUD JOBS</b>", "═══════════════════════════"]
    buttons = []
    for idx, (run_id, run_name, status) in enumerate(runs, 1):
        parsed = parse_run_name(run_name)
        job = ACTIVE_JOBS.get(int(parsed[2])) if parsed else None
        if job:
            engine_label = {"ghidra": "🐉 Ghidra", "jadx": "☕ JADX", "dex2jar": "🧬 dex2jar", "apktool": "📱 Apktool", "apktool-build": "⚒️ Apktool Build"}.get(job.get("engine", ""), "🔧 Unknown")
        else:
            engine_label = ENGINE_LABELS.get(parsed[0], "🔧 Unknown") if parsed else "🔧 Unknown"
        user_id = (job or {}).get("user_id", "?")
        username = (job or {}).get("username", "")
        name = (job or {}).get("name", "")
        filename = (job or {}).get("filename", "?")
        status_icon = "🟢 Running" if status == "in_progress" else "⏳ Queued"
        user_line = f"🆔 <code>{user_id}</code>"
        if username:
            user_line += f" | <b>@{username}</b>"
        if name:
            user_line += f" | {name}"
        if not job:
            user_line += " | <i>(unknown - bot restarted)</i>"
        task_label = TASK_LABELS.get((job or {}).get("engine", ""), "") if job else ""
        task_line = f"\n   🛠️ <b>Task:</b> {task_label}" if task_label else ""
        lines.append(
            f"\n{idx}. {status_icon} — {engine_label}\n"
            f"   {user_line}\n"
            f"   📄 <code>{filename}</code>{task_line}"
        )
        buttons.append([InlineKeyboardButton(f"🛑 Stop #{idx} ({engine_label.split()[1]})", callback_data=f"stoprun_{run_id}")])

    text = "\n".join(lines)
    await update.message.reply_text(
        text,
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def subscription_checker_loop(app: Application):
    while True:
        try:
            today = date.today()
            changed = False
            for uid, sub in list(db.data["subscriptions"].items()):
                try:
                    exp_date = date.fromisoformat(sub["expires_at"])
                    days_left = (exp_date - today).days

                    # 5 Days Warning
                    if 1 < days_left <= 5 and not sub.get("warned_5"):
                        sub["warned_5"] = True
                        changed = True
                        msg_text = (
                            "⚠️ <b>SUBSCRIPTION EXPIRY WARNING</b>\n"
                            "═══════════════════════════════════\n"
                            f"Your bot subscription will expire in <b>{days_left} days</b> (Expires: <code>{exp_date}</code>).\n\n"
                            "⚠️ Please contact an Admin to renew your subscription so you don't lose access!\n"
                            "👥 <b>Admins:</b> @Ghostofhackers"
                        )
                        try:
                            await app.bot.send_message(
                                chat_id=int(uid),
                                text=msg_text,
                                parse_mode=constants.ParseMode.HTML,
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("👤 Contact Admin to Renew", url="https://t.me/Ghostofhackers")]
                                ])
                            )
                        except Exception as e:
                            log.warning("Failed to send 5-day warning to %s: %s", uid, e)

                    # 1 Day Warning (24h before expiry)
                    elif 0 <= days_left <= 1 and not sub.get("warned_1"):
                        sub["warned_1"] = True
                        changed = True
                        msg_text = (
                            "🚨 <b>URGENT: SUBSCRIPTION EXPIRING TOMORROW!</b>\n"
                            "═══════════════════════════════════\n"
                            f"Your bot subscription will expire in <b>{max(1, days_left)} day</b> (Expires: <code>{exp_date}</code>).\n\n"
                            "⚠️ Contact Admin to renew immediately so you don't lose access!\n"
                            "👥 <b>Admins:</b> @Ghostofhackers"
                        )
                        try:
                            await app.bot.send_message(
                                chat_id=int(uid),
                                text=msg_text,
                                parse_mode=constants.ParseMode.HTML,
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("🔄 Renew Subscription", url="https://t.me/Ghostofhackers")]
                                ])
                            )
                        except Exception as e:
                            log.warning("Failed to send 1-day warning to %s: %s", uid, e)

                except Exception as e:
                    log.warning("Error checking subscription for %s: %s", uid, e)

            if changed:
                db.save()
        except Exception as e:
            log.exception("Error in subscription_checker_loop", exc_info=e)

        await asyncio.sleep(21600)  # Check every 6 hours


async def weekly_analytics_loop(app: Application):
    while True:
        await asyncio.sleep(604800)  # Every 7 days
        try:
            today = date.today()
            today_iso = today.isoformat()
            today_files = sum(rec["count"] for rec in db.data['daily_usage'].values() if rec.get("date") == today_iso)
            report_text = (
                "📈 <b>AUTOMATED WEEKLY ADMIN ANALYTICS REPORT</b>\n"
                "═══════════════════════════════════\n"
                f"🌍 <b>Total Users (Ever):</b> {len(db.data['names'])}\n"
                f"👥 <b>Total Approved Users:</b> {len(db.data['approved'])}\n"
                f"⭐ <b>Custom Subscribers:</b> {len(db.data['subscriptions'])}\n"
                f"🚫 <b>Banned Users:</b> {len(db.data['banned'])}\n"
                f"📅 <b>Today's Files Processed:</b> {today_files}\n"
                "⚙️ <b>Server Health:</b> 100% Operational 🔥\n\n"
                "⚡ <i>Powered By @Ghostofhackers</i>"
            )
            for admin_id in ADMIN_IDS:
                try:
                    await app.bot.send_message(
                        chat_id=int(admin_id),
                        text=report_text,
                        parse_mode=constants.ParseMode.HTML,
                    )
                except Exception as e:
                    log.warning("Failed to send weekly report to %s: %s", admin_id, e)
        except Exception as e:
            log.exception("Error in weekly_analytics_loop", exc_info=e)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        log.warning("Telegram 409 Conflict: %s", context.error)
        return
    log.exception("Handler error", exc_info=context.error)



async def cleanup_workflows_loop(app: Application):
    while True:
        try:
            if GITHUB_TOKEN and GITHUB_REPO:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    headers = {
                        "Authorization": f"Bearer {GITHUB_TOKEN}",
                        "Accept": "application/vnd.github+json"
                    }
                    r = await client.get(
                        f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs",
                        headers=headers
                    )
                    if r.status_code == 200:
                        runs = r.json().get("workflow_runs", [])
                        for run in runs:
                            if run.get("status") == "completed":
                                await client.delete(
                                    f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{run['id']}",
                                    headers=headers
                                )
        except Exception as e:
            pass # Silent failure to avoid spamming logs if there's an issue
        await asyncio.sleep(60)  # Check every 60 seconds


async def post_init(app: Application):
    asyncio.create_task(queue_worker_loop())
    asyncio.create_task(weekly_analytics_loop(app))
    asyncio.create_task(cleanup_workflows_loop(app))


def main():
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN env is not set!")
        sys.exit(1)

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    
    app.add_handler(CommandHandler("approved_users", cmd_list_users))
    app.add_handler(CommandHandler("unapproved_users", cmd_list_users))
    app.add_handler(CommandHandler("ban_users", cmd_list_users))
    app.add_handler(CommandHandler("banned_users", cmd_list_users))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("gdb", cmd_gdb))
    app.add_handler(CommandHandler("free", cmd_free))
    app.add_handler(CommandHandler("unfree", cmd_unfree))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("unapprove", cmd_unapprove))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("active", cmd_active))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text_message))
    app.add_handler(MessageHandler(filters.ATTACHMENT, handle_file))
    app.add_handler(CallbackQueryHandler(handle_engine_choice, pattern="^engine_"))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_error_handler(error_handler)

    webhook_url = WEBHOOK_URL.strip()

    if webhook_url:
        log.info("Webhook mode: %s", webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=webhook_url.rstrip("/") + "/" + BOT_TOKEN,
        )
    else:
        log.info("Polling mode")
        
        # HTTP server: passes Railway healthchecks + receives worker count reports
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class HealthCheckHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
            def do_POST(self):
                if self.path != "/internal/count":
                    self.send_response(404)
                    self.end_headers()
                    return
                token = self.headers.get("X-Count-Token", "")
                if token != BOT_TOKEN:
                    self.send_response(403)
                    self.end_headers()
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    uid = str(body.get("user_id", ""))
                    count = int(body.get("count", 0))
                    if uid and count > 0:
                        today_iso = date.today().isoformat()
                        rec = db.data['daily_usage'].get(uid)
                        if rec and rec["date"] == today_iso:
                            rec["count"] += count
                        else:
                            db.data['daily_usage'][uid] = {"date": today_iso, "count": count}
                        db.data["total_files"] = db.data.get("total_files", 0) + count
                        db.save()
                        log.info("Count report: user=%s +%d (now %d)", uid, count, db.data['daily_usage'][uid]["count"])
                    self.send_response(200)
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok": true}')
                except Exception as e:
                    log.error("Count report error: %s", e)
                    try:
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(b'{"ok": false}')
                    except Exception:
                        pass
            def log_message(self, format, *args):
                pass
        
        def start_dummy_server():
            try:
                server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
                log.info(f"HTTP server started on port {PORT} (healthchecks + /internal/count)")
                server.serve_forever()
            except Exception as e:
                log.error(f"Failed to start HTTP server: {e}")
                
        threading.Thread(target=start_dummy_server, daemon=True).start()
        
        while True:
            try:
                app.run_polling(allowed_updates=Update.ALL_TYPES)
                break
            except Conflict:
                log.error("409 Conflict — another instance is polling. Retrying in 60s...")
                time.sleep(60)


if __name__ == "__main__":
    main()
