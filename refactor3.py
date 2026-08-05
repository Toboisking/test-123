import re

with open("bot.py", "r") as f:
    content = f.read()

FORCE_JOIN_CODE = """
FORCE_CHANNELS = ["@allinformation0173"]
try:
    if os.environ.get("FORCE_CHANNEL_2"):
        FORCE_CHANNELS.append(os.environ.get("FORCE_CHANNEL_2"))
except: pass

async def check_force_join(update, context) -> bool:
    uid = update.effective_user.id
    if str(uid) in ADMIN_IDS: return True
    for ch in FORCE_CHANNELS:
        try:
            member = await context.bot.get_chat_member(chat_id=ch, user_id=uid)
            if member.status in ["left", "kicked"]:
                raise Exception("Not member")
        except Exception:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Join Channel 1", url="https://t.me/allinformation0173")],
                [InlineKeyboardButton("Join Channel 2", url="https://t.me/+gQawrH0MFs00M2Y1")]
            ])
            try:
                await update.message.reply_text("❌ <b>You must join our channels to use this bot!</b>\\nJoin the channels and try again.", reply_markup=keyboard, parse_mode="HTML")
            except: pass
            return False
    return True
"""

if "check_force_join" not in content:
    content = content.replace("def is_allowed", FORCE_JOIN_CODE + "\ndef is_allowed")

def add_check(func_name):
    global content
    pattern = f"async def {func_name}\\(update: Update, context: ContextTypes.DEFAULT_TYPE\\):\\n"
    repl = f"async def {func_name}(update: Update, context: ContextTypes.DEFAULT_TYPE):\\n    if not await check_force_join(update, context): return\\n"
    content = re.sub(pattern, repl, content)

add_check("cmd_start")
add_check("handle_file")
add_check("cmd_link")

COMMANDS_CODE = """
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
    
    text = f"<b>{title} ({len(users)}):</b>\\n"
    for u in users:
        text += f"• <code>{u}</code>\\n"
    if not users: text += "None found."
    await update.message.reply_text(text, parse_mode="HTML")
"""

if "cmd_list_users" not in content:
    content = content.replace("async def cmd_start", COMMANDS_CODE + "\nasync def cmd_start")
    
    handler_code = """
    app.add_handler(CommandHandler("approved_users", cmd_list_users))
    app.add_handler(CommandHandler("unapproved_users", cmd_list_users))
    app.add_handler(CommandHandler("ban_users", cmd_list_users))
    app.add_handler(CommandHandler("banned_users", cmd_list_users))
    app.add_handler(CommandHandler("premium_users", cmd_list_users))
"""
    content = content.replace('app.add_handler(CommandHandler("start", cmd_start))', handler_code + '    app.add_handler(CommandHandler("start", cmd_start))')


with open("bot.py", "w") as f:
    f.write(content)
