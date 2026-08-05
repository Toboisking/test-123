import os, sys, asyncio, hashlib
from pyrogram import Client
from pyrogram.errors import FloodWait, Unauthorized

# Dedicated directory so the session files can be persisted (Railway /opt volume)
# or cached between GitHub Actions runs. Reusing the same session avoids
# repeated 'auth.ImportBotAuthorization' calls which trigger Telegram FloodWait.
SESSION_DIR = os.environ.get("TG_SESSION_DIR", "/opt/tg_sessions")
os.makedirs(SESSION_DIR, exist_ok=True)


async def progress(current, total):
    if total > 0:
        pct = current * 100.0 / total
        print(f"PROGRESS:{pct:.2f}", flush=True)


async def main():
    raw_api = os.environ.get("API_ID", "").strip()
    api_id = int(raw_api) if raw_api.isdigit() else 0
    api_hash = os.environ.get("API_HASH", "").strip()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

    raw_chat = os.environ.get("PAYLOAD_CHAT_ID", "").strip()
    chat_id = int(raw_chat) if raw_chat.lstrip("-").isdigit() else 0

    file_path = sys.argv[1] if len(sys.argv) > 1 else ""
    caption = sys.argv[2] if len(sys.argv) > 2 else ""

    if not api_id or not api_hash or not bot_token or not chat_id or not file_path:
        print("Missing credentials or file_path for MTProto upload.")
        sys.exit(1)

    pool_id = int(hashlib.md5(file_path.encode("utf-8")).hexdigest(), 16) % 5
    session_name = f"worker_upload_pool_{pool_id}"
    app = Client(session_name, api_id=api_id, api_hash=api_hash, bot_token=bot_token, workdir=SESSION_DIR)

    print(f"Uploading file to chat_id {chat_id} via MTProto (Pyrogram)...", flush=True)

    last_error = None
    for attempt in range(3):
        try:
            async with app:
                await app.send_document(chat_id=int(chat_id), document=file_path, caption=caption, progress=progress)
            print("Upload complete.", flush=True)
            return
        except FloodWait as e:
            last_error = e
            wait = min(e.value, 90)
            print(f"WARN: FloodWait {e.value}s (auth), attempt {attempt+1}/3. Sleeping {wait}s...", flush=True)
            await asyncio.sleep(wait)
        except Unauthorized as e:
            last_error = e
            print(f"WARN: Unauthorized on auth, attempt {attempt+1}/3. Sleeping 30s...", flush=True)
            await asyncio.sleep(30)
        except Exception as e:
            last_error = e
            print(f"WARN: MTProto upload error, attempt {attempt+1}/3: {e}", flush=True)
            await asyncio.sleep(5)

    print(f"ERROR: MTProto upload failed after 3 attempts: {last_error}", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
