import asyncio
import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from html import unescape
from pathlib import Path
from urllib.parse import unquote

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker_dex_compile")

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
MODE = os.environ.get("PAYLOAD_DEXCOMPILE_MODE", "auto")
MAX_DOWNLOAD_MB = 2000 if IS_ADMIN else 500

SMALI_JAR = "/opt/smali.jar"
R8_JAR = "/opt/r8.jar"
MAX_SMALI_FILES_FREE = 1000
MAX_SMALI_FILES_PREMIUM = 5000

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

CANCEL_MARKER = "Job Cancelled by User"
CANCELLED = {"v": False}


class JobCancelled(BaseException):
    pass


async def cancel_watchdog():
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            while not CANCELLED["v"]:
                await asyncio.sleep(2)
                try:
                    resp = await client.post(
                        f"{API}/getMessage",
                        data={"chat_id": CHAT_ID, "message_id": MESSAGE_ID},
                    )
                    txt = ((resp.json() or {}).get("result") or {}).get("text") or ""
                except Exception:
                    continue
                if CANCEL_MARKER in txt:
                    CANCELLED["v"] = True
                    return
    except Exception as e:
        log.warning("Cancel watchdog stopped: %s", e)


def find_android_jar():
    for env in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        root = os.environ.get(env)
        if root and os.path.isdir(root):
            pl_dir = os.path.join(root, "platforms")
            if os.path.isdir(pl_dir):
                try:
                    vers = sorted((d for d in os.listdir(pl_dir) if re.match(r"^android-\d+$", d)),
                                  key=lambda v: int(v.split("-")[1]), reverse=True)
                except Exception:
                    vers = []
                for v in vers:
                    p = os.path.join(pl_dir, v, "android.jar")
                    if os.path.exists(p):
                        return p
    return ""


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
    if CANCELLED["v"]:
        return
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


