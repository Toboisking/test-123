import asyncio
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from html import unescape
from pathlib import Path
from urllib.parse import unquote

from zip_utils import extract_archive, extract_nested_archives

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
FILE_URL = os.environ.get("PAYLOAD_FILE_URL", "")
TG_FILE_PATH = os.environ.get("PAYLOAD_TG_FILE_PATH", "")
CHAT_ID = os.environ.get("PAYLOAD_CHAT_ID", "")
MESSAGE_ID = os.environ.get("PAYLOAD_MESSAGE_ID", "")
FILENAME = os.environ.get("PAYLOAD_FILENAME", "download")
JOB_ID = os.environ.get("PAYLOAD_JOB_ID", "")
IS_ADMIN = os.environ.get("PAYLOAD_IS_ADMIN", "False").lower() == "true"
IS_PREMIUM = os.environ.get("PAYLOAD_IS_PREMIUM", "False").lower() == "true"
GDB_SCRIPT = os.environ.get("PAYLOAD_GDB_SCRIPT", "")
USER_ID = os.environ.get("PAYLOAD_USER_ID", CHAT_ID)
REPORT_URL = os.environ.get("PAYLOAD_REPORT_URL", "")
REPORT_TOKEN = BOT_TOKEN
GHIDRA_HOME = Path(os.environ.get("GHIDRA_HOME", "/opt/ghidra"))
ANALYZE_HEADLESS = GHIDRA_HOME / "support" / "analyzeHeadless"
SCRIPT_DIR = Path(__file__).resolve().parent / "ghidra_scripts"
MAX_DOWNLOAD_MB = 2000

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


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


class EtaTracker:
    def __init__(self, smooth: int = 8):
        self.samples = []  # (pct, monotonic_time)
        self.smooth = smooth

    def eta(self, pct: float) -> float | None:
        pct = float(pct)
        if pct < 0:
            return None
        now = time.monotonic()
        self.samples.append((pct, now))
        self.samples = self.samples[-40:]
        if pct <= 0 or len(self.samples) < 3:
            return None
        recent = self.samples[-self.smooth:]
        p0, t0 = recent[0]
        p1, t1 = recent[-1]
        dt = t1 - t0
        dp = p1 - p0
        if dt <= 0 or dp <= 0:
            return None
        rate = dp / dt
        return (100 - p1) / rate


