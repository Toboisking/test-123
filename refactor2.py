import re

with open("bot.py", "r") as f:
    content = f.read()

content = content.replace('list(USER_SUBS.items())', 'list(db.data["subscriptions"].items())')
content = content.replace('USER_SUBS.items()', 'db.data["subscriptions"].items()')
content = content.replace('len(BANNED_USERS)', 'len(db.data["banned"])')
content = content.replace('len(USER_SUBS)', 'len(db.data["subscriptions"])')

# Fix line 1003 `if changed: `
content = content.replace('if changed:\n                \n        except Exception as e:', 'if changed:\n                db.save()\n        except Exception as e:')
content = content.replace('if changed:\n                \n', 'if changed:\n                db.save()\n')

with open("bot.py", "w") as f:
    f.write(content)
