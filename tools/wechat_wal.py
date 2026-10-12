"""Reconstruct an encrypted SQLCipher 4 snapshot from supplied byte strings.

No file, process, network, decryption, or key access occurs here. The caller
must capture a matching database/WAL pair and validate SQLCipher page HMACs.
WAL checksums cover encrypted on-disk page bytes, including reserved bytes.

References:
https://sqlite.org/fileformat2.html#walformat
https://github.com/sqlcipher/sqlcipher/blob/v4.6.1/src/wal.c
"""

from __future__ import annotations

import struct


class WalValidationError(ValueError):
    """Invalid inputs; the message contains a fixed reason, never input data."""


def wal_checksum(
    data: bytes,
    state: tuple[int, int] = (0, 0),
    *,
    big_endian: bool = False,
) -> tuple[int, int]:
    """Extend SQLite's two-word rolling checksum over a multiple of 8 bytes."""
    if len(data) % 8:
        raise WalValidationError("checksum_input_not_multiple_of_8")
    s0, s1 = state
    pair = ">II" if big_endian else "<II"
    for x0, x1 in struct.iter_unpack(pair, data):
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def apply_committed_wal(
    base: bytes,
    wal: bytes,
    *,
    page_size: int = 4096,
    max_output_bytes: int = 256 * 1024 * 1024,
) -> tuple[bytes, dict[str, int | bool | str]]:
    """Overlay the last valid committed WAL prefix on encrypted ``base``.

    The first invalid or incomplete frame ends the usable prefix. Valid
    uncommitted tail frames are never applied. Invalid WAL headers raise;
    an empty WAL is accepted. This does not establish capture consistency,
    authenticity, decryption compatibility, or freshness.
    """
    if not isinstance(base, bytes) or not isinstance(wal, bytes):
        raise WalValidationError("inputs_must_be_bytes")
    if page_size != 4096:
        raise WalValidationError("unsupported_sqlcipher4_page_size")
    if not isinstance(max_output_bytes, int) or max_output_bytes < page_size:
        raise WalValidationError("invalid_output_limit")
    if not base or len(base) % page_size:
        raise WalValidationError("invalid_base_length")
    if len(base) > max_output_bytes:
        raise WalValidationError("base_exceeds_output_limit")

    frame_size = 24 + page_size
    stats: dict[str, int | bool | str] = {
        "wal_present": bool(wal),
        "header_valid": False,
        "checksum_basis": "encrypted_page_bytes",
        "valid_frames": 0,
        "commits": 0,
        "last_commit_frame": 0,
        "uncommitted_tail_frames": 0,
        "applied_pages": 0,
        "base_pages": len(base) // page_size,
        "output_pages": len(base) // page_size,
        "stop_reason": "empty_wal",
        "capture_consistency_verified": False,
        "page_hmac_verified": False,
    }
    if not wal:
        return base, stats
    if len(wal) < 32:
        raise WalValidationError("incomplete_wal_header")
    magic, version, size, _sequence, _salt1, _salt2, ck0, ck1 = (
        struct.unpack(">8I", wal[:32])
    )
    if magic not in (0x377F0682, 0x377F0683):
        raise WalValidationError("invalid_wal_magic")
    if version != 3007000:
        raise WalValidationError("unsupported_wal_version")
    if size != page_size:
        raise WalValidationError("wal_page_size_mismatch")
    big_endian = magic == 0x377F0683
    state = wal_checksum(wal[:24], big_endian=big_endian)
    if state != (ck0, ck1):
        raise WalValidationError("wal_header_checksum_mismatch")
    stats["header_valid"] = True
    stats["stop_reason"] = "end_of_wal"
    salts = wal[16:24]
    last_commit = 0
    final_pages = len(base) // page_size
    frame_count = 0
    offset = 32

    while offset < len(wal):
        if len(wal) - offset < frame_size:
            stats["stop_reason"] = "incomplete_frame"
            break
        header = wal[offset : offset + 24]
        page = wal[offset + 24 : offset + frame_size]
        pgno, dbsize = struct.unpack(">II", header[:8])
        if header[8:16] != salts:
            stats["stop_reason"] = "frame_salt_mismatch"
            break
        if not 0 < pgno <= 0xFFFFFFFE:
            stats["stop_reason"] = "invalid_page_number"
            break
        next_state = wal_checksum(header[:8], state, big_endian=big_endian)
        next_state = wal_checksum(page, next_state, big_endian=big_endian)
        if next_state != struct.unpack(">II", header[16:24]):
            stats["stop_reason"] = "frame_checksum_mismatch"
            break
        # These are allocation constraints, not claims about SQLite validity.
        if dbsize and dbsize * page_size > max_output_bytes:
            raise WalValidationError("committed_size_exceeds_output_limit")
        if dbsize > 0xFFFFFFFE:
            raise WalValidationError("invalid_committed_database_size")
        state = next_state
        frame_count += 1
        if dbsize:
            last_commit = frame_count
            final_pages = dbsize
            stats["commits"] = int(stats["commits"]) + 1
        offset += frame_size

    stats["valid_frames"] = frame_count
    stats["last_commit_frame"] = last_commit
    stats["uncommitted_tail_frames"] = frame_count - last_commit
    if not last_commit:
        return base, stats

    # Store offsets rather than page copies. Only committed frames participate.
    latest: dict[int, int] = {}
    for index in range(last_commit):
        frame_offset = 32 + index * frame_size
        pgno = struct.unpack_from(">I", wal, frame_offset)[0]
        if pgno <= final_pages:
            latest[pgno] = frame_offset + 24

    base_pages = len(base) // page_size
    if any(pgno not in latest for pgno in range(base_pages + 1, final_pages + 1)):
        raise WalValidationError("missing_extended_database_page")
    output_size = final_pages * page_size
    result = bytearray(base[:output_size])
    if output_size > len(result):
        result.extend(b"\x00" * (output_size - len(result)))
    for pgno, page_offset in latest.items():
        destination = (pgno - 1) * page_size
        result[destination : destination + page_size] = wal[
            page_offset : page_offset + page_size
        ]
    stats["applied_pages"] = len(latest)
    stats["output_pages"] = final_pages
    return bytes(result), stats