def proc_cpu_usage(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
        return int(parts[13]) + int(parts[14])
    except Exception:
        return -1


def _detect_heap_mb() -> int:
    try:
        with open("/proc/meminfo", "r", errors="replace") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    total_mb = kb // 1024
                    if total_mb <= 0:
                        return 4096
                    return max(4096, min(int(total_mb * 0.7), 14336))
    except Exception:
        pass
    return 4096


def apply_memory_settings():
    env_val = os.environ.get("JAVA_MAX_MEM", "").strip()
    if env_val:
        mem = env_val
    else:
        mem = f"{_detect_heap_mb()}M"
    props = GHIDRA_HOME / "support" / "launch.properties"
    try:
        text = props.read_text(errors="replace")
        new = re.sub(r"^JAVA_MAX_MEM\s*=.*$", f"JAVA_MAX_MEM={mem}", text, flags=re.M)
        if new == text and "JAVA_MAX_MEM" not in text:
            new = text.rstrip("\n") + f"\nJAVA_MAX_MEM={mem}\n"
        if new != text:
            props.write_text(new)
        log.info("JAVA_MAX_MEM set to %s", mem)
    except Exception as e:
        log.warning("Could not set JAVA_MAX_MEM: %s", e)


def resolve_drive_url(url: str):
    if "drive.google.com" not in url:
        return url
    m = re.search(r"/file/d/([^/?#]+)", url)
    if not m:
        m = re.search(r"[?&]id=([^&#]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return url


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
                            url = (f"https://drive.usercontent.google.com/download"
                                   f"?id={fid}&export=download&confirm={m.group(1)}")
                            continue
                        if "Google Drive" in html or "drive.google" in html:
                            raise ValueError("Google Drive file not accessible (check link permission / sharing).")
                    m = re.search(r'href="(https?://download[0-9]+\.mediafire\.com/[^"]+)"', html)
                    if m:
                        url = unescape(m.group(1))
                        continue
                    raise ValueError("The link is a webpage, not a direct file.")

                filename = "download.bin"
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


ELF_ARCH_REMAP = {
    "aarch64": "ARM:LE:64:v8",
    "arm": "ARM:LE:32:v8",
    "x86-64": "x86:LE:64:default",
    "x86_64": "x86:LE:64:default",
    "intel 80386": "x86:LE:32:default",
    "i386": "x86:LE:32:default",
    "mips": "MIPS:LE:32:default",
    "powerpc": "PowerPC:LE:32:default",
    "risc-v": "RISCV:LE:64:default",
}


def guess_language_for(file_type: str) -> str:
    ftype = (file_type or "").lower()
    for key, lang in ELF_ARCH_REMAP.items():
        if key in ftype:
            return lang
    return ""


async def run_ghidra(file_path: Path, work_dir: Path, on_progress, extra_import_args=None) -> dict:
    project_dir = work_dir / "project"
    project_dir.mkdir(parents=True)
    out_c = work_dir / "decompiled.c"
    out_meta = work_dir / "info.txt"

    cmd = [
        str(ANALYZE_HEADLESS),
        str(project_dir),
        "Proj",
        "-overwrite",
        "-import", str(file_path),
        "-scriptPath", str(SCRIPT_DIR),
        "-postScript", "DecompileAll.java",
        str(out_c), str(out_meta),
        "-deleteProject",
        "-max-cpu", "4",
        "-analysisTimeoutPerFile", "5400",
    ]
    if extra_import_args:
        cmd[6:6] = extra_import_args
    log.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    tail = []
    await on_progress(5, "📥 Importing file into Ghidra...")

    SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    async def read_stream():
        last_activity = time.monotonic()
        last_cpu = proc_cpu_usage(proc.pid)
        while True:
            if CANCELLED["v"]:
                proc.kill()
                raise JobCancelled()
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=20)
                last_activity = time.monotonic()
            except asyncio.TimeoutError:
                cpu = proc_cpu_usage(proc.pid)
                if cpu > last_cpu:
                    last_cpu = cpu
                    last_activity = time.monotonic()
                elif time.monotonic() - last_activity >= 1800:
                    proc.kill()
                    raise RuntimeError("Ghidra stalled: no CPU activity for 30 minutes")
                await on_progress(-1, "🔧 Analyzing binary with Ghidra...")
                continue
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            tail.append(line)
            del tail[:-60]
            low = line.lower()
            if "analyzing" in low or "processing" in low:
                await on_progress(20, "🔧 Analyzing binary with Ghidra...")
            m = re.search(r"DECOMP_PROGRESS\s+(\d+)/(\d+)", line)
            if m:
                done, total = int(m.group(1)), int(m.group(2))
                pct = int(20 + 75 * (done / total)) if total else 20
                await on_progress(pct, f"🧠 Decompiling functions {done}/{total}...")
        return await proc.wait()

    try:
        rc = await asyncio.wait_for(read_stream(), timeout=7200)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError("Ghidra analysis timed out")
    log.info("analyzeHeadless exit=%s", rc)
    return {"c": out_c, "meta": out_meta, "tail": "\n".join(tail[-40:]), "returncode": rc}


def send_document(file_path: Path, caption: str, filename: str):
    with open(file_path, "rb") as fh:
        resp = httpx.post(
            f"{API}/sendDocument",
            data={"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"},
            files={"document": (filename, fh, "application/zip")},
            timeout=180,
        )
    return resp.json()


async def run_gdb(file_path: Path, work_dir: Path) -> None:
    edit("🐞 <b>Running GDB — Connecting to Ghost's Server…</b>", parse_mode="HTML")
    cmds = [c.strip() for c in re.split(r"[;\n]+", GDB_SCRIPT) if c.strip()][:20]
    if not cmds:
        cmds = ["info files"]
    cmdline = ["gdb", "-q", "-batch", "-ex", "set pagination off", "-ex", "set confirm off"]
    for c in cmds:
        cmdline += ["-ex", c]
    cmdline += ["--", str(file_path)]
    log.info("gdb cmd: %s", " ".join(cmdline))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmdline, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        edit("⏰ GDB timed out (180s). Try simpler commands.")
        return
    text = out.decode(errors="replace")
    log.info("GDB_OUT_LEN=%s HEAD=%s", len(text), text[:300].replace("\n", " | "))
    if not text.strip():
        edit("❌ GDB gave no output. Check the command syntax.")
        return
    if len(text) <= 3500:
        from html import escape
        edit("<pre>" + escape(text[:3500]) + "</pre>", parse_mode="HTML")
    else:
        tmp = work_dir / "gdb_output.txt"
        try:
            tmp.write_text(text, encoding="utf-8", errors="replace")
            send_document(tmp, "🐞 <b>GDB output delivered</b> — Powered By @Ghostofhackers", "gdb_output.txt")
            edit("✅ <b>GDB output sent</b> as file (" + str(len(text)) + " chars).", parse_mode="HTML")
        except Exception:
            from html import escape
            edit("<pre>" + escape(text[:3500]) + "</pre>", parse_mode="HTML")


def check_zip_limits(file_path: Path):
    if IS_ADMIN:
        return
    if Path(FILENAME).suffix.lower() != ".zip":
        return
    import zipfile
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


def _identify_file(file_path: Path) -> str:
    try:
        res = subprocess.run(["file", "-b", str(file_path)], capture_output=True, text=True, timeout=30)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception as e:
        log.warning("file cmd failed: %s", e)
    try:
        head = file_path.read_bytes()[:16]
    except Exception:
        return "unknown"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z archive"
    if head[:2] in (b"\x1f\x8b", b"BZ", b"\xfd7zXZ"):
        return "compressed archive (gz/bz2/xz)"
    if head.startswith(b"Rar!"):
        return "rar archive"
    return "unknown binary"


def _extract_other_archive(dest: Path, extract_dir: Path) -> bool:
    cmds = [
        ["bsdtar", "-xf", str(dest), "-C", str(extract_dir)],
        ["tar", "-xf", str(dest), "-C", str(extract_dir)],
        ["7z", "x", "-y", f"-o{extract_dir}", str(dest)],
    ]
    for cmd in cmds:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            if res.returncode == 0:
                log.info("extracted with %s", cmd[0])
                return True
        except FileNotFoundError:
            continue
        except Exception as e:
            log.warning("%s err: %s", cmd[0], str(e)[:100])
    return False


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing env TELEGRAM_BOT_TOKEN / PAYLOAD_CHAT_ID")
        sys.exit(1)
    asyncio.create_task(cancel_watchdog())

    edit("🟢 Job started! Preparing Ghidra engine on cloud server...", parse_mode="HTML")
    apply_memory_settings()

    job_start = time.monotonic()
    work_dir = Path(tempfile.gettempdir()) / ("ghidra_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        dest = work_dir / "input.bin"
        last = [-100.0]

        dl_method = ["📥 Downloading file..."]
        dl_eta = EtaTracker()
        async def on_dl(pct: float):
            if pct < last[0] or (pct - last[0] < 2.0 and pct < 100.0): return
            last[0] = pct
            line = f"{dl_method[0]}\n\n{progress_bar(pct)}"
            rem = dl_eta.eta(pct)
            if rem is not None and pct < 100:
                line += f"\n⏱️ ~{fmt_duration(rem)} remaining"
            edit(line)

        try:
            file_id = os.environ.get("PAYLOAD_FILE_ID", "")
            got_file = False
            if TG_FILE_PATH:
                try:
                    filename = FILENAME or "download.bin"
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
                filename = FILENAME or "download.bin"
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
            edit("❌ Download failed: " + str(e)[:300])
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

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Starting Ghidra analysis...")

        if GDB_SCRIPT:
            await run_gdb(dest, work_dir)
            return

        last = [0, "", time.monotonic()]
        eta_tracker = EtaTracker()
        SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

        async def on_progress(pct: int, label: str = "🧠 Analyzing..."):
            now = time.monotonic()
            changed = pct >= 0 and (pct > last[0] or label != last[1])
            heartbeat = now - last[2] >= 10
            if not changed and not heartbeat:
                return
            if pct >= 0:
                last[0] = max(pct, last[0])
            last[1], last[2] = label, now
            frame = SPIN[int(now) % len(SPIN)]
            bar_pct = last[0]
            line = f"{label}\n{progress_bar(bar_pct)}"
            rem = eta_tracker.eta(pct)
            if rem is not None:
                line += f"\n⏱️ ~{fmt_duration(rem)} remaining"
            else:
                line += f"\n{frame} working... {fmt_duration(now - job_start)} elapsed"
            edit(line)

        out_files = []

        # Check if downloaded file is a ZIP archive containing multiple binaries (Batch Decompile)
        if zipfile.is_zipfile(dest):
            extract_dir = work_dir / "extracted_batch"
            extract_dir.mkdir(parents=True, exist_ok=True)
            try:
                extract_archive(dest, extract_dir)
            except Exception as e:
                log.warning("ZIP extract error: %s", e)
            extract_nested_archives(extract_dir, depth=0)

            candidates = []
            is_apk = filename.lower().endswith(".apk")
            for root, dirs, files in os.walk(extract_dir):
                for f in files:
                    fp = Path(root) / f
                    ext = fp.suffix.lower()
                    if is_apk:
                        # For APKs, only decompile native .so files
                        if ext == ".so":
                            candidates.append(fp)
                    else:
                        if ext in [".so", ".dll", ".exe", ".elf", ".apk", ".bin", ".jar", ".o", ".dylib"] or (not ext and fp.stat().st_size > 1024):
                            candidates.append(fp)

            if len(candidates) > 5 and not IS_ADMIN:
                edit(f"⚠️ <b>Batch Limit Exceeded!</b>\nArchive contains <b>{len(candidates)} binary files</b>. Maximum batch limit is <b>5 files</b> per ZIP.", parse_mode="HTML")
                return

            if len(candidates) >= 1:
                edit(f"📦 <b>Batch / APK Detected!</b> Found {len(candidates)} binary file(s). Starting multi-file decompilation...", parse_mode="HTML")
                for idx, bin_path in enumerate(candidates, start=1):
                    edit(f"⚙️ <b>Processing ({idx}/{len(candidates)}):</b> <code>{bin_path.name}</code>...", parse_mode="HTML")
                    try:
                        res = await asyncio.wait_for(
                            run_ghidra(bin_path, work_dir / f"analysis_{idx}", on_progress), timeout=3600
                        )
                        bname = bin_path.stem
                        if res["c"].exists() and res["c"].stat().st_size > 0:
                            out_files.append((f"{bname}.c", res["c"]))
                        if res["meta"].exists() and res["meta"].stat().st_size > 0:
                            out_files.append((f"{bname}_info.txt", res["meta"]))
                    except Exception as e:
                        log.warning("Batch file %s failed: %s", bin_path.name, e)
        elif not out_files:
            # Not a zip — try other archive formats (7z, tar, gz, xz, rar)
            extract_dir = work_dir / "extracted_other"
            extract_dir.mkdir(parents=True, exist_ok=True)
            if _extract_other_archive(dest, extract_dir):
                candidates = []
                for root, dirs, files in os.walk(extract_dir):
                    for f in files:
                        fp = Path(root) / f
                        ext = fp.suffix.lower()
                        if ext in [".so", ".dll", ".exe", ".elf", ".apk", ".bin", ".jar", ".o", ".dylib"] or (not ext and fp.stat().st_size > 1024):
                            candidates.append(fp)
                if candidates:
                    edit(f"📦 <b>Archive Detected!</b> Found {len(candidates)} binary file(s). Extracting & decompiling...", parse_mode="HTML")
                    for idx, bin_path in enumerate(candidates[:5], start=1):
                        edit(f"⚙️ <b>Processing ({idx}):</b> <code>{bin_path.name}</code>...", parse_mode="HTML")
                        try:
                            res = await asyncio.wait_for(
                                run_ghidra(bin_path, work_dir / f"analysis_o_{idx}", on_progress), timeout=3600
                            )
                            bname = bin_path.stem
                            if res["c"].exists() and res["c"].stat().st_size > 0:
                                out_files.append((f"{bname}.c", res["c"]))
                            if res["meta"].exists() and res["meta"].stat().st_size > 0:
                                out_files.append((f"{bname}_info.txt", res["meta"]))
                        except Exception as e:
                            log.warning("Archive file %s failed: %s", bin_path.name, e)

        if not out_files:
            ftype = _identify_file(dest)
            lang = guess_language_for(ftype) if ftype else ""
            retried = False
            try:
                result = await asyncio.wait_for(
                    run_ghidra(dest, work_dir / "analysis", on_progress), timeout=7200
                )
                if not (result["c"].exists() and result["c"].stat().st_size > 0) and lang:
                    retried = True
                    edit(f"🔄 Import failed, retrying with explicit processor <code>{lang}</code>...", parse_mode="HTML")
                    result = await asyncio.wait_for(
                        run_ghidra(
                            dest, work_dir / "analysis2", on_progress,
                            extra_import_args=["-processor", lang],
                        ),
                        timeout=7200,
                    )
                bname = Path(filename).stem or "decompiled"
                if result["c"].exists() and result["c"].stat().st_size > 0:
                    out_files.append((f"{bname}.c", result["c"]))
                if result["meta"].exists() and result["meta"].stat().st_size > 0:
                    out_files.append((f"{bname}_info.txt", result["meta"]))
                if not out_files:
                    log.warning("Ghidra produced no output. tail:\n%s", result.get("tail", "")[:1500])
                    fatal_tail = result.get("tail", "")
                elif retried:
                    log.info("Retry with language %s succeeded", lang)
            except TimeoutError:
                edit("⏰ Timeout! The file is too big or complex.", keep_button=False)
                return
            except Exception as e:
                log.exception("Ghidra crashed")
                edit("❌ Decompilation failed: " + str(e)[:300], keep_button=False)
                return

        if not out_files:
            ftype = _identify_file(dest)
            tail_snippet = ""
            key_errors = ""
            try:
                tail_snippet = (result.get("tail") or "")[-600:]
                err_lines = [
                    ln for ln in (result.get("tail") or "").splitlines()
                    if re.search(r"(?i)(error|exception|caused by|fatal|failed|no load spec|program loader)", ln)
                ]
                if err_lines:
                    key_errors = " · ".join(l.strip()[:160] for l in err_lines[-4:])
            except Exception:
                pass
            explain = ""
            if key_errors:
                explain = f"\n\n<b>Key log lines:</b>\n<pre>{key_errors[:700]}</pre>"
            edit(
                f"❌ Analysis failed or no output files generated.\n\n"
                f"📄 <b>File type detected:</b> {ftype[:200]}{explain}\n\n"
                f"<b>Ghidra log:</b>\n<pre>" + tail_snippet[:900] + "</pre>",
                parse_mode="HTML", keep_button=False
            )
            return

        edit("📦 Packaging results...")
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', "_", filename)[:60] or "file"
        orig_stem = Path(safe_name).stem or "decompiled"

        zip_path = work_dir / f"{orig_stem}_decompiled.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, fp in out_files:
                zf.write(fp, arcname)

        edit("✅ Decompilation complete! Sending ZIP...")
        
        caption = f"✅ Decompiled <b>{safe_name}</b> with Ghidra — Powered By @Ghostofhackers"
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
            edit("✅ Decompilation complete! ZIP file delivered. 🔥", keep_button=False)
            
            if JOB_ID:
                # App integration needs a direct link which we no longer have. 
                notify_app("FINAL_ZIP_URL:telegram_direct_upload")
        except Exception as e:
            edit(f"❌ Result ZIP ready, but upload failed: {e}", keep_button=False)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except JobCancelled:
        pass
