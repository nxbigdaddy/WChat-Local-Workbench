"""Synthetic in-memory databases only; no Weixin process or user files."""

from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

import zstandard

from wchat.reader import ReaderCancelled, ReaderError, WeChatReader, decode_content


USER = "synthetic_friend"
TABLE = "Msg_" + hashlib.md5(USER.encode()).hexdigest()


def synthetic_db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
    conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES(7,'synthetic_sender')")
    conn.execute(f"CREATE TABLE [{TABLE}](local_id INTEGER,local_type INTEGER,create_time INTEGER,real_sender_id INTEGER,message_content,WCDB_CT_message_content INTEGER)")
    conn.executemany(f"INSERT INTO [{TABLE}] VALUES(?,?,?,?,?,?)", rows)
    return conn


class SyntheticReader(WeChatReader):
    def __init__(self, shards, **kwargs):
        super().__init__(**kwargs)
        self._db = Path("synthetic/contact/contact.db")
        self._storage = Path("synthetic")
        self._ready = True
        self.shards = shards
        self._names = {"synthetic_sender": "合成发送者"}

    def _bind(self):
        self._check()

    def _message_paths(self):
        return [Path(name) for name in self.shards]

    @contextmanager
    def _connection(self, path, progress=None):
        conn = synthetic_db(self.shards[path.name])
        try:
            yield conn, {"applied_pages": 0}
        finally:
            conn.close()


def row(local_id, timestamp, text, encoding=0):
    return (local_id, 1, timestamp, 7, text, encoding)


