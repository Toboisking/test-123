import asyncio
import glob
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile
from html import unescape
from pathlib import Path
from urllib.parse import unquote

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker_dex2jar")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
FILE_URL = os.environ.get("PAYLOAD_FILE_URL", "")
TG_FILE_PATH = os.environ.get("PAYLOAD_TG_FILE_PATH", "")
CHAT_ID = os.environ.get("PAYLOAD_CHAT_ID", "")
MESSAGE_ID = os.environ.get("PAYLOAD_MESSAGE_ID", "")
FILENAME = os.environ.get("PAYLOAD_FILENAME", "download")
JOB_ID = os.environ.get("PAYLOAD_JOB_ID", "")
IS_ADMIN = os.environ.get("PAYLOAD_IS_ADMIN", "False").lower() == "true"
IS_PREMIUM = os.environ.get("PAYLOAD_IS_PREMIUM", "False").lower() == "true"
USER_ID = os.environ.get("PAYLOAD_USER_ID", CHAT_ID)
REPORT_URL = os.environ.get("PAYLOAD_REPORT_URL", "")
REPORT_TOKEN = BOT_TOKEN
MAX_DOWNLOAD_MB = 2000 if IS_ADMIN else 500


def _dex2jar_cp() -> str:
    if glob.glob("/opt/dex2jar/lib/*.jar"):
        return "/opt/dex2jar/lib/*"
    libs = sorted(glob.glob("/opt/dex2jar/**/lib", recursive=True))
    if libs:
        return libs[0] + "/*"
    return "/opt/dex2jar/lib/*"


DEX2JAR_CP = _dex2jar_cp()
CFR_JAR = "/opt/cfr.jar"
log.info("DEX2JAR_CP=%s", DEX2JAR_CP)

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def check_download_size(total_bytes: int):
    if total_bytes and total_bytes > MAX_DOWNLOAD_MB * 1024 * 1024 and not IS_ADMIN:
        raise ValueError(
            f"File is {total_bytes/1024/1024:.1f} MB — max download limit is {MAX_DOWNLOAD_MB} MB."
        )


def notify_app(message: str, title: str = None):
    if not JOB_ID:
        return
    headers = {}
    if title:
        headers["Title"] = title.encode("utf-8")
    try:
        httpx.post(f"https://ntfy.sh/{JOB_ID}", data=message.encode("utf-8"), headers=headers, timeout=10)
    except Exception as e:
        log.warning("Ntfy failed: %s", e)


def tg(method: str, **params):
    try:
        resp = httpx.post(f"{API}/{method}", data=params, timeout=60)
        return resp.json()
    except Exception as e:
        log.warning("tg %s failed: %s", method, e)
        return None


import json

def edit(text: str, parse_mode: str = None, keep_button: bool = True):
    params = {"chat_id": CHAT_ID, "message_id": MESSAGE_ID, "text": text}
    if parse_mode:
        params["parse_mode"] = parse_mode
    if keep_button:
        params["reply_markup"] = json.dumps({
            "inline_keyboard": [[{"text": "🛑 Stop Processing", "callback_data": f"stop_{MESSAGE_ID}"}]]
        })
    tg("editMessageText", **params)
    notify_app(text)


def progress_bar(pct: float) -> str:
    val = float(pct)
    filled = max(0, min(16, int(val * 16 / 100)))
    bar = "▰" * filled + "▱" * (16 - filled)
    return f"{bar} {val:.2f} %"


async def send_error_log(work_dir, exception_obj, title="dex2jar Decompilation failed"):
    import traceback
    err_str = traceback.format_exc()
    log.error("%s: %s", title, exception_obj)
    sent = False
    try:
        err_file = Path(work_dir) / "error.txt"
        err_file.write_text(f"❌ {title}:\n\n{err_str}")
        caption = f"❌ Error Log:\n{str(exception_obj)[:100]}"
        try:
            with open(err_file, "rb") as ef:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"document": ef})
                    resp.raise_for_status()
            sent = True
        except Exception as e:
            log.warning("HTTP error log upload failed, falling back to MTProto: %s", e)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "upload_file.py", str(err_file), caption,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            await proc.wait()
            sent = (proc.returncode == 0)
    except Exception as e:
        log.error("Failed to upload error log: %s", e)
    if sent:
        edit(f"❌ {title}. Error log sent.", keep_button=False)
    else:
        edit(f"❌ {title}. Could not send error log. Try again later.", keep_button=False)


