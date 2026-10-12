"""Bounded, local-only Weixin contact.db compatibility probe.

Only reads the currently open contact database and Weixin process memory.
No networking, injection, source-database writes, key files, or plaintext caches.
SQLCipher page layout cross-checked against wechat-cli commit
a3789232d4f79bf0b30634d9dadbce71e4acd601 (keys/common.py, core/crypto.py).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import time

import psutil
from Crypto.Cipher import AES
from wechat_wal import apply_committed_wal

WORKSPACE = Path(__file__).resolve().parents[1]
REPORT = WORKSPACE / "reports" / "wechat-contacts-verification.json"
EXE = Path("C:/Program Files/Tencent/Weixin/Weixin.exe")
PAGE = 4096
RESERVE = 80


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def installed_version():
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wt.DWORD
    version.GetFileVersionInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = wt.BOOL
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wt.LPCWSTR,
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.UINT)]
    version.VerQueryValueW.restype = wt.BOOL
    unused = wt.DWORD()
    size = version.GetFileVersionInfoSizeW(str(EXE), ctypes.byref(unused))
    if not size:
        raise RuntimeError("Could not read installed Weixin version")
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(EXE), 0, size, data):
        raise RuntimeError("Could not load installed Weixin version")
    ptr = ctypes.c_void_p()
    length = wt.UINT()
    if not version.VerQueryValueW(data, "\\", ctypes.byref(ptr), ctypes.byref(length)) or length.value < 52:
        raise RuntimeError("Weixin version structure missing")
    fields = struct.unpack("<13I", ctypes.string_at(ptr, 52))
    if fields[0] != 0xFEEF04BD:
        raise RuntimeError("Weixin version structure invalid")
    return ".".join(map(str, (fields[2] >> 16, fields[2] & 65535, fields[3] >> 16, fields[3] & 65535)))


def discover_active():
    config = Path(os.environ["APPDATA"]) / "Tencent/xwechat/config"
    roots = set()
    for ini in config.glob("*.ini"):
        raw = ini.read_bytes()[:1024]
        for encoding in ("utf-8", "gbk"):
            try:
                value = raw.decode(encoding).strip()
            except UnicodeDecodeError:
                continue
            if value and not any(c in value for c in "\r\n\0"):
                candidate = Path(value)
                if candidate.is_dir():
                    roots.add(candidate)
            break
    contacts = sorted({p.resolve() for root in roots for p in
                       (root / "xwechat_files").glob("*/db_storage/contact/contact.db")})
    active = {}
    for process in psutil.process_iter(["name", "exe"]):
        if (process.info["name"] or "").lower() != "weixin.exe":
            continue
        if Path(process.info["exe"] or "").resolve() != EXE.resolve():
            continue
        try:
            opened = [Path(f.path) for f in process.open_files()]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for db in contacts:
            storage = db.parent.parent
            if any(p.is_relative_to(storage) for p in opened):
                active.setdefault(db, []).append(process.pid)
    if len(active) != 1:
        raise RuntimeError(f"Expected one active account; matched {len(active)}")
    db, pids = next(iter(active.items()))
    return db, pids, len(contacts)


def signature(path):
    if not path.exists():
        return None
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def snapshot(db):
    wal = Path(str(db) + "-wal")
    for _ in range(3):
        before = signature(db), signature(wal)
        data = db.read_bytes()
        wal_data = wal.read_bytes() if wal.exists() else b""
        second_data = db.read_bytes()
        second_wal = wal.read_bytes() if wal.exists() else b""
        after = signature(db), signature(wal)
        if before == after and data == second_data and wal_data == second_wal:
            return data, wal_data
        time.sleep(0.2)
    raise RuntimeError("Contact database changed during all three snapshot attempts")


def derive_mac_key(key, salt):
    return hashlib.pbkdf2_hmac("sha512", key, bytes(b ^ 0x3A for b in salt), 2, 32)


def verify_page(page, number, mac_key):
    start = 16 if number == 1 else 0
    calculated = hmac.new(mac_key, page[start:PAGE - 64] + struct.pack("<I", number), hashlib.sha512).digest()
    return hmac.compare_digest(calculated, page[PAGE - 64:])


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]


def kernel_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.CloseHandle.restype = wt.BOOL
    kernel.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
    kernel.VirtualQueryEx.restype = ctypes.c_size_t
    kernel.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype = wt.BOOL
    return kernel


def scan_contact_key(pids, first_page, seconds=45):
    kernel = kernel_api()
    salt = first_page[:16]
    salt_hex = salt.hex().encode()
    pattern = re.compile(rb"[xX]'([0-9a-fA-F]{64,256})'")
    wide_pattern = re.compile(rb"[xX]\x00'\x00((?:[0-9a-fA-F]\x00){64,256})'\x00")
    started = time.monotonic()
    last_progress = started
    scanned = patterns = regions = 0
    tested = set()
    lengths = {}
    matching_salts = wide_patterns = 0
    for pid in pids:
        handle = kernel.OpenProcess(0x0010 | 0x0400, False, pid)
        if not handle:
            raise RuntimeError(f"Weixin read-only process access failed: {ctypes.get_last_error()}")
        try:
            addr = 0
            mbi = MBI()
            while addr < 0x7FFFFFFFFFFF:
                if time.monotonic() - started > seconds:
                    raise TimeoutError("Bounded contact-key scan reached its time limit")
                if not kernel.VirtualQueryEx(handle, addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
                    break
                base, size = mbi.BaseAddress or 0, mbi.RegionSize
                nxt = base + size
                if nxt <= addr:
                    break
                addr = nxt
                if mbi.State != 0x1000 or mbi.Protect & 0x100 or (mbi.Protect & 0xFF) not in {2, 4, 8, 0x20, 0x40, 0x80}:
                    continue
                regions += 1
                for offset in range(0, size, 4 * 1024 * 1024):
                    if time.monotonic() - started > seconds:
                        raise TimeoutError("Bounded contact-key scan reached its time limit")
                    chunk_size = min(size - offset, 4 * 1024 * 1024 + 200)
                    buffer = ctypes.create_string_buffer(chunk_size)
                    read = ctypes.c_size_t()
                    kernel.ReadProcessMemory(handle, base + offset, buffer, chunk_size, ctypes.byref(read))
                    if not read.value:
                        continue
                    data = buffer.raw[:read.value]
                    scanned += read.value
                    candidates = [m.group(1).lower() for m in pattern.finditer(data)]
                    wide_candidates = [m.group(1).replace(b"\0", b"").lower()
                                       for m in wide_pattern.finditer(data)]
                    wide_patterns += len(wide_candidates)
                    for candidate in candidates + wide_candidates:
                        patterns += 1
                        lengths[len(candidate)] = lengths.get(len(candidate), 0) + 1
                        matching_salts += int(candidate[-32:] == salt_hex)
                        # The same encryption key may occur with another database's
                        # salt. Verify every candidate against contact.db itself.
                        candidate_key = bytes.fromhex(candidate[:64].decode("ascii"))
                        if candidate_key in tested:
                            continue
                        tested.add(candidate_key)
                        if verify_page(first_page, 1, derive_mac_key(candidate_key, salt)):
                            emit("contact_key_validated", scanned_mib=round(scanned / 2**20, 1),
                                 seconds=round(time.monotonic() - started, 2))
                            return bytearray(candidate_key)
                    if time.monotonic() - last_progress > 10:
                        emit("scan_progress", scanned_mib=round(scanned / 2**20, 1), regions=regions)
                        last_progress = time.monotonic()
        finally:
            kernel.CloseHandle(handle)
    raise RuntimeError(f"No validated contact key found; scanned {scanned // 2**20} MiB, "
                       f"{patterns} patterns, lengths={lengths}, unique_keys={len(tested)}, "
                       f"matching_salts={matching_salts}, utf16_patterns={wide_patterns}")


def scan_config_key(pids, first_page, seconds=55):
    """Read-only WCDB Config.Cipher scan, HMAC gated for contact.db only.

    Object layout adapted from wcdb-key-tool commit
    79f1b5b92e12c66aa281b4a60a3c478b5f547dfa, not assumed compatible.
    """
    needle = b"com.Tencent.WCDB.Config.Cipher"
    mask = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")
    literal = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
    kernel = kernel_api()
    started = time.monotonic()
    stats = {"needle_occurrences": 0, "object_refs": 0, "valid_blobs": 0, "candidate_keys": 0}
    tested = set()
    for pid in pids:
        handle = kernel.OpenProcess(0x0010 | 0x0400, False, pid)
        if not handle:
            raise RuntimeError("Read-only Weixin process access failed")
        try:
            def read(addr, size):
                if time.monotonic() - started > seconds:
                    raise TimeoutError("Bounded WCDB Config.Cipher scan reached its time limit")
                if not 0x10000 <= addr < 0x800000000000 or not 0 < size <= 4 * 1024 * 1024 + 256:
                    return b""
                buffer = ctypes.create_string_buffer(size)
                got = ctypes.c_size_t()
                kernel.ReadProcessMemory(handle, addr, buffer, size, ctypes.byref(got))
                return buffer.raw[:got.value]

            regions = []
            addr = 0
            mbi = MBI()
            while addr < 0x7FFFFFFFFFFF:
                if not kernel.VirtualQueryEx(handle, addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
                    break
                base, size = mbi.BaseAddress or 0, mbi.RegionSize
                if base + size <= addr:
                    break
                addr = base + size
                if mbi.State == 0x1000 and not mbi.Protect & 0x100 and (mbi.Protect & 0xFF) in {2, 4, 8, 0x20, 0x40, 0x80}:
                    regions.append((base, size))

            def chunks():
                for base, size in regions:
                    for offset in range(0, size, 4 * 1024 * 1024):
                        yield base + offset, read(base + offset, min(size - offset, 4 * 1024 * 1024 + 128))

            addresses = set()
            for base, data in chunks():
                pos = data.find(needle)
                while pos >= 0:
                    addresses.add(base + pos)
                    if len(addresses) > 128:
                        raise RuntimeError("Unexpectedly many cipher-name references")
                    pos = data.find(needle, pos + 1)
            stats["needle_occurrences"] += len(addresses)
            emit("config_scan_name_pass", **stats, seconds=round(time.monotonic() - started, 2))
            pairs = [struct.pack("<QQ", address, len(needle)) for address in addresses]
            seen_nodes = set()
            for base, data in chunks():
                for pair in pairs:
                    pos = data.find(pair)
                    while pos >= 0:
                        node_addr = base + pos - 16
                        pos = data.find(pair, pos + 1)
                        if node_addr in seen_nodes:
                            continue
                        seen_nodes.add(node_addr)
                        stats["object_refs"] += 1
                        node = read(node_addr, 0x50)
                        if len(node) != 0x50 or struct.unpack_from("<Q", node, 0x10)[0] not in addresses:
                            continue
                        config_ptr = struct.unpack_from("<Q", node, 0x28)[0]
                        obj = read(config_ptr + 0x88, 0x28)
                        if len(obj) != 0x28:
                            continue
                        data_ptr, data_len = struct.unpack_from("<QQ", obj, 8)
                        if not 0 < data_len <= 1024:
                            continue
                        blob = read(data_ptr, data_len)
                        if len(blob) != data_len:
                            continue
                        stats["valid_blobs"] += 1
                        decoded = bytes(b ^ mask[i % len(mask)] for i, b in enumerate(blob))
                        for match in literal.finditer(decoded):
                            run = match.group(1)
                            starts = [0]
                            if len(run) > 96:
                                starts.extend(range(0, len(run) - 63, 32))
                                starts.append(len(run) - 64)
                            for start in set(starts):
                                candidate = bytes.fromhex(run[start:start + 64].decode("ascii"))
                                if candidate in tested:
                                    continue
                                tested.add(candidate)
                                stats["candidate_keys"] += 1
                                if verify_page(first_page, 1, derive_mac_key(candidate, first_page[:16])):
                                    emit("config_database_key_validated", **stats,
                                         seconds=round(time.monotonic() - started, 2))
                                    return bytearray(candidate), stats
        finally:
            kernel.CloseHandle(handle)
    raise RuntimeError("No validated Config.Cipher key: " + json.dumps(stats))


def decrypt_database(data, key):
    if len(data) < PAGE or len(data) % PAGE:
        raise RuntimeError("Unsupported encrypted database page length")
    mac_key = derive_mac_key(key, data[:16])
    result = bytearray()
    verified = zero_pages = 0
    for offset in range(0, len(data), PAGE):
        page = data[offset:offset + PAGE]
        pgno = offset // PAGE + 1
        if pgno != 1 and page == bytes(PAGE):
            result.extend(page)
            zero_pages += 1
            continue
        if not verify_page(page, pgno, mac_key):
            raise RuntimeError(f"Encrypted page authentication failed at page {pgno}")
        iv = page[PAGE - RESERVE:PAGE - RESERVE + 16]
        start = 16 if pgno == 1 else 0
        plain = AES.new(key, AES.MODE_CBC, iv).decrypt(page[start:PAGE - RESERVE])
        result.extend((b"SQLite format 3\0" if pgno == 1 else b"") + plain + bytes(RESERVE))
        verified += 1
    return result, verified, zero_pages


def inspect_contacts(data):
    # Disable WAL mode only in the private in-memory image: no source writes.
    data[18:20] = b"\x01\x01"
    conn = sqlite3.connect(":memory:")
    try:
        conn.deserialize(bytes(data))
        conn.execute("PRAGMA query_only=ON")
        check = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        if check != ["ok"]:
            raise RuntimeError("SQLite integrity_check failed on main-database snapshot")
        columns = [r[1] for r in conn.execute("PRAGMA table_info(contact)")]
        if not {"username", "nick_name", "remark"}.issubset(columns):
            raise RuntimeError("Expected contact fields missing")
        count, names, remarks = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN COALESCE(nick_name,'')!='' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN COALESCE(remark,'')!='' THEN 1 ELSE 0 END) FROM contact").fetchone()
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        types = [{"local_type": r[0], "delete_flag": r[1], "count": r[2]} for r in conn.execute(
            "SELECT local_type, delete_flag, COUNT(*) FROM contact GROUP BY local_type, delete_flag ORDER BY local_type, delete_flag")]
        return {"integrity_check": "ok", "contact_columns": columns,
                "table_names": tables, "contact_type_distribution": types,
                "contact_entity_count": count, "entities_with_nickname": names,
                "entities_with_remark": remarks, "friend_count": None,
                "friend_count_note": "Contact entities include groups, service accounts and other cached identities; no friend-only filter verified."}
    finally:
        conn.close()


def main():
    report = {"generated_utc": datetime.now(timezone.utc).isoformat(), "status": "running",
              "scope": "contact.db and its WAL only",
              "source_access": "read-only files and PROCESS_VM_READ | PROCESS_QUERY_INFORMATION",
              "key_persisted": False, "plaintext_database_persisted": False,
              "contact_values_printed": False, "network_requests": 0}
    key = None
    stage = "active_account_binding"
    try:
        report["tested_weixin_version"] = installed_version()
        db, pids, candidates = discover_active()
        report.update(account_directories_detected=candidates, active_accounts_matched=1, active_pids=pids)
        emit("active_account_bound", candidate_accounts=candidates, matched_accounts=1)
        stage = "snapshot"
        source, wal = snapshot(db)
        report.update(database_bytes=len(source), wal_bytes=len(wal), wal_applied=False,
                      plaintext_sqlite_header=source.startswith(b"SQLite format 3\0"),
                      snapshot_note="Main database only; pending WAL updates are not included.")
        stage = "contact_key_scan"
        key, scan_stats = scan_config_key(pids, source[:PAGE])
        report.update(key_scan_method="WCDB Config.Cipher; target-db HMAC gated", key_scan_statistics=scan_stats,
                      legacy_hex_scan_result="Previously failed; no valid target key")
        report["contact_key_hmac_validated"] = True
        stage = "page_decryption"
        plain, verified, zeros = decrypt_database(source, key)
        report.update(authenticated_pages=verified, zero_unallocated_pages=zeros)
        stage = "sqlite_contact_query"
        main_result = inspect_contacts(plain)
        report["main_database_check"] = {"integrity_check": main_result["integrity_check"],
                                         "contact_entity_count": main_result["contact_entity_count"]}
        stage = "wal_validation"
        encrypted_snapshot, wal_stats = apply_committed_wal(source, wal)
        report["wal_validation"] = wal_stats
        stage = "committed_snapshot_decryption"
        committed_plain, verified, zeros = decrypt_database(encrypted_snapshot, key)
        wal_stats["page_hmac_verified"] = wal_stats["applied_pages"] > 0
        report["output_snapshot_page_hmac_verified"] = True
        report.update(authenticated_pages=verified, zero_unallocated_pages=zeros,
                      wal_applied=wal_stats["applied_pages"] > 0,
                      snapshot_note=("Two identical byte reads with stable file metadata; committed WAL pages applied. "
                                     if wal_stats["applied_pages"] else
                                     "Two identical byte reads with stable file metadata; no committed WAL pages applied. ")
                                    + "No SQLite read-transaction lock, completeness not asserted.")
        stage = "committed_snapshot_contact_query"
        report.update(inspect_contacts(committed_plain))
        report["status"] = ("passed_database_and_wal_snapshot" if report["wal_applied"]
                            else "passed_main_database_snapshot")
    except Exception as exc:
        report.update(status="failed", failed_stage=stage, error=str(exc))
    finally:
        if key is not None:
            key[:] = bytes(len(key))
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    emit("verification_result", **report)
    return 0 if report["status"].startswith("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
