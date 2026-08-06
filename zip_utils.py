import logging
import struct
import subprocess
import zipfile
from pathlib import Path

log = logging.getLogger("zip_utils")


def extract_archive(zip_path: Path, out: Path) -> None:
    try:
        _extract_zip_python(zip_path, out)
    except Exception as e:
        log.warning("zipfile extract of %s failed (%s) — trying unzip CLI", zip_path.name, str(e)[:120])
        try:
            _extract_zip_unzip(zip_path, out)
        except Exception as e2:
            log.warning("unzip CLI failed (%s) — trying manual parser", str(e2)[:120])
            _extract_zip_manual(zip_path, out)


def extract_and_unpack(zip_path: Path, out: Path, max_depth: int = 3) -> None:
    extract_archive(zip_path, out)
    extract_nested_archives(out, depth=0, max_depth=max_depth)


def _extract_zip_python(zip_path: Path, out: Path) -> None:
    max_total = 4 * 1024 * 1024 * 1024
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        total = sum((i.file_size or 0) for i in infos)
        if total > max_total:
            raise ValueError(f"Archive expands to {total/1024/1024:.0f} MB — too large")
        for info in infos:
            name = info.filename
            if ".." in name or name.startswith("/"):
                continue
            zf.extract(info, out)


def _extract_zip_unzip(zip_path: Path, out: Path) -> None:
    cmds = [
        ["unzip", "-o", "-q", str(zip_path), "-d", str(out)],
        ["bsdtar", "-xf", str(zip_path), "-C", str(out)],
    ]
    for cmd in cmds:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if res.returncode in (0, 1):
                return
        except FileNotFoundError:
            continue
        except Exception as e:
            log.warning("%s err: %s", cmd[0], str(e)[:100])
    raise ValueError("no working unzip tool")


def _extract_zip_manual(zip_path: Path, out: Path) -> None:
    import zlib

    data = zip_path.read_bytes()
    eocd = -1
    for i in range(len(data) - 22, max(0, len(data) - 22 - 65535) - 1, -1):
        if data[i : i + 4] == b"PK\x05\x06":
            cmlen = struct.unpack("<H", data[i + 20 : i + 22])[0]
            if i + 22 + cmlen == len(data):
                eocd = i
                break
    if eocd < 0:
        raise ValueError("not a zip (no EOCD)")
    cd_count = struct.unpack("<H", data[eocd + 10 : eocd + 12])[0]
    cd_off = struct.unpack("<I", data[eocd + 16 : eocd + 20])[0]
    if cd_off == 0xFFFFFFFF or cd_count == 0xFFFF:
        loc = eocd - 20
        if loc >= 0 and data[loc : loc + 4] == b"PK\x06\x07":
            z64 = struct.unpack("<Q", data[loc + 8 : loc + 16])[0]
            cd_count = struct.unpack("<Q", data[z64 + 32 : z64 + 40])[0]
            cd_off = struct.unpack("<Q", data[z64 + 48 : z64 + 56])[0]
    pos = cd_off
    total = 0
    for _ in range(cd_count):
        if data[pos : pos + 4] != b"PK\x01\x02":
            break
        flags = struct.unpack("<H", data[pos + 8 : pos + 10])[0]
        method = struct.unpack("<H", data[pos + 10 : pos + 12])[0]
        csize = struct.unpack("<I", data[pos + 20 : pos + 24])[0]
        usize = struct.unpack("<I", data[pos + 24 : pos + 28])[0]
        fnlen = struct.unpack("<H", data[pos + 28 : pos + 30])[0]
        exlen = struct.unpack("<H", data[pos + 30 : pos + 32])[0]
        cmlen = struct.unpack("<H", data[pos + 32 : pos + 34])[0]
        lho = struct.unpack("<I", data[pos + 42 : pos + 46])[0]
        name = data[pos + 46 : pos + 46 + fnlen].decode("utf-8", "replace")
        extra = data[pos + 46 + fnlen : pos + 46 + fnlen + exlen]
        pos += 46 + fnlen + exlen + cmlen
        if (usize, csize, lho).count(0xFFFFFFFF):
            ei = 0
            while ei + 4 <= len(extra):
                eid, elen = struct.unpack("<HH", extra[ei : ei + 4])
                if eid == 1:
                    off = ei + 4
                    if usize == 0xFFFFFFFF:
                        usize = struct.unpack("<Q", extra[off : off + 8])[0]
                        off += 8
                    if csize == 0xFFFFFFFF:
                        csize = struct.unpack("<Q", extra[off : off + 8])[0]
                        off += 8
                    if lho == 0xFFFFFFFF:
                        lho = struct.unpack("<Q", extra[off : off + 8])[0]
                    break
                ei += 4 + elen
        if name.endswith("/") or ".." in name or name.startswith("/"):
            continue
        if flags & 1:
            continue
        if data[lho : lho + 4] != b"PK\x03\x04":
            continue
        lfnelen = struct.unpack("<H", data[lho + 26 : lho + 28])[0]
        lexlen = struct.unpack("<H", data[lho + 28 : lho + 30])[0]
        start = lho + 30 + lfnelen + lexlen
        raw = data[start : start + csize]
        try:
            if method == 0:
                payload = raw
            elif method == 8:
                payload = zlib.decompress(raw, -15)
            else:
                continue
        except Exception:
            continue
        total += len(payload)
        if total > 4 * 1024 * 1024 * 1024:
            raise ValueError("archive too large")
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def extract_nested_archives(root: Path, depth: int, max_depth: int = 3) -> None:
    if depth > max_depth:
        return
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            head = p.read_bytes()[:4]
        except Exception:
            continue
        if head[:4] != b"PK\x03\x04":
            continue
        sub = p.with_suffix(p.suffix + ".x")
        sub.mkdir(parents=True, exist_ok=True)
        try:
            extract_archive(p, sub)
        except Exception as e:
            log.warning("nested extract %s failed: %s", p.name, str(e)[:120])
        extract_nested_archives(sub, depth + 1, max_depth)