async def download_url(url: str, dest: Path, on_progress) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    fid = None
    if "drive.google.com" in url:
        m = re.search(r"/file/d/([^/?#]+)", url) or re.search(r"[?&]id=([^&#]+)", url)
        if m:
            fid = m.group(1)
            url = f"https://drive.google.com/uc?export=download&id={fid}"

    timeout = httpx.Timeout(30.0, connect=30.0, read=300.0, write=300.0)
    transport = httpx.AsyncHTTPTransport(retries=3)
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout, transport=transport) as client:
        for attempt in range(3):
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "")
                if "text/html" in ct:
                    if attempt == 2:
                        raise ValueError("The link is a webpage, not a direct file.")
                    html = (await resp.aread()).decode(errors="replace")
                    if fid:
                        m = re.search(r'name="confirm"\s+value="([^"]+)"', html)
                        if m:
                            url = f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm={m.group(1)}"
                            continue
                        if "Google Drive" in html or "drive.google" in html:
                            raise ValueError("Google Drive file not accessible.")
                    m = re.search(r'href="(https?://download[0-9]+\.mediafire\.com/[^"]+)"', html)
                    if m:
                        url = unescape(m.group(1))
                        continue
                    raise ValueError("The link is a webpage, not a direct file.")

                filename = "download.apk"
                cd = resp.headers.get("content-disposition", "")
                m = re.search(r'filename="?([^";]+)"?', cd)
                if m:
                    filename = unquote(m.group(1)).strip()
                else:
                    path_part = unquote(resp.url.path.rstrip("/").rsplit("/", 1)[-1])
                    if path_part:
                        filename = path_part

                total = int(resp.headers.get("content-length") or 0)
                check_download_size(total)
                downloaded = 0
                with open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes(65536):
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = min(100, int(downloaded * 100 / total))
                            await on_progress(pct)
                return filename
        raise ValueError("Could not download file from this link.")


