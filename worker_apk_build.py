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
log = logging.getLogger("worker_apk_build")

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
SDK_ROOT = os.environ.get("PAYLOAD_SDK_ROOT", "")
R8_JAR = os.environ.get("PAYLOAD_R8_JAR", "")
KOTLINC_ROOT = os.environ.get("PAYLOAD_KOTLINC", "")
MAX_DOWNLOAD_MB = 2000 if IS_ADMIN else 500

JAVA_EXTENSIONS = {".java"}
KOTLIN_EXTENSIONS = {".kt"}
MAX_SRC_FILES_FREE = 50
MAX_SRC_FILES_PREMIUM = 200

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

TOOL_LOG_FH = None


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


async def send_error_log(work_dir, exception_obj, title="APK Build failed"):
    import traceback
    err_str = traceback.format_exc()
    log.error("%s: %s", title, exception_obj)
    sent = False
    try:
        err_file = Path(work_dir) / "error.txt"
        text = f"❌ {title}:\n\n{err_str}"
        if TOOL_LOG_FH is not None:
            try:
                TOOL_LOG_FH.flush()
                log_path = Path(TOOL_LOG_FH.name)
                if log_path.exists():
                    tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-500:])
                    text += f"\n\n══════════ TOOL OUTPUT (last 500 lines) ══════════\n{tail}"
            except Exception:
                pass
        err_file.write_text(text)
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
                if TOOL_LOG_FH is not None:
                    try:
                        TOOL_LOG_FH.write(line + "\n")
                    except Exception:
                        pass
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


def find_dir(src_dir: Path, names) -> Path:
    for n in names:
        for cand in sorted(src_dir.rglob(n)):
            if cand.is_dir():
                return cand
    return None


def kotlinc_bin():
    if not KOTLINC_ROOT:
        return ""
    for cand in (os.path.join(KOTLINC_ROOT, "bin", "kotlinc"),
                 os.path.join(KOTLINC_ROOT, "kotlinc")):
        if os.path.isfile(cand):
            return cand
    return ""


def find_sdk():
    root = SDK_ROOT or os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or "/usr/local/lib/android/sdk"
    if not os.path.isdir(root):
        home_sdk = Path.home() / "Android" / "Sdk"
        if home_sdk.is_dir():
            root = str(home_sdk)
    bt_dir = os.path.join(root, "build-tools")
    build_tools = []
    if os.path.isdir(bt_dir):
        build_tools = sorted(
            (d for d in os.listdir(bt_dir) if os.path.isdir(os.path.join(bt_dir, d))),
            key=lambda v: [int(x) for x in re.findall(r"\d+", v)] or [0], reverse=True)
    platforms = []
    pl_dir = os.path.join(root, "platforms")
    if os.path.isdir(pl_dir):
        platforms = sorted(
            (d for d in os.listdir(pl_dir) if re.match(r"^android-\d+$", d)),
            key=lambda v: int(v.split("-")[1]), reverse=True)
    return {"root": root, "build_tools": build_tools, "platforms": platforms}


def get_tool(sdk, name):
    for v in sdk["build_tools"]:
        p = os.path.join(sdk["root"], "build-tools", v, name)
        if os.path.exists(p):
            return p
    return ""


def get_android_jar(sdk):
    for p in sdk["platforms"]:
        pj = os.path.join(sdk["root"], "platforms", p, "android.jar")
        if os.path.exists(pj):
            return pj, int(p.split("-")[1])
    return "", 0


def parse_manifest(path: Path):
    text = path.read_text(errors="replace")
    package = ""
    m = re.search(r'package="([^"]+)"', text)
    if m:
        package = m.group(1)
    min_sdk = None
    m = re.search(r'<uses-sdk[^>]*android:minSdkVersion\s*=\s*"(\d+)"', text)
    if m:
        min_sdk = int(m.group(1))
    target_sdk = None
    m = re.search(r'<uses-sdk[^>]*android:targetSdkVersion\s*=\s*"(\d+)"', text)
    if m:
        target_sdk = int(m.group(1))
    version_code = None
    m = re.search(r'android:versionCode\s*=\s*"(\d+)"', text)
    if m:
        version_code = int(m.group(1))
    version_name = None
    m = re.search(r'android:versionName\s*=\s*"([^"]+)"', text)
    if m:
        version_name = m.group(1)
    return {"package": package, "min_sdk": min_sdk, "target_sdk": target_sdk,
            "version_code": version_code, "version_name": version_name}


