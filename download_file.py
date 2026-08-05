import os, sys, asyncio, hashlib
from pyrogram import Client
from pyrogram.errors import FloodWait, Unauthorized

# Dedicated directory so the session files can be persisted (Railway /opt volume)
# or cached between GitHub Actions runs. Reusing the same session avoids
# repeated 'auth.ImportBotAuthorization' calls which trigger Telegram FloodWait.
SESSION_DIR = os.environ.get("TG_SESSION_DIR", "/opt/tg_sessions")
os.makedirs(SESSION_DIR, exist_ok=True)


class DownloadStallError(Exception):
    pass


async def robust_download(app, media, dest_path):
    import time
    last_progress_time = time.time()

    async def progress_cb(current, total):
        nonlocal last_progress_time
        last_progress_time = time.time()
        if total > 0:
            pct = current * 100.0 / total
            print(f"PROGRESS:{pct:.2f}", flush=True)

    async def watchdog():
        while True:
            await asyncio.sleep(5)
            if time.time() - last_progress_time > 45:
                print("ERROR: Download connection stalled for 45 seconds!", flush=True)
                return  # signals a stall

    watchdog_task = asyncio.create_task(watchdog())
    main_task = asyncio.create_task(app.download_media(media, file_name=dest_path, progress=progress_cb))
    try:
        done, _ = await asyncio.wait(
            {main_task, watchdog_task},
            timeout=3600,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if watchdog_task in done:
            main_task.cancel()
            raise DownloadStallError("Download connection stalled for 45 seconds")
        if main_task not in done:
            main_task.cancel()
            raise TimeoutError("Download timed out after 60 minutes")
        exc = main_task.exception()
        if exc:
            raise exc
    finally:
        watchdog_task.cancel()
        if not main_task.done():
            main_task.cancel()


async def main():
    raw_api = os.environ.get("API_ID", "").strip()
    api_id = int(raw_api) if raw_api.isdigit() else 0
    api_hash = os.environ.get("API_HASH", "").strip()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    file_id = os.environ.get("PAYLOAD_FILE_ID", "").strip()

    raw_chat = os.environ.get("PAYLOAD_CHAT_ID", "").strip()
    chat_id = int(raw_chat) if raw_chat.lstrip("-").isdigit() else 0

    raw_orig = os.environ.get("PAYLOAD_ORIGINAL_MESSAGE_ID", "").strip()
    orig_msg_id = int(raw_orig) if raw_orig.isdigit() else 0

    dest_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/input_file"

    if not api_id or not api_hash or not bot_token or not file_id:
        print("Missing credentials or file_id for MTProto download.")
        sys.exit(1)

    # Stable session pool: the same file_id always maps to the same session name.
    # NOTE: python's built-in hash() is randomized per process (PYTHONHASHSEED),
    # so hashlib is required for a stable name across runs.
    pool_id = int(hashlib.md5(file_id.encode("utf-8")).hexdigest(), 16) % 5
    session_name = f"worker_session_pool_{pool_id}"
    app = Client(session_name, api_id=api_id, api_hash=api_hash, bot_token=bot_token, workdir=SESSION_DIR)

    print(f"Downloading file_id {file_id} via MTProto (Pyrogram)...", flush=True)

    def remove_session_file():
        import glob
        for f in glob.glob(os.path.join(SESSION_DIR, session_name + ".*")):
            try:
                os.remove(f)
                print(f"Removed stale session: {f}", flush=True)
            except OSError:
                pass

    last_error = None
    for attempt in range(5):
        try:
            async with app:
                if chat_id and orig_msg_id:
                    msg = await app.get_messages(chat_id, orig_msg_id)
                    if msg and msg.document:
                        await robust_download(app, msg, dest_path)
                    else:
                        print("Failed to fetch message or no document found. Trying fallback...", flush=True)
                        await robust_download(app, file_id, dest_path)
                else:
                    await robust_download(app, file_id, dest_path)
            print("Download complete.", flush=True)
            return
        except FloodWait as e:
            last_error = e
            wait = min(int(e.value) + 5, 900)
            print(f"WARN: FloodWait {e.value}s, attempt {attempt+1}/5. Waiting {wait}s...", flush=True)
            await asyncio.sleep(wait)
        except Unauthorized as e:
            last_error = e
            print(f"WARN: Unauthorized on auth, attempt {attempt+1}/5. Resetting this session...", flush=True)
            remove_session_file()
            await asyncio.sleep(30)
        except Exception as e:
            last_error = e
            print(f"WARN: MTProto download error, attempt {attempt+1}/5: {e}", flush=True)
            await asyncio.sleep(5)

    print(f"ERROR: MTProto download failed after 5 attempts: {last_error}", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
