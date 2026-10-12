"""Verify one active Weixin message shard, with aggregate-only output.

Keys, plaintext database and sampled message bodies remain in process memory.
No attachments, network calls, transcript exports or Weixin writes.
"""

from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import sqlite3

import zstandard

from verify_wechat_contacts import (
    WORKSPACE, PAGE, discover_active, installed_version, signature,
    snapshot, scan_config_key, decrypt_database, emit,
)
from wechat_wal import apply_committed_wal

REPORT = WORKSPACE / "reports" / "wechat-messages-verification.json"
MAX_TEXT_BYTES = 4 * 1024 * 1024


def inspect_messages(plain):
    plain[18:20] = b"\x01\x01"
    conn = sqlite3.connect(":memory:")
    try:
        conn.deserialize(bytes(plain))
        conn.execute("PRAGMA query_only=ON")
        if list(conn.execute("PRAGMA integrity_check")) != [("ok",)]:
            raise RuntimeError("Message snapshot integrity_check failed")
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if re.fullmatch(r"Msg_[0-9a-f]{32}", r[0])]
        if not tables:
            raise RuntimeError("No supported message tables")
        required = {"local_id", "local_type", "create_time", "real_sender_id",
                    "message_content", "WCDB_CT_message_content"}
        total = texts = 0
        ranked = []
        for table in tables:
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info([{table}])")}
            if not required <= columns:
                raise RuntimeError("Unsupported message table schema")
            count, text_count, last = conn.execute(
                f"SELECT COUNT(*), SUM(CASE WHEN (local_type & 4294967295)=1 THEN 1 ELSE 0 END), "
                f"MAX(create_time) FROM [{table}]").fetchone()
            total += count
            texts += text_count or 0
            if text_count:
                ranked.append((last or 0, table))
        attempted = decoded = compressed = failed = 0
        for _, table in sorted(ranked, reverse=True):
            if attempted >= 20:
                break
            rows = conn.execute(
                f"SELECT message_content, WCDB_CT_message_content FROM [{table}] "
                "WHERE (local_type & 4294967295)=1 ORDER BY create_time DESC LIMIT ?",
                (min(5, 20 - attempted),)).fetchall()
            for body, encoding in rows:
                attempted += 1
                try:
                    if isinstance(body, bytes):
                        if encoding == 4:
                            compressed += 1
                            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(body)) as reader:
                                body = reader.read(MAX_TEXT_BYTES + 1)
                        if len(body) > MAX_TEXT_BYTES:
                            raise ValueError("Text sample size limit")
                        text = body.decode("utf-8", errors="strict")
                    elif isinstance(body, str):
                        text = body
                    else:
                        raise ValueError("Unsupported text storage")
                    if not text:
                        raise ValueError("Empty text sample")
                    decoded += 1
                    del text
                except Exception:
                    failed += 1
        return {"integrity_check": "ok", "message_tables_in_shard": len(tables),
                "message_rows_in_shard": total, "text_rows_in_shard": texts,
                "required_message_fields": sorted(required),
                "text_samples_attempted": attempted, "text_samples_decoded": decoded,
                "zstd_text_samples": compressed, "text_sample_failures": failed,
                "sample_content_output": False}
    finally:
        conn.close()


def main():
    report = {"generated_utc": datetime.now(timezone.utc).isoformat(), "status": "running",
              "scope": "one most-recent active-account message shard and its WAL",
              "source_access": "read-only", "key_persisted": False,
              "plaintext_database_persisted": False, "message_bodies_printed": False,
              "network_requests": 0, "full_history_coverage_verified": False,
              "media_retrieval_verified": False, "capture_consistency_verified": False}
    key = None
    stage = "active_account_binding"
    try:
        report["tested_weixin_version"] = installed_version()
        contact, pids, _ = discover_active()
        candidates = [p for p in (contact.parent.parent / "message").glob("message_*.db")
                      if re.fullmatch(r"message_\d+\.db", p.name)]
        if not candidates:
            raise RuntimeError("No active-account message shards")
        def activity(p):
            wal_sig = signature(Path(str(p) + "-wal"))
            return max(p.stat().st_mtime_ns, wal_sig[1] if wal_sig else 0)
        target = max(candidates, key=activity)
        if target.stat().st_size > 128 * 1024 * 1024:
            raise RuntimeError("Latest shard exceeds bounded probe size")
        report.update(message_shards_detected=len(candidates), tested_shard=target.name)
        emit("message_shard_selected", tested_shard=target.name, message_shards=len(candidates))
        stage = "snapshot"
        source, wal = snapshot(target)
        report.update(database_bytes=len(source), wal_bytes=len(wal))
        stage = "message_key_validation"
        key, stats = scan_config_key(pids, source[:PAGE])
        report.update(message_key_hmac_validated=True, key_scan_statistics=stats)
        stage = "wal_validation"
        merged, wal_stats = apply_committed_wal(source, wal)
        report.update(wal_validation=wal_stats, wal_applied=wal_stats["applied_pages"] > 0)
        stage = "page_decryption"
        plain, verified, zeros = decrypt_database(merged, key)
        report.update(authenticated_pages=verified, zero_unallocated_pages=zeros,
                      output_snapshot_page_hmac_verified=True)
        wal_stats["page_hmac_verified"] = report["wal_applied"]
        stage = "message_query_and_text_decode"
        report.update(inspect_messages(plain))
        report["status"] = ("passed_message_shard_text_read" if report["text_samples_decoded"] > 0
                            and report["text_sample_failures"] == 0 else "partial_message_read")
    except Exception as exc:
        report.update(status="failed", failed_stage=stage, error=str(exc))
    finally:
        if key is not None:
            key[:] = bytes(len(key))
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    emit("message_verification_result", **report)
    return 0 if report["status"].startswith("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