def ensure_manifest_package(manifest: Path, extract_dir: Path) -> str:
    text = manifest.read_text(errors="replace")
    m = re.search(r"<manifest[^>]*\bpackage\s*=\s*[\"']([^\"']+)[\"']", text)
    if m:
        return m.group(1)
    pkg = ""
    for gp in sorted(extract_dir.rglob("build.gradle")) + sorted(extract_dir.rglob("build.gradle.kts")):
        try:
            gt = gp.read_text(errors="replace")
        except Exception:
            continue
        gm = (re.search(r"namespace\s*=\s*\"([^\"]+)\"", gt)
              or re.search(r"applicationId\s*=\s*\"([^\"]+)\"", gt)
              or re.search(r"applicationId\s+\"([^\"]+)\"", gt)
              or re.search(r"namespace\s+\"([^\"]+)\"", gt))
        if gm:
            pkg = gm.group(1)
            break
    if not pkg:
        pkg = "com.example.app"
    text = text.replace("<manifest", f'<manifest package="{pkg}"', 1)
    manifest.write_text(text)
    return pkg


async def build_apk_from_source(input_path: Path, work_dir: Path, on_progress, sdk):
    await on_progress(15, "📦 Extracting source code...")
    extract_dir = work_dir / "src"
    extract_dir.mkdir(exist_ok=True)
    from zip_utils import extract_archive
    extract_archive(input_path, extract_dir)

    manifest = extract_dir / "AndroidManifest.xml"
    if not manifest.exists():
        m2 = sorted(extract_dir.rglob("AndroidManifest.xml"))
        if not m2:
            raise ValueError("No AndroidManifest.xml found in the ZIP (standard source layout required).")
        manifest = m2[0]
    mp = parse_manifest(manifest)
    if not mp["package"]:
        mp["package"] = ensure_manifest_package(manifest, extract_dir)

    res_dir = find_dir(extract_dir, ["res", "app/res"])
    assets_dir = find_dir(extract_dir, ["assets", "app/assets"])
    jni_dir = find_dir(extract_dir, ["jniLibs", "app/src/main/jniLibs", "libs"])
    libs_dir = find_dir(extract_dir, ["libs", "app/libs"])

    src_dirs = []
    for n in ["src", "java", "app/src/main/java", "app/src", "kotlin", "kt", "app/src/main/kotlin", "app/src/main/kt", "app/src/main"]:
        d = extract_dir / n
        if d.is_dir():
            src_dirs.append(d)
    java_files = []
    kt_files = []
    for d in src_dirs:
        java_files += find_inputs(d, JAVA_EXTENSIONS)
        kt_files += find_inputs(d, KOTLIN_EXTENSIONS)
    java_files = sorted(set(java_files))
    kt_files = sorted(set(kt_files))

    max_files = MAX_SRC_FILES_PREMIUM if IS_PREMIUM else MAX_SRC_FILES_FREE
    if not IS_ADMIN and (len(java_files) + len(kt_files)) > max_files:
        raise ValueError(f"Too many source files: {len(java_files) + len(kt_files)} — max {max_files} allowed for {'Premium' if IS_PREMIUM else 'Free'} users.")

    aapt2 = get_tool(sdk, "aapt2")
    zipalign = get_tool(sdk, "zipalign")
    apksigner = get_tool(sdk, "apksigner")
    android_jar, compile_api = get_android_jar(sdk)
    if not aapt2 or not zipalign or not apksigner:
        raise ValueError("Android SDK build-tools missing (aapt2/zipalign/apksigner required).")
    if not android_jar:
        raise ValueError("No Android platform (android.jar) found in the SDK.")

    target_sdk = mp["target_sdk"] or (compile_api or 34)
    min_sdk = mp["min_sdk"] or 4
    package = mp["package"] or "com.example.app"

    await on_progress(25, "📦 Compiling resources (aapt2)...")
    compiled_res = None
    if res_dir and any(res_dir.rglob("*")):
        compiled_res = work_dir / "compiled_res.zip"
        await run_tool([aapt2, "compile", "--dir", str(res_dir), "-o", str(compiled_res)], on_progress, "aapt2 compile")

    gen_dir = work_dir / "gen"
    gen_dir.mkdir(exist_ok=True)
    base_apk = work_dir / "base.apk"
    link_cmd = [aapt2, "link", "-o", str(base_apk), "-I", android_jar, "--manifest", str(manifest),
                "--java", str(gen_dir),
                "--min-sdk-version", str(min_sdk),
                "--target-sdk-version", str(target_sdk)]
    if mp["version_code"]:
        link_cmd += ["--version-code", str(mp["version_code"])]
    if mp["version_name"]:
        link_cmd += ["--version-name", mp["version_name"]]
    if assets_dir and assets_dir.is_dir():
        link_cmd += ["-A", str(assets_dir)]
    if compiled_res and compiled_res.exists():
        link_cmd += [str(compiled_res)]
    await run_tool(link_cmd, on_progress, "aapt2 link")
    if not base_apk.exists():
        raise ValueError("aapt2 link produced no APK (check AndroidManifest.xml).")

    class_dirs = []
    cp_parts = [android_jar]
    lib_jars = []
    if libs_dir and libs_dir.is_dir():
        lib_jars = find_inputs(libs_dir, {".jar"})
        cp_parts += lib_jars

    if kt_files:
        kbin = kotlinc_bin()
        if not kbin:
            raise ValueError("Kotlin compiler not found (send Java-only source or fix runner).")
        kotlin_out = work_dir / "kotlin_out"
        kotlin_out.mkdir(exist_ok=True)
        await on_progress(40, "☕ Compiling Kotlin sources...")
        kcmd = ["bash", kbin, "-classpath", android_jar, "-jvm-target", "1.8", "-d", str(kotlin_out)] + kt_files
        await run_tool(kcmd, on_progress, "kotlinc")
        class_dirs.append(kotlin_out)
        cp_parts.append(str(kotlin_out))
        stdlib = os.path.join(KOTLINC_ROOT, "lib", "kotlin-stdlib.jar")
        if os.path.isfile(stdlib):
            cp_parts.append(stdlib)
            lib_jars.append(stdlib)
        for extra in ("kotlin-stdlib-jdk8.jar", "kotlin-stdlib-jdk7.jar"):
            p = os.path.join(KOTLINC_ROOT, "lib", extra)
            if os.path.isfile(p):
                lib_jars.append(p)

    r_java = find_inputs(gen_dir, JAVA_EXTENSIONS)
    if java_files or r_java:
        java_out = work_dir / "java_out"
        java_out.mkdir(exist_ok=True)
        await on_progress(50, "☕ Compiling Java sources...")
        jcmd = ["javac", "-encoding", "UTF-8", "-classpath", ":".join(cp_parts), "-d", str(java_out)] + r_java + java_files
        await run_tool(jcmd, on_progress, "javac")
        class_dirs.append(java_out)
        if not any(java_out.rglob("*.class")) and not any(kotlin_out.rglob("*.class") if 'kotlin_out' in dir() else []):
            raise ValueError("No .class files generated (check your Java/Kotlin source).")

    dex_files = []
    class_files = []
    for d in class_dirs:
        class_files += [str(p) for p in sorted(Path(d).rglob("*.class"))]
    if class_files:
        await on_progress(65, "🧬 Converting classes → .dex (D8)...")
        dex_out = work_dir / "dex_out"
        dex_out.mkdir(exist_ok=True)
        if R8_JAR and os.path.isfile(R8_JAR):
            dcmd = ["java", "-Xmx4G", "-cp", R8_JAR, "com.android.tools.r8.D8", "--release", "--min-api", str(min_sdk), "--lib", android_jar, "--output", str(dex_out)] + class_files + lib_jars
        else:
            d8 = get_tool(sdk, "d8")
            if not d8:
                raise ValueError("d8 not found (need PAYLOAD_R8_JAR or build-tools d8).")
            dcmd = [d8, "--release", "--min-api", str(min_sdk), "--lib", android_jar, "--output", str(dex_out)] + class_files + lib_jars
        await run_tool(dcmd, on_progress, "d8")
        dex_files = sorted(dex_out.glob("*.dex"))
        if not dex_files:
            raise ValueError("d8 produced no .dex output.")

    await on_progress(80, "📦 Assembling APK...")
    unsigned_apk = work_dir / "unsigned.apk"
    extra = {}
    for d in dex_files:
        extra[d.name] = d
    if jni_dir and jni_dir.is_dir():
        for so in sorted(jni_dir.rglob("*.so")):
            rel = so.relative_to(jni_dir)
            extra[str(Path("lib") / rel)] = so
    _merge_apk(base_apk, unsigned_apk, extra)

    aligned = work_dir / "aligned.apk"
    await run_tool([zipalign, "-p", "-f", "4", str(unsigned_apk), str(aligned)], on_progress, "zipalign")
    await on_progress(88, "🔏 Signing APK...")
    keystore = await asyncio.to_thread(make_keystore, work_dir / "debug.keystore")
    signed_apk = work_dir / "signed.apk"
    await run_tool([apksigner, "sign", "--ks", str(keystore), "--ks-pass", "pass:android", "--key-pass", "pass:android",
                    "--min-sdk-version", str(min_sdk), "--out", str(signed_apk), str(aligned)], on_progress, "apksigner")
    await run_tool([apksigner, "verify", str(signed_apk)], on_progress, "apksigner verify")
    await on_progress(95, "✅ APK built!")
    return signed_apk, aligned