class ReaderTests(unittest.TestCase):
    def test_plain_and_zstd_strict_decode(self):
        encoded = "合成聊天正文".encode()
        compressed = zstandard.ZstdCompressor().compress(encoded)
        self.assertEqual(decode_content(compressed, 4), ("合成聊天正文", None))
        self.assertEqual(decode_content(encoded, 0), ("合成聊天正文", None))
        self.assertIsNotNone(decode_content(b"\xff", 0)[1])
        self.assertIsNotNone(decode_content(b"broken", 4)[1])

    def test_decode_limits_and_unknown_codec(self):
        compressed = zstandard.ZstdCompressor().compress(b"x" * 10000)
        self.assertIsNotNone(decode_content(compressed, 4, max_bytes=2048)[1])
        self.assertIsNotNone(decode_content(b"abc", 99)[1])

    def test_cross_shard_global_order_and_same_second_pagination(self):
        reader = SyntheticReader({"message_0.db": [row(1, 100, "a"), row(2, 100, "b"), row(3, 99, "c")],
                                  "message_1.db": [row(1, 100, "d"), row(2, 98, "e")]})
        first = reader.messages(USER, limit=2)
        second = reader.messages(USER, limit=2, before=first["next_before"])
        third = reader.messages(USER, limit=2, before=second["next_before"])
        self.assertEqual([m["text"] for m in first["messages"]], ["b", "d"])
        self.assertEqual([m["text"] for m in second["messages"]], ["c", "a"])
        self.assertEqual([m["text"] for m in third["messages"]], ["e"])
        self.assertFalse(third["has_more"])
        self.assertEqual(first["messages"][0]["sender_name"], "合成发送者")

    def test_search_decodes_compressed_text_before_matching(self):
        compressed = zstandard.ZstdCompressor().compress("old NEEDLE message".encode())
        reader = SyntheticReader({"message_0.db": [row(1, 100, "new message"), row(2, 10, compressed, 4)]})
        result = reader.messages(USER, keyword="needle", limit=1)
        self.assertEqual([m["local_id"] for m in result["messages"]], [2])
        self.assertEqual(result["scanned"], 2)
        self.assertTrue(result["coverage"]["search_complete"])

    def test_search_partial_budget_has_no_misleading_cursor(self):
        reader = SyntheticReader({"message_0.db": [row(i, i, "find") for i in range(10)]}, max_scan_rows=3)
        result = reader.messages(USER, keyword="find", limit=1)
        self.assertTrue(result["coverage"]["partial"])
        self.assertTrue(result["coverage"]["budget_exhausted"])
        self.assertIsNone(result["next_before"])

    def test_cancellation_before_and_during_query(self):
        event = threading.Event()
        reader = SyntheticReader({"message_0.db": [row(1, 1, "synthetic")]}, cancel_event=event)
        reader.cancel()
        with self.assertRaises(ReaderCancelled):
            reader.messages(USER)
        reader.reset_cancel()
        def progress(_message):
            reader.cancel()
        with self.assertRaises(ReaderCancelled):
            reader.messages(USER, progress=progress)

    def test_decode_failure_retained_without_search_and_reported(self):
        reader = SyntheticReader({"message_0.db": [row(1, 1, b"invalid zstd", 4)]})
        result = reader.messages(USER)
        self.assertEqual(result["messages"][0]["text"], "")
        self.assertTrue(result["messages"][0]["decode_error"])
        self.assertEqual(result["coverage"]["decode_failures"], 1)

    def test_group_sender_prefix_and_explicit_identity(self):
        reader = SyntheticReader({})
        message = reader._build_message((1, 1, 1, 100, 7, "synthetic_sender:\nhello", 0),
                                        "synthetic@chatroom", "message_0.db", {7: "synthetic_sender"})
        self.assertEqual(message["text"], "hello")
        self.assertEqual(message["sender_name"], "合成发送者")

    def test_memory_cache_lru_bound_and_signature_invalidation(self):
        reader = SyntheticReader({}, max_cache_bytes=8)
        reader._cache_image(Path("a"), (1, 2), b"aaaa", {"ok": True})
        reader._cache_image(Path("b"), (1, 2), b"bbbb", {"ok": True})
        self.assertEqual(reader._cached_image(Path("a"), (1, 2))[0], b"aaaa")
        reader._cache_image(Path("c"), (1, 2), b"cccc", {})
        self.assertIsNone(reader._cached_image(Path("b"), (1, 2)))
        self.assertEqual(reader._cache_bytes, 8)
        self.assertIsNone(reader._cached_image(Path("a"), (1, 3)))
        self.assertEqual(reader._cache_bytes, 4)
        reader.close()
        self.assertEqual(reader._cache_bytes, 0)

    def test_contact_fields_and_account_hint_without_self_guess(self):
        class ContactsReader(SyntheticReader):
            def _bind(self):
                self._db = Path("synthetic/contact/contact.db")
                self._storage = Path("synthetic")
                self._account = {"account_hint": "synthetic_account_suffix", "username": None}

            @contextmanager
            def _connection(self, path, progress=None):
                conn = sqlite3.connect(":memory:")
                try:
                    conn.execute("CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,alias TEXT,description TEXT,local_type INTEGER,delete_flag INTEGER)")
                    conn.execute("INSERT INTO contact VALUES('fake','昵称','备注','别名','描述',1,0)")
                    yield conn, {}
                finally:
                    conn.close()

        with patch("wchat.reader._probe.installed_version", return_value="synthetic"):
            result = ContactsReader({}).start()
        self.assertEqual(result["contacts"][0]["display_name"], "备注")
        self.assertEqual(result["contacts"][0]["alias"], "别名")
        self.assertIsNone(result["account"]["username"])
        self.assertIsNone(result["coverage"]["friend_count"])

    def test_failed_start_cannot_read_half_initialized_account(self):
        class FailedStartReader(SyntheticReader):
            def _bind(self):
                self._db = Path("new_account/contact/contact.db")
                self._storage = Path("new_account")

            @contextmanager
            def _connection(self, path, progress=None):
                raise ReaderError("合成通讯录读取失败")
                yield

        reader = FailedStartReader({})
        with patch("wchat.reader._probe.installed_version", return_value="synthetic"):
            with self.assertRaisesRegex(ReaderError, "合成通讯录读取失败"):
                reader.start()
        self.assertFalse(reader._ready)
        with self.assertRaisesRegex(ReaderError, "先成功读取通讯录"):
            reader.messages(USER)

    def test_cancelled_start_revokes_ready_even_before_binding(self):
        reader = SyntheticReader({})
        reader.cancel()
        with self.assertRaises(ReaderCancelled):
            reader.start()
        reader.reset_cancel()
        self.assertFalse(reader._ready)
        with self.assertRaisesRegex(ReaderError, "先成功读取通讯录"):
            reader.messages(USER)

    def test_account_change_revokes_ready_and_requires_successful_start(self):
        class AccountReader(SyntheticReader):
            _bind = WeChatReader._bind

            @contextmanager
            def _connection(self, path, progress=None):
                conn = sqlite3.connect(":memory:")
                try:
                    conn.execute("CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT)")
                    conn.execute("INSERT INTO contact VALUES('fake_new','新账号联系人','')")
                    yield conn, {}
                finally:
                    conn.close()

        reader = AccountReader({})
        new_db = Path("new_account/db_storage/contact/contact.db")
        reader._keys.append(bytearray(b"k" * 32))
        reader._cache_image(Path("synthetic_cache"), (1, 2), b"abcd", {})
        with patch("wchat.reader._probe.discover_active", return_value=(new_db, [123], 2)):
            with self.assertRaisesRegex(ReaderError, "账号已经切换"):
                reader.messages(USER)
            self.assertFalse(reader._ready)
            self.assertEqual(reader._keys, [])
            self.assertEqual(reader._cache_bytes, 0)
            self.assertEqual(reader._names, {})
            with self.assertRaisesRegex(ReaderError, "先成功读取通讯录"):
                reader.messages(USER)
            with patch("wchat.reader._probe.installed_version", return_value="synthetic"):
                reader.start()
            self.assertTrue(reader._ready)
            self.assertEqual(reader._db, new_db)
            self.assertEqual(reader.messages("fake_new")["messages"], [])
        reader.close()
        self.assertFalse(reader._ready)


if __name__ == "__main__":
    unittest.main()