def proc_cpu_usage(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
        return int(parts[13]) + int(parts[14])
    except Exception:
        return -1


async def send_error_log(work_dir, exception_obj, title="DEX Compile failed"):
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

                filename = "download"
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
                        if CANCELLED["v"]:
                            raise JobCancelled()
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = min(100, int(downloaded * 100 / total))
                            await on_progress(pct)
                return filename
        raise ValueError("Could not download file from this link.")


async def run_tool(cmd: list, on_progress, label: str, timeout: int = 3600, progress_stall: int = 1800):
    log.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out_lines = []

    async def read_stream():
        last_activity = time.monotonic()
        last_cpu = proc_cpu_usage(proc.pid)
        while True:
            if CANCELLED["v"]:
                proc.kill()
                raise JobCancelled()
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
                last_activity = time.monotonic()
            except asyncio.TimeoutError:
                cpu = proc_cpu_usage(proc.pid)
                if cpu > last_cpu:
                    last_cpu = cpu
                    last_activity = time.monotonic()
                elif time.monotonic() - last_activity >= progress_stall:
                    proc.kill()
                    raise RuntimeError(f"{label} stalled: no CPU activity for {progress_stall//60} minutes")
                continue
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if line:
                out_lines.append(line)
                if len(out_lines) > 100:
                    del out_lines[:-100]
                if "error" in line.lower() or "exception" in line.lower():
                    await on_progress(60, f"⚠️ {label} (checking)...")
        return await proc.wait()

    try:
        rc = await asyncio.wait_for(read_stream(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError(f"{label} timed out")

    if rc != 0:
        raise RuntimeError(f"{label} failed with exit code {rc}:\n" + "\n".join(out_lines[-25:]))
    return "\n".join(out_lines[-25:])


def find_inputs(src_dir: Path, suffixes) -> list:
    return [str(p) for p in sorted(Path(src_dir).rglob("*")) if p.is_file() and p.suffix.lower() in suffixes]


async def compile_smali_to_dex(input_path: Path, work_dir: Path, on_progress) -> Path:
    await on_progress(30, "🧩 Assembling Smali → .dex...")
    smali_sources = []
    if input_path.is_file() and input_path.suffix.lower() == ".smali":
        smali_sources = [str(input_path)]
    elif input_path.is_dir():
        smali_sources = [str(p) for p in sorted(input_path.rglob("*.smali"))]
    else:
        ext = Path(FILENAME).suffix.lower()
        if ext == ".zip":
            extract_dir = work_dir / "smali_src"
            extract_dir.mkdir(exist_ok=True)
            from zip_utils import extract_archive
            extract_archive(input_path, extract_dir)
            smali_sources = [str(p) for p in sorted(extract_dir.rglob("*.smali"))]
            input_path = extract_dir
        else:
            raise ValueError("Unsupported input. Send a .smali file or a ZIP containing .smali files.")

    if not smali_sources:
        raise ValueError("No .smali files found in the input.")

    max_files = MAX_SMALI_FILES_PREMIUM if IS_PREMIUM else MAX_SMALI_FILES_FREE
    if not IS_ADMIN and len(smali_sources) > max_files:
        raise ValueError(f"Too many .smali files: {len(smali_sources)} — max {max_files} allowed for {'Premium' if IS_PREMIUM else 'Free'} users.")

    dex_path = work_dir / "classes.dex"
    assemble_root = input_path if input_path.is_dir() else input_path.parent
    cmd = ["java", "-Xmx8G", "-jar", SMALI_JAR, "assemble", str(assemble_root), "-o", str(dex_path)]
    await run_tool(cmd, on_progress, "Smali assemble")
    await on_progress(70, "🧩 Smali assembly done!")
    if not dex_path.exists():
        raise ValueError("Smali assembly produced no .dex output.")
    return dex_path


async def compile_java_to_dex(input_path: Path, work_dir: Path, on_progress) -> Path:
    await on_progress(25, "☕ Preparing Java sources...")
    java_files = []
    input_jar = None
    android_jar = find_android_jar()
    ext = Path(FILENAME).suffix.lower()

    if input_path.is_file():
        if ext == ".java":
            java_files = [str(input_path)]
        elif ext in (".jar", ".class"):
            input_jar = input_path
        elif ext == ".zip":
            extract_dir = work_dir / "java_src"
            extract_dir.mkdir(exist_ok=True)
            from zip_utils import extract_archive
            extract_archive(input_path, extract_dir)
            java_files = find_inputs(extract_dir, {".java"})
            if not java_files:
                jars = find_inputs(extract_dir, {".jar"})
                if jars:
                    input_jar = Path(jars[0])
                else:
                    classes = find_inputs(extract_dir, {".class"})
                    if classes:
                        cls_dir = extract_dir
                        class_root = work_dir / "classes_root"
                        class_root.mkdir(exist_ok=True)
                        for c in classes:
                            rel = Path(c).relative_to(cls_dir)
                            dst = class_root / rel
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(c, dst)
                        input_jar = await asyncio.to_thread(_jar_dir, class_root, work_dir / "classes.jar")
                    else:
                        raise ValueError("No .java, .jar, or .class files found in the ZIP.")
            else:
                java_files = java_files
    elif input_path.is_dir():
        java_files = find_inputs(input_path, {".java"})
        if not java_files:
            jars = find_inputs(input_path, {".jar"})
            if jars:
                input_jar = Path(jars[0])
            else:
                raise ValueError("No .java or .jar files found in the directory.")
    else:
        raise ValueError("Unsupported input for Java → DEX compile.")

    max_files = MAX_SMALI_FILES_PREMIUM if IS_PREMIUM else MAX_SMALI_FILES_FREE
    if not IS_ADMIN and len(java_files) > max_files:
        raise ValueError(f"Too many .java files: {len(java_files)} — max {max_files} allowed for {'Premium' if IS_PREMIUM else 'Free'} users.")

    if java_files:
        classes_dir = work_dir / "classes"
        classes_dir.mkdir(exist_ok=True)
        await on_progress(40, "☕ Compiling Java → .class (javac)...")
        java_files = [str(await asyncio.to_thread(_prepare_java_file, Path(f))) for f in java_files]
        cmd = ["javac", "-d", str(classes_dir)]
        if android_jar:
            cmd += ["-classpath", android_jar]
        cmd += java_files
        await run_tool(cmd, on_progress, "javac")
        if not any(classes_dir.rglob("*.class")):
            raise ValueError("javac produced no .class files (check your Java source).")
        input_jar = await asyncio.to_thread(_jar_dir, classes_dir, work_dir / "classes.jar")
        await on_progress(60, "📦 Packaged classes → JAR...")

    await on_progress(65, "🧬 Running D8 (class → .dex)...")
    dex_out = work_dir / "dex_out"
    dex_out.mkdir(exist_ok=True)
    cmd = ["java", "-Xmx8G", "-cp", R8_JAR, "com.android.tools.r8.D8", "--release", "--output", str(dex_out)]
    if android_jar:
        cmd += ["--lib", android_jar]
    cmd += [str(input_jar)]
    await run_tool(cmd, on_progress, "D8")
    dex_files = sorted(dex_out.glob("*.dex"))
    if not dex_files:
        raise ValueError("D8 produced no .dex output.")
    if len(dex_files) == 1:
        return dex_files[0]
    dex_zip = work_dir / "dex_output.zip"
    with zipfile.ZipFile(dex_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in dex_files:
            zf.write(d, d.name)
    await on_progress(80, "✅ D8 done!")
    return dex_zip


COMMON_ANDROID_IMPORTS = {
    "Activity": "android.app.Activity", "Dialog": "android.app.Dialog",
    "AlertDialog": "android.app.AlertDialog", "Notification": "android.app.Notification",
    "NotificationManager": "android.app.NotificationManager",
    "Context": "android.content.Context", "Intent": "android.content.Intent",
    "SharedPreferences": "android.content.SharedPreferences",
    "ColorStateList": "android.content.res.ColorStateList",
    "Resources": "android.content.res.Resources", "Configuration": "android.content.res.Configuration",
    "Bundle": "android.os.Bundle", "Handler": "android.os.Handler", "Looper": "android.os.Looper",
    "Build": "android.os.Build", "Environment": "android.os.Environment", "StatFs": "android.os.StatFs",
    "PowerManager": "android.os.PowerManager", "Vibrator": "android.os.Vibrator",
    "BlurMaskFilter": "android.graphics.BlurMaskFilter", "Paint": "android.graphics.Paint",
    "Canvas": "android.graphics.Canvas", "Color": "android.graphics.Color", "Bitmap": "android.graphics.Bitmap",
    "Typeface": "android.graphics.Typeface",
    "Drawable": "android.graphics.drawable.Drawable",
    "GradientDrawable": "android.graphics.drawable.GradientDrawable",
    "ColorDrawable": "android.graphics.drawable.ColorDrawable",
    "View": "android.view.View", "ViewGroup": "android.view.ViewGroup",
    "Window": "android.view.Window", "Gravity": "android.view.Gravity",
    "View": "android.view.View", "View.OnClickListener": "android.view.View$OnClickListener",
    "Spannable": "android.text.Spannable", "SpannableString": "android.text.SpannableString",
    "Log": "android.util.Log", "SparseArray": "android.util.SparseArray",
    "FrameLayout": "android.widget.FrameLayout", "RelativeLayout": "android.widget.RelativeLayout",
    "LinearLayout": "android.widget.LinearLayout", "ScrollView": "android.widget.ScrollView",
    "TextView": "android.widget.TextView", "EditText": "android.widget.EditText",
    "Button": "android.widget.Button", "ImageView": "android.widget.ImageView",
    "Toast": "android.widget.Toast", "ListView": "android.widget.ListView",
    "BaseAdapter": "android.widget.BaseAdapter", "ArrayAdapter": "android.widget.ArrayAdapter",
    "AdapterView": "android.widget.AdapterView",
    "Animator": "android.animation.Animator", "ValueAnimator": "android.animation.ValueAnimator",
    "ObjectAnimator": "android.animation.ObjectAnimator",
}


def _auto_add_android_imports(path: Path) -> Path:
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return path
    if re.search(r"^\s*package\s+", text, re.M):
        return path
    existing_imports = set(re.findall(r"^\s*import\s+([\w.]+);", text, re.M))
    declared = set(re.findall(r"\b(?:class|interface|enum)\s+([A-Za-z_$][A-Za-z0-9_$]*)", text))
    needed = []
    for simple, full in COMMON_ANDROID_IMPORTS.items():
        base_simple = simple.rsplit(".", 1)[-1]
        if base_simple in declared:
            continue
        if any(imp.split(".")[-1] == base_simple for imp in existing_imports):
            continue
        if re.search(rf"\b{re.escape(base_simple)}\b", text):
            if full not in existing_imports:
                needed.append(full)
    if not needed:
        return path
    needed.sort()
    header = "\n".join(f"import {n};" for n in needed) + "\n"
    m = re.search(r"^", text)
    new_text = text[:m.start()] + header + text[m.start():]
    path.write_text(new_text)
    return path


def _rename_java_to_public_class(path: Path) -> Path:
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return path
    m = re.search(r"public\s+(?:abstract\s+|final\s+)?(?:strictfp\s+)?(class|interface|enum|@interface|record)\s+([A-Za-z_$][A-Za-z0-9_$]*)", text)
    if m:
        name = m.group(2)
        if path.name != name + ".java":
            new_path = path.with_name(name + ".java")
            try:
                shutil.move(str(path), str(new_path))
                return new_path
            except Exception:
                return path
    return path


def _prepare_java_file(path: Path) -> Path:
    path = _rename_java_to_public_class(path)
    path = _auto_add_android_imports(path)
    return path
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return path
    m = re.search(r"public\s+(?:abstract\s+|final\s+)?(?:strictfp\s+)?(class|interface|enum|@interface|record)\s+([A-Za-z_$][A-Za-z0-9_$]*)", text)
    if m:
        name = m.group(2)
        if path.name != name + ".java":
            new_path = path.with_name(name + ".java")
            try:
                shutil.move(str(path), str(new_path))
                return new_path
            except Exception:
                return path
    return path


def _jar_dir(src_dir: Path, out_jar: Path) -> Path:
    with zipfile.ZipFile(out_jar, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src_dir):
            for f in files:
                fp = os.path.join(root, f)
                arcname = os.path.relpath(fp, src_dir)
                zf.write(fp, arcname)
    return out_jar


def check_zip_limits(file_path: Path):
    if IS_ADMIN:
        return
    if Path(FILENAME).suffix.lower() != ".zip":
        return
    with zipfile.ZipFile(file_path) as zf:
        names = zf.namelist()
    so_dex = sum(1 for n in names if n.lower().endswith((".so", ".dex")))
    apks = sum(1 for n in names if n.lower().endswith(".apk"))
    max_so_dex = 5 if IS_PREMIUM else 1
    max_apk = 2 if IS_PREMIUM else 1
    if so_dex > max_so_dex:
        raise ValueError(f"ZIP contains {so_dex} .so/.dex files — max {max_so_dex} allowed for {'Premium' if IS_PREMIUM else 'Free'} users.")
    if apks > max_apk:
        raise ValueError(f"ZIP contains {apks} .apk files — max {max_apk} allowed for {'Premium' if IS_PREMIUM else 'Free'} users.")


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing env TELEGRAM_BOT_TOKEN / PAYLOAD_CHAT_ID")
        sys.exit(1)
    asyncio.create_task(cancel_watchdog())

    edit("🟢 Job started! Preparing DEX Compile engine on cloud server...", parse_mode="HTML")

    work_dir = Path(tempfile.gettempdir()) / ("dexcompile_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        ext = Path(FILENAME).suffix or ".bin"
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
                    filename = FILENAME or "download"
                    tg_url = TG_FILE_PATH if TG_FILE_PATH.startswith("http") else f"{API}/file/{TG_FILE_PATH}"
                    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120, read=300)) as client:
                        async with client.stream("GET", tg_url) as resp:
                            resp.raise_for_status()
                            total = int(resp.headers.get("content-length") or 0)
                            check_download_size(total)
                            done = 0
                            with open(dest, "wb") as fh:
                                async for chunk in resp.aiter_bytes(65536):
                                    if CANCELLED["v"]:
                                        raise JobCancelled()
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
                filename = FILENAME or "download"
                dl_method[0] = "📥 Downloading via MTProto (Pyrogram)..."
                await on_dl(0.0)
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "download_file.py", str(dest),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                dl_logs = []
                while True:
                    if CANCELLED["v"]:
                        proc.kill()
                        raise JobCancelled()
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
            check_zip_limits(dest)
        except ValueError as e:
            edit(f"❌ {e}", keep_button=False)
            return

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Preparing DEX compile...")

        last_prog = [0, ""]
        async def on_progress(pct: int, label: str = "🛠️ Compiling to .dex..."):
            if pct - last_prog[0] < 5 and label == last_prog[1]:
                return
            last_prog[0], last_prog[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)}")

        mode = MODE
        if mode == "auto":
            fname = FILENAME.lower()
            if fname.endswith(".smali") or (fname.endswith(".zip") and _zip_has(dest, ".smali")):
                mode = "smali"
            elif fname.endswith((".java", ".jar", ".class")) or (fname.endswith(".zip") and _zip_has_java(dest)):
                mode = "java"
            else:
                raise ValueError("Could not detect source type. Use the compile button for Smali or Java.")

        try:
            if mode == "smali":
                result = await compile_smali_to_dex(dest, work_dir, on_progress)
                caption = f"✅ Compiled Smali → <b>.dex</b> — Powered By @Ghostofhackers"
                done_msg = "✅ Smali → DEX compile complete!"
            else:
                result = await compile_java_to_dex(dest, work_dir, on_progress)
                caption = f"✅ Compiled Java → <b>.dex</b> — Powered By @Ghostofhackers"
                done_msg = "✅ Java → DEX compile complete!"
        except asyncio.TimeoutError:
            edit("⏰ Timeout! The input is too large to compile.", keep_button=False)
            return
        except ValueError as e:
            edit(f"❌ {e}", keep_button=False)
            return
        except Exception as e:
            await send_error_log(work_dir, e, "DEX Compile crashed")
            return

        await on_progress(100, done_msg)
        edit("📦 Packaging .dex file...")

        safe_name = re.sub(r'[^A-Za-z0-9._-]+', "_", FILENAME)[:60] or "file"
        orig_stem = Path(safe_name).stem or "code"
        up_last = [0]
        async def on_up(pct: int):
            if pct < up_last[0] or pct - up_last[0] < 2: return
            up_last[0] = pct
            edit(f"{done_msg}\n📤 Sending .dex...\n\n{progress_bar(pct)}")

        try:
            http_ok = False
            MAX_HTTP_UPLOAD = 50 * 1024 * 1024
            if result.stat().st_size <= MAX_HTTP_UPLOAD:
                try:
                    with open(result, "rb") as doc_f:
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
                    sys.executable, "upload_file.py", str(result), caption,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                while True:
                    if CANCELLED["v"]:
                        proc.kill()
                        raise JobCancelled()
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
            edit("✅ DEX compile complete! .dex file delivered. 🔥", keep_button=False)

            if JOB_ID:
                notify_app("FINAL_ZIP_URL:telegram_direct_upload")
        except Exception as e:
            await send_error_log(work_dir, e, "Result upload failed")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _zip_has(zip_path: Path, suffix: str) -> bool:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            return any(n.lower().endswith(suffix) for n in zf.namelist())
    except Exception:
        return False


def _zip_has_java(zip_path: Path) -> bool:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        return any(n.lower().endswith((".java", ".jar", ".class")) for n in names)
    except Exception:
        return False


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except JobCancelled:
        pass
