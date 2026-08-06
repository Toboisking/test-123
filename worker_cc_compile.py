import asyncio
import json
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
log = logging.getLogger("worker_cc_compile")

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
NDK_BIN = os.environ.get("PAYLOAD_NDK_BIN", "")
MAX_DOWNLOAD_MB = 2000

CC_EXTENSIONS = {".c"}
CPP_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c++", ".cp"}
MAX_SRC_FILES_FREE = 5
MAX_SRC_FILES_PREMIUM = 20

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


async def send_error_log(work_dir, exception_obj, title="C/C++ Compile failed"):
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


def is_zip_file(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"PK\x03\x04"
    except Exception:
        return False


def detect_cc_language(path: Path) -> str:
    """Return 'c', 'cpp', or '' based on file content."""
    try:
        data = path.read_bytes()[:16384]
    except Exception:
        return ""
    if b"\x00" in data[:1024]:
        return ""
    text = data.decode("utf-8", errors="replace")
    cpp_markers = ("#include <iostream>", "#include <vector>", "#include <string>",
                   "#include <map>", "#include <unordered_map>", "std::", "namespace ",
                   "using namespace", "template<", "template <", "class ", "public:",
                   "private:", "protected:", "cout <<", "cin >>")
    c_markers = ("#include <stdio.h>", "#include <stdlib.h>", "#include <string.h>",
                 "#include <unistd.h>", "printf(", "malloc(", "calloc(", "realloc(",
                 "free(", "typedef struct", "#include <pthread.h>")
    if any(m in text for m in cpp_markers):
        return "cpp"
    if any(m in text for m in c_markers):
        return "c"
    return ""


def find_ndk_bin() -> str:
    if NDK_BIN and os.path.isdir(NDK_BIN):
        return NDK_BIN
    for env in ("ANDROID_NDK_LATEST_HOME", "ANDROID_NDK_HOME", "ANDROID_NDK_ROOT"):
        root = os.environ.get(env)
        if root and os.path.isdir(root):
            cand = os.path.join(root, "toolchains", "llvm", "prebuilt", "linux-x86_64", "bin")
            if os.path.isdir(cand):
                return cand
    sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if sdk:
        ndk_dir = os.path.join(sdk, "ndk")
        if os.path.isdir(ndk_dir):
            try:
                versions = sorted(os.listdir(ndk_dir), reverse=True)
            except Exception:
                versions = []
            for v in versions:
                cand = os.path.join(ndk_dir, v, "toolchains", "llvm", "prebuilt", "linux-x86_64", "bin")
                if os.path.isdir(cand):
                    return cand
    return ""


def find_clang(ndk_bin: str):
    for name in ("aarch64-linux-android24-clang", "aarch64-linux-android-clang"):
        c = os.path.join(ndk_bin, name)
        if os.path.exists(c):
            return c, os.path.join(ndk_bin, name + "++")
    return "", ""


async def compile_to_so(input_path: Path, work_dir: Path, on_progress) -> Path:
    await on_progress(20, "⚙️ Preparing C/C++ sources...")
    c_files: list = []
    cpp_files: list = []
    ext = Path(FILENAME).suffix.lower()

    if input_path.is_file():
        if ext in CC_EXTENSIONS:
            c_files = [str(input_path)]
        elif ext in CPP_EXTENSIONS:
            cpp_files = [str(input_path)]
        elif ext == ".zip" or is_zip_file(input_path):
            extract_dir = work_dir / "cc_src"
            extract_dir.mkdir(exist_ok=True)
            from zip_utils import extract_archive
            extract_archive(input_path, extract_dir)
            c_files = find_inputs(extract_dir, CC_EXTENSIONS)
            cpp_files = find_inputs(extract_dir, CPP_EXTENSIONS)
        elif (lang := detect_cc_language(input_path)):
            suffix = ".cpp" if lang == "cpp" else ".c"
            renamed = work_dir / f"source_0{suffix}"
            shutil.copyfile(input_path, renamed)
            if lang == "cpp":
                cpp_files = [str(renamed)]
            else:
                c_files = [str(renamed)]
        else:
            raise ValueError("Unsupported input. Send a .c/.cpp file or a ZIP containing C/C++ sources.")
    elif input_path.is_dir():
        c_files = find_inputs(input_path, CC_EXTENSIONS)
        cpp_files = find_inputs(input_path, CPP_EXTENSIONS)
    else:
        raise ValueError("Unsupported input for C/C++ compile.")

    total = len(c_files) + len(cpp_files)
    if total == 0:
        raise ValueError("No .c or .cpp source files found in the input.")
    max_files = MAX_SRC_FILES_PREMIUM if IS_PREMIUM else MAX_SRC_FILES_FREE
    if not IS_ADMIN and total > max_files:
        raise ValueError(f"Too many source files: {total} — max {max_files} allowed for {'Premium' if IS_PREMIUM else 'Free'} users.")

    ndk_bin = find_ndk_bin()
    if not ndk_bin:
        raise ValueError("Android NDK not found on the runner.")
    clang, clangxx = find_clang(ndk_bin)
    if not clang:
        raise ValueError("Android NDK clang not found in the toolchain.")

    obj_dir = work_dir / "obj"
    obj_dir.mkdir(exist_ok=True)
    objs = []

    safe_name = re.sub(r'[^A-Za-z0-9._-]+', "_", FILENAME)[:60] or "file"
    lib_name = f"lib_{Path(safe_name).stem or 'code'}.so"
    out_so = work_dir / lib_name

    if c_files and not cpp_files:
        await on_progress(40, "⚙️ Compiling + linking C sources...")
        await run_tool([clang, "-shared", "-fPIC", "-O2", "-std=c11", "-o", str(out_so)] + c_files, on_progress, "clang")
    elif cpp_files and not c_files:
        await on_progress(40, "⚙️ Compiling + linking C++ sources...")
        await run_tool([clangxx, "-shared", "-fPIC", "-O2", "-std=c++17", "-o", str(out_so)] + cpp_files, on_progress, "clang++")
    else:
        await on_progress(40, "⚙️ Compiling C/C++ → object files...")
        for i, f in enumerate(c_files):
            obj = obj_dir / f"c_{i}.o"
            await run_tool([clang, "-c", "-fPIC", "-O2", "-std=c11", "-o", str(obj), f], on_progress, "clang")
            if not obj.exists():
                raise RuntimeError(f"clang produced no object file for {f}")
            objs.append(str(obj))
        for i, f in enumerate(cpp_files):
            obj = obj_dir / f"cpp_{i}.o"
            await run_tool([clangxx, "-c", "-fPIC", "-O2", "-std=c++17", "-o", str(obj), f], on_progress, "clang++")
            if not obj.exists():
                raise RuntimeError(f"clang++ produced no object file for {f}")
            objs.append(str(obj))
        if not objs:
            raise ValueError("No object files produced from C/C++ sources.")
        await on_progress(70, "🔗 Linking shared library (.so)...")
        await run_tool([clangxx, "-shared", "-O2", "-o", str(out_so)] + objs, on_progress, "link")

    if not out_so.exists():
        raise ValueError("Linker produced no .so output.")
    await on_progress(85, "✅ .so built!")
    return out_so


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

    edit("🟢 Job started! Preparing C/C++ Compile engine on cloud server...", parse_mode="HTML")

    work_dir = Path(tempfile.gettempdir()) / ("cccompile_" + os.urandom(8).hex())
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

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Preparing C/C++ compile...")

        last_prog = [0, ""]
        async def on_progress(pct: int, label: str = "⚙️ Compiling to .so..."):
            if pct - last_prog[0] < 5 and label == last_prog[1]:
                return
            last_prog[0], last_prog[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)}")

        try:
            result = await compile_to_so(dest, work_dir, on_progress)
            caption = f"✅ Compiled C/C++ → <b>.so</b> (Android ARM64) — Powered By @Ghostofhackers"
            done_msg = "✅ C/C++ → .so compile complete!"
        except asyncio.TimeoutError:
            edit("⏰ Timeout! The input is too large to compile.", keep_button=False)
            return
        except ValueError as e:
            edit(f"❌ {e}", keep_button=False)
            return
        except Exception as e:
            await send_error_log(work_dir, e, "C/C++ Compile crashed")
            return

        await on_progress(100, done_msg)
        edit("📦 Packaging .so file...")

        up_last = [0]
        async def on_up(pct: int):
            if pct < up_last[0] or pct - up_last[0] < 2: return
            up_last[0] = pct
            edit(f"{done_msg}\n📤 Sending .so...\n\n{progress_bar(pct)}")

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
            edit("✅ C/C++ compile complete! .so file delivered. 🔥", keep_button=False)

            if JOB_ID:
                notify_app("FINAL_ZIP_URL:telegram_direct_upload")
        except Exception as e:
            await send_error_log(work_dir, e, "Result upload failed")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except JobCancelled:
        pass
