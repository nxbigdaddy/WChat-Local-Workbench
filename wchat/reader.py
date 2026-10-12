"""Local, read-only Weixin data access for a GUI worker thread.

Keys and decrypted SQLite images exist only in this process. No transcript,
key file, decrypted cache, report, network request, or Weixin write is made.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections import OrderedDict
import hashlib
import heapq
import io
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import types

import zstandard

from tools import wechat_wal

# The existing standalone probe uses this absolute import. Both names refer
# to the same audited local module; its command-line main is never executed.
sys.modules.setdefault("wechat_wal", wechat_wal)
from tools import verify_wechat_contacts as _probe


class ReaderError(RuntimeError):
    """A user-facing error without keys, message bodies, or memory contents."""


class ReaderCancelled(ReaderError):
    pass


class ReaderLimitError(ReaderError):
    pass


TYPE_NAMES = {1: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频",
              47: "表情", 48: "位置", 49: "链接/文件", 50: "通话",
              10000: "系统", 10002: "撤回"}
CONTACT_FIELDS = ("username", "nick_name", "remark", "alias", "description",
                  "local_type", "delete_flag")
MESSAGE_FIELDS = {"local_id", "local_type", "create_time", "real_sender_id",
                  "message_content", "WCDB_CT_message_content"}
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_RESULT_TEXT_BYTES = 16 * 1024 * 1024


def decode_content(body, encoding, max_bytes=MAX_TEXT_BYTES):
    """Decode a WCDB text value, strictly and with a decompression limit."""
    if body is None:
        return "", None
    if isinstance(body, str):
        if len(body) > max_bytes or len(body.encode("utf-8")) > max_bytes:
            return "", "正文超过大小限制"
        return body, None
    if not isinstance(body, bytes):
        return "", "未支持的正文存储类型"
    if len(body) > max_bytes:
        return "", "正文超过大小限制"
    try:
        if encoding == 4:
            # Limit both decoded output and the zstd history window.
            with zstandard.ZstdDecompressor(max_window_size=max_bytes // 1024).stream_reader(
                io.BytesIO(body)
            ) as stream:
                body = stream.read(max_bytes + 1)
            if len(body) > max_bytes:
                return "", "解压正文超过大小限制"
        elif encoding not in (None, 0, 1):
            return "", "未支持的正文压缩标记"
        return body.decode("utf-8", errors="strict"), None
    except (UnicodeError, zstandard.ZstdError, ValueError, OSError):
        return "", "正文解码失败或需要未提供的压缩字典"


def _sort_key(message):
    return (message["create_time"], message["source_shard"],
            message["local_id"], message["rowid"])


def _cursor(message):
    return {"time": message["create_time"], "shard": message["source_shard"],
            "local_id": message["local_id"], "rowid": message["rowid"]}


class WeChatReader:
    """Synchronous API; run one operation at a time in a GUI worker.

    Call reset_cancel() before a new operation after cancel(). A database up
    to 320 MiB may temporarily require approximately 1-2 GiB of memory while
    validating and deserializing. Only one decrypted shard is open at a time.
    """

    def __init__(self, cancel_event=None, max_database_bytes=320 * 1024 * 1024,
                 max_wal_bytes=64 * 1024 * 1024, max_scan_rows=100000,
                 max_scan_seconds=180, max_contacts=100000,
                 max_cache_bytes=384 * 1024 * 1024):
        self.cancel_event = cancel_event or threading.Event()
        self.max_database_bytes = int(max_database_bytes)
        self.max_wal_bytes = int(max_wal_bytes)
        self.max_scan_rows = int(max_scan_rows)
        self.max_scan_seconds = float(max_scan_seconds)
        self.max_contacts = int(max_contacts)
        self.max_cache_bytes = max(0, int(max_cache_bytes))
        if min(self.max_database_bytes, self.max_wal_bytes, self.max_scan_rows,
               self.max_scan_seconds, self.max_contacts) <= 0:
            raise ValueError("读取上限必须为正数")
        self._lock = threading.Lock()
        self._db = None
        self._storage = None
        self._pids = []
        self._keys = []
        self._names = {}
        self._account = {}
        self._ready = False
        self._cache = OrderedDict()
        self._cache_bytes = 0

    def cancel(self):
        self.cancel_event.set()

    def reset_cancel(self):
        self.cancel_event.clear()

    def close(self):
        self.cancel()
        with self._lock:
            self._ready = False
            self._clear_keys()
            self._clear_cache()
            self._names.clear()
            self._db = self._storage = None
            self._pids = []

    def _clear_keys(self):
        for key in self._keys:
            key[:] = bytes(len(key))
        self._keys.clear()

    def _clear_cache(self):
        self._cache.clear()
        self._cache_bytes = 0

    def _cached_image(self, path, signature):
        item = self._cache.get(str(path))
        if item is None:
            return None
        if item[0] != signature:
            self._cache_bytes -= len(item[1])
            del self._cache[str(path)]
            return None
        self._cache.move_to_end(str(path))
        return item[1], dict(item[2])

    def _cache_image(self, path, signature, image, coverage):
        old = self._cache.pop(str(path), None)
        if old is not None:
            self._cache_bytes -= len(old[1])
        if len(image) > self.max_cache_bytes:
            return
        while self._cache and self._cache_bytes + len(image) > self.max_cache_bytes:
            _, old = self._cache.popitem(last=False)
            self._cache_bytes -= len(old[1])
        self._cache[str(path)] = (signature, image, dict(coverage))
        self._cache_bytes += len(image)

    def _check(self):
        if self.cancel_event.is_set():
            raise ReaderCancelled("读取已取消")

    def _progress(self, callback, message):
        self._check()
        if callback is not None:
            callback(message)

    @contextmanager
    def _operation(self, initializing=False):
        if not self._lock.acquire(blocking=False):
            raise ReaderError("已有读取任务正在运行，请先取消或等待完成")
        try:
            if initializing:
                self._ready = False
            self._check()
            yield
        finally:
            self._lock.release()

    def _bind(self):
        self._check()
        try:
            db, pids, candidates = _probe.discover_active()
        except Exception:
            raise ReaderError("无法唯一识别已登录的微信账号，请确认微信正在运行且仅登录一个账号") from None
        if self._db is not None and self._db != db:
            self._ready = False
            self._clear_keys()
            self._clear_cache()
            self._names.clear()
            raise ReaderError("微信账号已经切换，请重新读取通讯录")
        if self._pids and self._pids != pids:
            self._clear_keys()
            self._clear_cache()
        self._db, self._storage, self._pids = db, db.parent.parent, pids
        self._account = {"account_hint": db.parent.parent.parent.name,
                         "username": None, "active_accounts_matched": 1,
                         "account_directories_detected": candidates}

    def _bounded_read(self, path, limit):
        self._check()
        if path.stat().st_size > limit:
            raise ReaderError("数据库或 WAL 超过配置的内存读取上限")
        with path.open("rb") as source:
            data = source.read(limit + 1)
        if len(data) > limit:
            raise ReaderError("数据库读取期间增长并超过内存上限")
        return data

    def _file_digest(self, path, limit):
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as source:
            while True:
                self._check()
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ReaderError("数据库读取期间增长并超过内存上限")
                digest.update(chunk)
        return digest.digest()

    def _snapshot(self, path):
        wal_path = Path(str(path) + "-wal")
        for _ in range(3):
            self._check()
            before = (_probe.signature(path), _probe.signature(wal_path))
            data = self._bounded_read(path, self.max_database_bytes)
            wal = self._bounded_read(wal_path, self.max_wal_bytes) if wal_path.exists() else b""
            second = self._file_digest(path, self.max_database_bytes)
            second_wal = self._file_digest(wal_path, self.max_wal_bytes) if wal_path.exists() else hashlib.sha256(b"").digest()
            after = (_probe.signature(path), _probe.signature(wal_path))
            if before == after and hashlib.sha256(data).digest() == second and hashlib.sha256(wal).digest() == second_wal:
                return data, wal
            if self.cancel_event.wait(0.1):
                self._check()
        raise ReaderError("数据库正在变化，无法取得稳定副本，请稍后重试")

    def _key(self, page, progress):
        if len(page) != _probe.PAGE:
            raise ReaderError("数据库首页不完整")
        for key in self._keys:
            self._check()
            if _probe.verify_page(page, 1, _probe.derive_mac_key(key, page[:16])):
                return key
        self._progress(progress, "正在从微信进程只读验证数据库密钥…")
        kernel = _probe.kernel_api()
        owner = self

        class CancellableKernel:
            def __getattr__(self, name):
                function = getattr(kernel, name)
                if name == "CloseHandle":
                    return function

                def call(*args):
                    owner._check()
                    return function(*args)
                return call

        # Clone only the function globals: do not mutate the probe module or
        # redirect process-wide stdout while other GUI workers are running.
        namespace = dict(_probe.scan_config_key.__globals__)
        namespace["kernel_api"] = lambda: CancellableKernel()
        namespace["emit"] = lambda *_args, **_kwargs: owner._progress(progress, "正在验证数据库访问…")
        scanner = types.FunctionType(_probe.scan_config_key.__code__, namespace,
                                     argdefs=_probe.scan_config_key.__defaults__)
        try:
            key, _ = scanner(self._pids, page, seconds=55)
        except ReaderCancelled:
            raise
        except Exception:
            raise ReaderError("未能取得此数据库的有效密钥，当前微信版本或运行状态可能不兼容") from None
        if len(self._keys) >= 128:
            old = self._keys.pop(0)
            old[:] = bytes(len(old))
        self._keys.append(key)
        return key

    def _decrypt(self, data, key):
        namespace = dict(_probe.decrypt_database.__globals__)
        def verify(*args):
            self._check()
            return _probe.verify_page(*args)
        namespace["verify_page"] = verify
        decrypt = types.FunctionType(_probe.decrypt_database.__code__, namespace)
        try:
            return decrypt(data, key)
        except ReaderCancelled:
            raise
        except Exception:
            raise ReaderError("数据库解密或页完整性校验失败，已停止读取") from None

    @contextmanager
    def _connection(self, path, progress=None):
        self._check()
        resolved = path.resolve()
        if self._storage is None or not resolved.is_relative_to(self._storage.resolve()):
            raise ReaderError("数据库不属于当前绑定账号")
        wal_path = Path(str(resolved) + "-wal")
        signature = (_probe.signature(resolved), _probe.signature(wal_path))
        cached = self._cached_image(resolved, signature)
        if cached is not None:
            image, coverage = cached
            coverage["memory_cache_hit"] = True
        else:
            source, wal = self._snapshot(resolved)
            key = self._key(source[:_probe.PAGE], progress)
            try:
                merged, coverage = wechat_wal.apply_committed_wal(
                    source, wal, max_output_bytes=self.max_database_bytes)
            except wechat_wal.WalValidationError:
                raise ReaderError("WAL 格式或提交边界校验失败，未使用不可靠副本") from None
            del source, wal
            plain, verified, zeros = self._decrypt(merged, key)
            del merged
            plain[18:20] = b"\x01\x01"
            image = bytes(plain)
            plain.clear()
            coverage.update(authenticated_pages=verified, zero_unallocated_pages=zeros,
                            output_snapshot_page_hmac_verified=True,
                            page_hmac_verified=coverage["applied_pages"] > 0,
                            full_history_coverage_verified=False, memory_cache_hit=False,
                            note="本机数据库副本；未持有微信读取事务锁，不保证与手机或云端历史完整一致")
        conn = sqlite3.connect(":memory:")
        try:
            conn.deserialize(image)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.set_progress_handler(lambda: int(self.cancel_event.is_set()), 1000)
            if not cached and list(conn.execute("PRAGMA integrity_check")) != [("ok",)]:
                raise ReaderError("SQLite 数据完整性检查失败")
            if not cached and signature == (_probe.signature(resolved), _probe.signature(wal_path)):
                self._cache_image(resolved, signature, image, coverage)
            yield conn, coverage
        except sqlite3.Error:
            self._check()
            raise ReaderError("数据库结构不兼容或 SQLite 查询失败") from None
        finally:
            conn.close()

    def start(self, progress=None):
        with self._operation(initializing=True):
            self._progress(progress, "正在识别当前微信账号…")
            # start() is the explicit way to bind a different account.
            self._db = None
            self._names.clear()
            self._clear_keys()
            self._clear_cache()
            self._bind()
            try:
                version = _probe.installed_version()
            except Exception:
                raise ReaderError("无法读取当前微信版本") from None
            self._progress(progress, "正在读取并验证通讯录…")
            with self._connection(self._db, progress) as (conn, coverage):
                columns = {r[1] for r in conn.execute("PRAGMA table_info(contact)")}
                if not {"username", "nick_name", "remark"} <= columns:
                    raise ReaderError("通讯录缺少必要字段，当前格式尚未适配")
                expressions = [f'"{field}"' if field in columns else f'NULL AS "{field}"' for field in CONTACT_FIELDS]
                contacts = []
                content_bytes = 0
                rows = conn.execute("SELECT " + ",".join(expressions) + " FROM contact LIMIT ?", (self.max_contacts + 1,))
                for row in rows:
                    self._check()
                    if len(contacts) >= self.max_contacts:
                        raise ReaderError("联系人实体数量超过上限，请提高读取配置后重试")
                    contact = dict(zip(CONTACT_FIELDS, row))
                    for field in CONTACT_FIELDS[:5]:
                        value = contact[field]
                        if value is None:
                            value = ""
                        if not isinstance(value, str):
                            raise ReaderError("通讯录字段编码不兼容")
                        content_bytes += len(value.encode("utf-8"))
                        contact[field] = value
                    if content_bytes > 64 * 1024 * 1024:
                        raise ReaderError("通讯录文本超过内存上限")
                    contact["display_name"] = contact["remark"] or contact["nick_name"] or contact["username"]
                    contacts.append(contact)
            contacts.sort(key=lambda row: (row["display_name"].casefold(), row["username"]))
            self._names = {row["username"]: row["display_name"] for row in contacts}
            coverage.update(contact_entity_count=len(contacts), friend_count=None)
            self._progress(progress, f"通讯录读取完成：{len(contacts)} 个本地联系人实体")
            self._ready = True
            return {"version": version, "account": dict(self._account),
                    "contacts": contacts, "coverage": coverage}

    def _message_paths(self):
        return sorted((p for p in (self._storage / "message").glob("message_*.db")
                       if re.fullmatch(r"message_\d+\.db", p.name)), key=lambda p: p.name)

    @staticmethod
    def _before_clause(before, shard):
        if before is None:
            return "", []
        if isinstance(before, (int, float)):
            return " WHERE create_time < ?", [before]
        if not isinstance(before, dict) or set(before) != {"time", "shard", "local_id", "rowid"}:
            raise ReaderError("消息翻页游标无效")
        if not isinstance(before["shard"], str) or not all(isinstance(before[f], int) for f in ("time", "local_id", "rowid")):
            raise ReaderError("消息翻页游标无效")
        if shard == before["shard"]:
            return " WHERE (create_time, local_id, rowid) < (?, ?, ?)", [before["time"], before["local_id"], before["rowid"]]
        return (" WHERE create_time <= ?" if shard < before["shard"] else " WHERE create_time < ?"), [before["time"]]

    def _build_message(self, row, username, shard, senders):
        rowid, local_id, local_type, created, sender_id, body, encoding = row
        if not all(isinstance(value, int) for value in (rowid, local_id, local_type, created)):
            raise ReaderError("消息的编号或时间字段类型不兼容")
        text, error = decode_content(body, encoding)
        sender = senders.get(sender_id, "")
        if "@chatroom" in username and ":\n" in text:
            prefix, rest = text.split(":\n", 1)
            if prefix and len(prefix) <= 256 and "\n" not in prefix and not any(ch.isspace() for ch in prefix):
                # Prefer the explicit Name2Id mapping over body prefixes.
                if not sender:
                    sender = prefix
                if prefix == sender:
                    text = rest
        base_type = int(local_type) & 0xFFFFFFFF
        return {"rowid": int(rowid), "local_id": int(local_id), "local_type": int(local_type),
                "type_name": TYPE_NAMES.get(base_type, f"类型 {base_type}"),
                "create_time": int(created), "sender_username": sender,
                "sender_name": self._names.get(sender, sender) or "未知发送者",
                "text": text, "decode_error": error, "source_shard": shard,
                "is_group": "@chatroom" in username, "media_loaded": False}

    def messages(self, username, limit=100, keyword="", before=None, progress=None):
        if not isinstance(username, str) or not username or len(username) > 1024:
            raise ReaderError("联系人标识无效")
        if not isinstance(keyword, str) or len(keyword) > 4096:
            raise ReaderError("搜索内容过长或无效")
        limit = max(1, min(int(limit), 500))
        table = "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()
        with self._operation():
            if not self._ready or self._db is None:
                raise ReaderError("请先成功读取通讯录，再读取聊天记录")
            self._bind()
            paths = self._message_paths()
            selected = []
            total = scanned = matched = decode_failures = 0
            selected_bytes = 0
            completed, failures, shard_reports = [], [], []
            started = time.monotonic()
            needle = keyword.casefold()
            budget_exhausted = False
            for index, path in enumerate(paths):
                self._check()
                if scanned >= self.max_scan_rows or time.monotonic() - started > self.max_scan_seconds:
                    budget_exhausted = True
                    break
                self._progress(progress, f"正在读取聊天分库 {index + 1}/{len(paths)}…")
                try:
                    with self._connection(path, progress) as (conn, shard_coverage):
                        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                            completed.append(path.name)
                            shard_reports.append({"shard": path.name, "has_conversation": False, **shard_coverage})
                            continue
                        columns = {r[1] for r in conn.execute(f"PRAGMA table_info([{table}])")}
                        if not MESSAGE_FIELDS <= columns:
                            raise ReaderError("消息表缺少必要字段")
                        total += conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
                        senders = {}
                        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='Name2Id'").fetchone():
                            for sender_id, name in conn.execute("SELECT rowid, user_name FROM Name2Id LIMIT 100001"):
                                if len(senders) >= 100000:
                                    raise ReaderError("发送者映射超过内存上限")
                                if isinstance(name, str):
                                    senders[sender_id] = name
                        where, params = self._before_clause(before, path.name)
                        sql = (f"SELECT rowid,local_id,local_type,create_time,real_sender_id,message_content,WCDB_CT_message_content "
                               f"FROM [{table}]{where} ORDER BY create_time DESC,local_id DESC,rowid DESC")
                        if not keyword:
                            sql += " LIMIT ?"
                            params.append(limit + 1)
                        query = conn.execute(sql, params)
                        finished_shard = True
                        for row in query:
                            self._check()
                            if scanned >= self.max_scan_rows or time.monotonic() - started > self.max_scan_seconds:
                                budget_exhausted = True
                                finished_shard = False
                                break
                            scanned += 1
                            message = self._build_message(row, username, path.name, senders)
                            if message["decode_error"]:
                                decode_failures += 1
                            if keyword and (message["decode_error"] or needle not in message["text"].casefold()):
                                continue
                            matched += 1
                            item = (_sort_key(message), message)
                            size = len(message["text"].encode("utf-8"))
                            if len(selected) < limit + 1:
                                heapq.heappush(selected, item)
                                selected_bytes += size
                            elif item[0] > selected[0][0]:
                                previous = heapq.heapreplace(selected, item)[1]
                                selected_bytes += size - len(previous["text"].encode("utf-8"))
                            if selected_bytes > MAX_RESULT_TEXT_BYTES:
                                raise ReaderLimitError("本页正文超过内存上限，请减少每页数量")
                        if finished_shard:
                            completed.append(path.name)
                        shard_reports.append({"shard": path.name, "has_conversation": True, **shard_coverage})
                except ReaderCancelled:
                    raise
                except ReaderLimitError:
                    raise
                except ReaderError as exc:
                    failures.append({"shard": path.name, "reason": str(exc)})
                except (OSError, sqlite3.Error):
                    self._check()
                    failures.append({"shard": path.name, "reason": "分库读取失败，请确认微信仍在运行"})
                if budget_exhausted:
                    break
            partial = len(completed) != len(paths)
            ordered = [entry[1] for entry in sorted(selected, key=lambda item: item[0], reverse=True)]
            has_more = len(ordered) > limit or partial
            page = ordered[:limit]
            next_before = _cursor(page[-1]) if page and len(ordered) > limit and not partial else None
            page.reverse()
            return {"messages": page, "total": total, "scanned": scanned,
                    "matched_in_scan": matched, "has_more": has_more, "next_before": next_before,
                    "coverage": {"shards_detected": len(paths), "shards_read": len(completed),
                                 "partial": partial, "failures": failures, "shards": shard_reports,
                                 "budget_exhausted": budget_exhausted, "decode_failures": decode_failures,
                                 "search_complete": bool(keyword) and not partial and decode_failures == 0,
                                 "full_history_coverage_verified": False,
                                 "capture_consistency_verified": False,
                                 "total_note": "已读分库中该会话的消息总行数；不等同关键词命中数或手机全部历史",
                                 "note": "仅本机可验证分库；未读取附件。不完整扫描结果不提供翻页游标，以免跳过消息。"}}