async def run_dex2jar(file_path: Path, work_dir: Path, on_progress) -> Path:
    out_jar = work_dir / "output.jar"

    inputs = [str(file_path)]
    if file_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(file_path, "r") as zf:
            names = zf.namelist()
            dex_entries = [n for n in names if n.lower().endswith(".dex")]
            if dex_entries:
                extract_dir = work_dir / "dex_input"
                extract_dir.mkdir(exist_ok=True)
                for de in dex_entries:
                    zf.extract(de, extract_dir)
                inputs = [str(p) for p in sorted(extract_dir.rglob("*.dex"))]
            elif any(n.lower().endswith(".apk") for n in names):
                apk_files = [n for n in names if n.lower().endswith(".apk")]
                extract_dir = work_dir / "apk_input"
                extract_dir.mkdir(exist_ok=True)
                zf.extract(apk_files[0], extract_dir)
                inputs = [str(extract_dir / apk_files[0])]

    if not inputs:
        raise ValueError("No DEX files found in the input.")

    cmd = [
        "java", "-Xmx10G",
        "-cp", DEX2JAR_CP,
        "com.googlecode.dex2jar.tools.Dex2jarCmd",
        "-f", "-o", str(out_jar),
    ] + inputs
    log.info("Running dex2jar: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    await on_progress(15, "🧬 Converting DEX to JAR (dex2jar)...")

    out_lines = []
    async def read_stream():
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if line:
                out_lines.append(line)
            await on_progress(45, "🧬 Converting DEX to JAR...")
        return await proc.wait()

    try:
        rc = await asyncio.wait_for(read_stream(), timeout=3600)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError("dex2jar conversion timed out")

    if rc != 0 or not out_jar.exists() or out_jar.stat().st_size == 0:
        raise RuntimeError(f"dex2jar failed with exit code {rc}:\n" + "\n".join(out_lines[-20:]))

    return out_jar


async def run_cfr(jar_path: Path, work_dir: Path, on_progress) -> Path:
    out_dir = work_dir / "java_src"
    out_dir.mkdir(exist_ok=True)
    cmd = [
        "java", "-Xmx10G",
        "-jar", CFR_JAR,
        str(jar_path),
        "--outputdir", str(out_dir),
        "--silent", "false",
    ]
    log.info("Running CFR: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    await on_progress(55, "☕ Decompiling JAR to Java (CFR)...")

    out_lines = []
    async def read_stream():
        count = 0
        idle = 0
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
                idle = 0
            except asyncio.TimeoutError:
                idle += 60
                if idle >= 1800:
                    proc.kill()
                    raise RuntimeError("CFR stalled: no output for 30 minutes")
                continue
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if line:
                out_lines.append(line)
                if len(out_lines) > 100:
                    del out_lines[:-100]
                count += 1
                if count % 20 == 0:
                    await on_progress(80, f"☕ Decompiling JAR to Java... ({count} classes)")
        return await proc.wait()

    try:
        rc = await asyncio.wait_for(read_stream(), timeout=2700)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError("CFR decompilation timed out")

    if rc != 0:
        raise RuntimeError(f"CFR failed with exit code {rc}:\n" + "\n".join(out_lines[-20:]))

    java_files = [p for p in out_dir.rglob("*.java")] if out_dir.exists() else []
    if not java_files:
        raise ValueError("No Java source generated from the JAR.")

    return out_dir


async def run_jadx_fallback(jar_path: Path, work_dir: Path, on_progress) -> Path:
    out_dir = work_dir / "java_src_fallback"
    cmd = [
        "/opt/jadx/bin/jadx",
        "-d", str(out_dir),
        "--no-res",
        str(jar_path),
    ]
    log.info("Running JADX fallback: %s", " ".join(cmd))
    env = dict(os.environ, JADX_OPTS="-Xmx12G")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
    )

    await on_progress(70, "☕ Decompiling with JADX fallback...")

    out_lines = []
    async def read_stream():
        count = 0
        idle = 0
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
                idle = 0
            except asyncio.TimeoutError:
                idle += 60
                if idle >= 1800:
                    proc.kill()
                    raise RuntimeError("JADX fallback stalled: no output for 30 minutes")
                continue
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if line:
                out_lines.append(line)
                if len(out_lines) > 100:
                    del out_lines[:-100]
                count += 1
                if count % 20 == 0:
                    await on_progress(85, f"☕ Decompiling with JADX... ({count} classes)")
        return await proc.wait()

    try:
        rc = await asyncio.wait_for(read_stream(), timeout=3600)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError("JADX fallback decompilation timed out")

    if rc != 0:
        java_files = [p for p in out_dir.rglob("*.java")] if out_dir.exists() else []
        if not java_files:
            raise RuntimeError("JADX fallback failed:\n" + "\n".join(out_lines[-20:]))
        log.warning("JADX fallback finished with errors (rc=%s) but generated %d java files", rc, len(java_files))

    java_files = [p for p in out_dir.rglob("*.java")] if out_dir.exists() else []
    if not java_files:
        raise ValueError("No Java source generated (CFR and JADX both failed).")

    return out_dir


def check_zip_limits(file_path: Path):
    if Path(FILENAME).suffix.lower() != ".zip":
        return
    import zipfile
    with zipfile.ZipFile(file_path) as zf:
        names = zf.namelist()
    so_dex = sum(1 for n in names if n.lower().endswith((".so", ".dex")))
    apks = sum(1 for n in names if n.lower().endswith(".apk"))
    max_so_dex = 5 if IS_PREMIUM else 1
    max_apk = 2 if IS_PREMIUM else 0
    if so_dex > max_so_dex:
        raise ValueError(f"ZIP contains {so_dex} .so/.dex files — max {max_so_dex} allowed for {'Premium' if IS_PREMIUM else 'Free'} users.")
    if apks > max_apk:
        raise ValueError(f"ZIP contains {apks} .apk files — max {max_apk} allowed for {'Premium' if IS_PREMIUM else 'Free'} users.")


def count_zip_so_dex(file_path: Path) -> int:
    if Path(FILENAME).suffix.lower() != ".zip":
        return 0
    import zipfile
    with zipfile.ZipFile(file_path) as zf:
        names = zf.namelist()
    return sum(1 for n in names if n.lower().endswith((".so", ".dex")))


def report_extra_count(extra: int):
    if not REPORT_URL or not REPORT_TOKEN or extra <= 0:
        return
    try:
        httpx.post(
            REPORT_URL,
            json={"user_id": USER_ID, "count": extra},
            headers={"X-Count-Token": REPORT_TOKEN},
            timeout=10,
        )
        log.info("Reported extra count %d for user %s", extra, USER_ID)
    except Exception as e:
        log.warning("Count report failed: %s", e)


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing env TELEGRAM_BOT_TOKEN / PAYLOAD_CHAT_ID")
        sys.exit(1)

    edit("🟢 Job started! Preparing dex2jar engine on cloud server...", parse_mode="HTML")

    work_dir = Path(tempfile.gettempdir()) / ("dex2jar_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        ext = Path(FILENAME).suffix or ".apk"
        dest = work_dir / f"input_file{ext}"
        last = [-100.0]

        dl_method = ["📥 Downloading file..."]
        async def on_dl(pct: float):
            if pct < last[0] or (pct - last[0] < 2.0 and pct < 100.0): return
            last[0] = pct
            edit(f"{dl_method[0]}\n\n{progress_bar(pct)}")

        try:
            file_id = os.environ.get("PAYLOAD_FILE_ID", "")
            got_file = False
            if TG_FILE_PATH:
                try:
                    filename = FILENAME or "download.apk"
                    tg_url = TG_FILE_PATH if TG_FILE_PATH.startswith("http") else f"{API}/file/{TG_FILE_PATH}"
                    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120, read=300)) as client:
                        async with client.stream("GET", tg_url) as resp:
                            resp.raise_for_status()
                            total = int(resp.headers.get("content-length") or 0)
                            check_download_size(total)
                            done = 0
                            with open(dest, "wb") as fh:
                                async for chunk in resp.aiter_bytes(65536):
                                    fh.write(chunk)
                                    done += len(chunk)
                                    if total:
                                        pct = min(100, int(done * 100 / total))
                                        await on_dl(pct)
                    got_file = True
                except Exception as http_err:
                    if not file_id:
                        raise
                    log.warning("HTTP download failed, falling back to MTProto: %s", http_err)
            if not got_file and file_id:
                filename = FILENAME or "download.apk"
                dl_method[0] = "📥 Downloading via MTProto (Pyrogram)..."
                await on_dl(0.0)
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "download_file.py", str(dest),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                dl_logs = []
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode(errors="replace").strip()
                    if line:
                        dl_logs.append(line)
                    if line.startswith("PROGRESS:"):
                        try:
                            pct = float(line.split(":")[1])
                            await on_dl(pct)
                        except ValueError:
                            pass
                await proc.wait()
                if proc.returncode != 0:
                    err_tail = "\n".join(dl_logs[-8:]) or "no output"
                    raise ValueError(f"MTProto Download failed with code {proc.returncode}: {err_tail}")
                got_file = True
            if not got_file:
                filename = await asyncio.wait_for(download_url(FILE_URL, dest, on_dl), timeout=1800)
        except Exception as e:
            await send_error_log(work_dir, e, "Download failed")
            return

        size = dest.stat().st_size
        if size == 0:
            edit("❌ Downloaded file is empty.", keep_button=False)
            return
        if size > MAX_DOWNLOAD_MB * 1024 * 1024 and not IS_ADMIN:
            edit(f"❌ File is {size/1024/1024:.1f} MB — max download limit is {MAX_DOWNLOAD_MB} MB.", keep_button=False)
            return

        try:
            extra = count_zip_so_dex(dest)
        except Exception as e:
            log.warning("Could not count zip contents: %s", e)
            extra = 0
        if extra:
            report_extra_count(extra)

        try:
            check_zip_limits(dest)
        except ValueError as e:
            edit(f"❌ {e}", keep_button=False)
            return

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Starting dex2jar + CFR...")

        last_prog = [0, ""]
        async def on_progress(pct: int, label: str = "🧬 Processing..."):
            if pct - last_prog[0] < 5 and label == last_prog[1]:
                return
            last_prog[0], last_prog[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)}")

        try:
            out_jar = await run_dex2jar(dest, work_dir, on_progress)
        except asyncio.TimeoutError:
            edit("⏰ Timeout! The file is too big for dex2jar.", keep_button=False)
            return
        except Exception as e:
            log.warning("dex2jar crashed (%s), falling back to JADX on input", e)
            try:
                edit("⚠️ dex2jar crashed — falling back to JADX for Java source...")
                src_dir = await run_jadx_fallback(dest, work_dir, on_progress)
                out_jar = None
            except Exception as e2:
                await send_error_log(work_dir, e2, "dex2jar conversion crashed")
                return

        try:
            src_dir = await run_cfr(out_jar, work_dir, on_progress)
        except asyncio.TimeoutError as e:
            log.warning("CFR timed out, falling back to JADX: %s", e)
            try:
                edit("⏰ CFR too slow — falling back to JADX for Java source...")
                src_dir = await run_jadx_fallback(out_jar, work_dir, on_progress)
            except Exception as e2:
                await send_error_log(work_dir, e2, "Java decompilation failed")
                return
        except ValueError as e:
            edit(f"❌ {e}", keep_button=False)
            return
        except Exception as e:
            log.warning("CFR crashed, falling back to JADX: %s", e)
            try:
                edit("⚠️ CFR crashed — falling back to JADX for Java source...")
                src_dir = await run_jadx_fallback(out_jar, work_dir, on_progress)
            except Exception as e2:
                await send_error_log(work_dir, e2, "Java decompilation failed")
                return

        await on_progress(100, "✅ Decompilation complete!")
        edit("📦 Packaging JAR + Java Source...")
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', "_", filename)[:60] or "file"
        orig_stem = Path(safe_name).stem or "dex2jar"

        zip_path = work_dir / f"{orig_stem}_dex2jar_java.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if out_jar is not None:
                zf.write(out_jar, f"{orig_stem}.jar")
            for root, dirs, files in os.walk(src_dir):
                for f in files:
                    fp = os.path.join(root, f)
                    arcname = os.path.join("src", os.path.relpath(fp, src_dir))
                    zf.write(fp, arcname)

        if out_jar is not None:
            edit("✅ dex2jar + CFR complete! Sending ZIP...")
        else:
            edit("✅ Java Source ready (JADX fallback)! Sending ZIP...")

        if out_jar is not None:
            caption = f"✅ Decompiled <b>{safe_name}</b> to JAR + Java Source — Powered By @R3V_X"
        else:
            caption = f"⚠️ dex2jar crashed (too large?) — delivered <b>Java Source</b> via JADX fallback — Powered By @R3V_X"
        up_last = [0]
        async def on_up(pct: int):
            if pct < up_last[0] or pct - up_last[0] < 2: return
            up_last[0] = pct
            edit(f"✅ Decompilation complete!\n📤 Sending ZIP...\n\n{progress_bar(pct)}")

        try:
            http_ok = False
            MAX_HTTP_UPLOAD = 50 * 1024 * 1024
            if zip_path.stat().st_size <= MAX_HTTP_UPLOAD:
                try:
                    with open(zip_path, "rb") as doc_f:
                        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
                        data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
                        files = {"document": doc_f}
                        async with httpx.AsyncClient(timeout=300) as client:
                            resp = await client.post(url, data=data, files=files)
                            resp.raise_for_status()
                    http_ok = True
                except Exception as e:
                    log.warning("HTTP upload failed, falling back to MTProto: %s", e)
            if not http_ok:
                if not os.environ.get("API_ID", "").strip():
                    raise ValueError("File too large for Bot API (50MB) and no API_ID/API_HASH configured.")
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "upload_file.py", str(zip_path), caption,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode(errors="replace").strip()
                    if line.startswith("PROGRESS:"):
                        try:
                            pct = int(float(line.split(":")[1]))
                            await on_up(pct)
                        except ValueError:
                            pass
                await proc.wait()
                if proc.returncode != 0:
                    raise ValueError(f"MTProto Upload failed with code {proc.returncode}")
            edit("✅ Decompilation complete! ZIP file delivered. 🔥", keep_button=False)

            if JOB_ID:
                notify_app("FINAL_ZIP_URL:telegram_direct_upload")
        except Exception as e:
            await send_error_log(work_dir, e, "Result upload failed")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
