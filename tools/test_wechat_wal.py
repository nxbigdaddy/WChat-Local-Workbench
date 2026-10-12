"""Entirely synthetic WAL tests; never opens a database or private data."""

import struct
import unittest

from wechat_wal import WalValidationError, apply_committed_wal, wal_checksum


PAGE = 4096
FRAME = PAGE + 24


def reference_checksum(data, state=(0, 0), big=False):
    """Deliberately separate fixture encoder using byte slicing."""
    s0, s1 = state
    order = "big" if big else "little"
    for at in range(0, len(data), 8):
        first = int.from_bytes(data[at : at + 4], order)
        second = int.from_bytes(data[at + 4 : at + 8], order)
        s0 = (s0 + first + s1) % (2**32)
        s1 = (s1 + second + s0) % (2**32)
    return s0, s1


def make_wal(frames, *, big=False):
    salts = struct.pack(">II", 0x10203040, 0x50607080)
    prefix = struct.pack(">4I", 0x377F0683 if big else 0x377F0682, 3007000, PAGE, 7)
    prefix += salts
    state = reference_checksum(prefix, big=big)
    result = prefix + struct.pack(">II", *state)
    for pgno, dbsize, payload in frames:
        assert len(payload) == PAGE
        start = struct.pack(">II", pgno, dbsize)
        state = reference_checksum(start + payload, state, big=big)
        result += start + salts + struct.pack(">II", *state) + payload
    return result


class WalTests(unittest.TestCase):
    def setUp(self):
        self.a = b"A" * PAGE
        self.b = b"B" * PAGE
        self.c = b"C" * PAGE
        self.d = b"D" * PAGE
        self.base = self.a + self.b

    def test_checksum_known_words_both_orders_and_rollover(self):
        for big, order in ((False, "little"), (True, "big")):
            encoded = b"".join(n.to_bytes(4, order) for n in (1, 2, 3, 4))
            self.assertEqual(wal_checksum(encoded, big_endian=big), (7, 14))
        self.assertEqual(wal_checksum(b"\xff" * 8), (0xFFFFFFFF, 0xFFFFFFFE))

    def test_committed_pages_and_uncommitted_tail_both_orders(self):
        for big in (False, True):
            with self.subTest(big=big):
                wal = make_wal([(1, 0, self.c), (2, 2, self.d), (1, 0, self.a)], big=big)
                result, stats = apply_committed_wal(self.base, wal)
                self.assertEqual(result, self.c + self.d)
                self.assertEqual(stats["last_commit_frame"], 2)
                self.assertEqual(stats["uncommitted_tail_frames"], 1)
                self.assertEqual(stats["applied_pages"], 2)

    def test_latest_committed_instance_of_page_wins(self):
        wal = make_wal([(1, 2, self.c), (1, 2, self.d)])
        result, stats = apply_committed_wal(self.base, wal)
        self.assertEqual(result, self.d + self.b)
        self.assertEqual(stats["commits"], 2)

    def test_bad_checksum_stops_before_later_commit(self):
        wal = bytearray(make_wal([(1, 2, self.c), (2, 0, self.d), (1, 2, self.a)]))
        wal[32 + FRAME + 24] ^= 1
        result, stats = apply_committed_wal(self.base, bytes(wal))
        self.assertEqual(result, self.c + self.b)
        self.assertEqual(stats["stop_reason"], "frame_checksum_mismatch")
        self.assertEqual(stats["valid_frames"], 1)

    def test_salt_mismatch_stops_without_skipping(self):
        wal = bytearray(make_wal([(1, 2, self.c), (2, 2, self.d), (1, 2, self.a)]))
        wal[32 + FRAME + 8] ^= 1
        result, stats = apply_committed_wal(self.base, bytes(wal))
        self.assertEqual(result, self.c + self.b)
        self.assertEqual(stats["stop_reason"], "frame_salt_mismatch")

    def test_truncated_commit_does_not_commit_prior_pending_frames(self):
        wal = make_wal([(1, 2, self.c), (2, 0, self.d), (1, 2, self.a)])[:-1]
        result, stats = apply_committed_wal(self.base, wal)
        self.assertEqual(result, self.c + self.b)
        self.assertEqual(stats["stop_reason"], "incomplete_frame")
        self.assertEqual(stats["uncommitted_tail_frames"], 1)

    def test_no_commit_never_applies_frames(self):
        result, stats = apply_committed_wal(self.base, make_wal([(1, 0, self.c)]))
        self.assertEqual(result, self.base)
        self.assertEqual(stats["last_commit_frame"], 0)

    def test_commit_truncates_database(self):
        result, stats = apply_committed_wal(self.base, make_wal([(1, 1, self.c)]))
        self.assertEqual(result, self.c)
        self.assertEqual(stats["output_pages"], 1)

    def test_commit_extends_database_using_present_frames(self):
        result, _ = apply_committed_wal(self.base, make_wal([(3, 3, self.c)]))
        self.assertEqual(result, self.base + self.c)

    def test_missing_extension_page_is_rejected(self):
        with self.assertRaisesRegex(WalValidationError, "missing_extended"):
            apply_committed_wal(self.base, make_wal([(4, 4, self.c)]))

    def test_incomplete_headers_are_rejected(self):
        header = make_wal([])
        for length in (1, 16, 31):
            with self.subTest(length=length):
                with self.assertRaisesRegex(WalValidationError, "incomplete_wal_header"):
                    apply_committed_wal(self.base, header[:length])

    def test_invalid_headers_fail_closed(self):
        for offset, expected in ((0, "magic"), (4, "version"), (8, "page_size"), (24, "checksum")):
            with self.subTest(offset=offset):
                wal = bytearray(make_wal([]))
                wal[offset] ^= 1
                with self.assertRaisesRegex(WalValidationError, expected):
                    apply_committed_wal(self.base, bytes(wal))

    def test_empty_and_header_only_wals_preserve_base(self):
        for wal in (b"", make_wal([])):
            result, stats = apply_committed_wal(self.base, wal)
            self.assertEqual(result, self.base)
            self.assertEqual(stats["applied_pages"], 0)

    def test_zero_page_number_ends_prefix(self):
        result, stats = apply_committed_wal(self.base, make_wal([(0, 2, self.c)]))
        self.assertEqual(result, self.base)
        self.assertEqual(stats["stop_reason"], "invalid_page_number")

    def test_size_limit_prevents_allocation(self):
        with self.assertRaisesRegex(WalValidationError, "committed_size_exceeds"):
            apply_committed_wal(self.base, make_wal([(1, 0x7FFFFFFF, self.c)]))

    def test_inputs_are_not_mutated_and_stats_contain_no_payloads(self):
        wal = make_wal([(1, 2, self.c)])
        original = wal
        _, stats = apply_committed_wal(self.base, wal)
        self.assertEqual(wal, original)
        self.assertEqual(self.base, self.a + self.b)
        self.assertFalse(stats["capture_consistency_verified"])
        self.assertFalse(stats["page_hmac_verified"])
        self.assertTrue(all(isinstance(value, (int, bool, str)) for value in stats.values()))


if __name__ == "__main__":
    unittest.main()
