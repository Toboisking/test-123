import re

with open("bot.py", "r") as f:
    content = f.read()

# Replace APPROVED_USERS.add(...)
content = re.sub(r'APPROVED_USERS\.add\(([^)]+)\)', r'db.add_approved(\1)', content)
# Replace APPROVED_USERS.discard(...)
content = re.sub(r'APPROVED_USERS\.discard\(([^)]+)\)', r'db.remove_approved(\1)', content)
# Replace BANNED_USERS.add(...)
content = re.sub(r'BANNED_USERS\.add\(([^)]+)\)', r'db.ban(\1)', content)
# Replace BANNED_USERS.discard(...)
content = re.sub(r'BANNED_USERS\.discard\(([^)]+)\)', r'db.unban(\1)', content)

# Replace len(APPROVED_USERS)
content = re.sub(r'len\(APPROVED_USERS\)', r'len(db.data["approved"])', content)
# Replace list(APPROVED_USERS)
content = re.sub(r'list\(APPROVED_USERS\)', r'db.data["approved"]', content)
# Replace str(user_id) in APPROVED_USERS -> handled by is_allowed
# Wait, let's just replace APPROVED_USERS entirely where needed, or leave it.

# USER_SUBS
content = re.sub(r'USER_SUBS\.get\(([^)]+)\)', r'db.data["subscriptions"].get(\1)', content)
content = re.sub(r'USER_SUBS\.pop\(([^,]+), None\)', r'db.remove_sub(\1)', content)
content = re.sub(r'USER_SUBS\[([^\]]+)\] = (.+)', r'# Sub replaced below', content) # Need manual fix for setting subs
content = content.replace("save_user_subscriptions()", "")
content = content.replace("save_approved_users()", "")
content = re.sub(r'len\(USER_SUBS\)', r'len(db.data["subscriptions"])', content)

# Check for check_daily_limit
# It has USER_SUBS.get(uid) -> db.data["subscriptions"].get(uid)

with open("bot.py", "w") as f:
    f.write(content)