def make_keystore(path: Path) -> Path:
    import subprocess as sp
    if not path.exists():
        sp.run(["keytool", "-genkeypair", "-keystore", str(path), "-storepass", "android", "-keypass", "android",
                "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
                "-dname", "CN=Android Debug,O=Android,C=US"], check=True, capture_output=True)
    return path


class TolerantZipFile(zipfile.ZipFile):
    def _RealGetContents(self):
        super()._RealGetContents()
        for zinfo in self.filelist:
            zinfo._end_offset = None


def _merge_apk(base_apk: Path, out_apk: Path, extra: dict):
    with TolerantZipFile(base_apk) as zin:
        with zipfile.ZipFile(out_apk, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            for arc, src in extra.items():
                zout.write(str(src), str(arc))


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


async def upload_document(path: Path, caption: str):
    http_ok = False
    if path.stat().st_size <= 50 * 1024 * 1024:
        try:
            with open(path, "rb") as doc_f:
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
            sys.executable, "upload_file.py", str(path), caption,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        await proc.wait()
        if proc.returncode != 0:
            raise ValueError(f"MTProto Upload failed with code {proc.returncode}")


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing env TELEGRAM_BOT_TOKEN / PAYLOAD_CHAT_ID")
        sys.exit(1)
    asyncio.create_task(cancel_watchdog())

    edit("🟢 Job started! Preparing APK Build engine on cloud server...", parse_mode="HTML")

    work_dir = Path(tempfile.gettempdir()) / ("apkbuild_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        global TOOL_LOG_FH
        TOOL_LOG_FH = open(work_dir / "build_log.txt", "a", encoding="utf-8", errors="replace")
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

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Preparing APK build...")

        sdk = find_sdk()
        if not sdk["build_tools"] or not sdk["platforms"]:
            edit("❌ Android SDK build-tools/platforms not found on the runner.", keep_button=False)
            return

        last_prog = [0, ""]
        async def on_progress(pct: int, label: str = "📦 Building APK..."):
            if pct - last_prog[0] < 5 and label == last_prog[1]:
                return
            last_prog[0], last_prog[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)}")

        try:
            signed_apk, unsigned_apk = await build_apk_from_source(dest, work_dir, on_progress, sdk)
            done_msg = "✅ APK build complete!"
        except asyncio.TimeoutError:
            edit("⏰ Timeout! The source is too large to build.", keep_button=False)
            return
        except Exception as e:
            await send_error_log(work_dir, e, "APK Build failed")
            return

        await on_progress(100, done_msg)
        try:
            await upload_document(signed_apk, f"✅ <b>Signed APK</b> built from source — Powered By @Ghostofhackers")
            edit("📤 Sending unsigned APK...")
            await upload_document(unsigned_apk, f"✅ <b>Unsigned APK</b> built from source — Powered By @Ghostofhackers")
            edit("✅ APK build complete! Signed + Unsigned delivered. 🔥", keep_button=False)
            if JOB_ID:
                notify_app("FINAL_ZIP_URL:telegram_direct_upload")
        except Exception as e:
            await send_error_log(work_dir, e, "Result upload failed")
    finally:
        if TOOL_LOG_FH is not None:
            try:
                TOOL_LOG_FH.close()
            except Exception:
                pass
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except JobCancelled:
        pass
