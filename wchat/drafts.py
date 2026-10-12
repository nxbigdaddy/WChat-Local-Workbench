"""Local remark drafts; this module never modifies WeChat data.

Every stored entry means "待在微信修改" until a subsequent contact snapshot
confirms the requested remark. Only explicitly edited drafts are persisted.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Iterator


MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DRAFTS = 10_000
MAX_REMARK_CHARS = 4096
MAX_IDENTIFIER_CHARS = 512
_PURPOSE = "待在微信修改的本地备注草稿"
_FIELDS = {"old_remark", "new_remark", "status", "updated_at", "verified_at"}


class DraftStoreError(RuntimeError):
    """A draft operation failed; actual WeChat remarks remain untouched."""


class CorruptDraftStoreError(DraftStoreError):
    """Existing draft data is invalid and must not be overwritten."""


class DraftStoreBusyError(DraftStoreError):
    """Another operation holds the draft file lock; the caller may retry."""


class DraftStoreLimitError(DraftStoreError):
    """The draft file or proposed edit exceeds the supported limits."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("账号及联系人标识必须是非空文本。")
    if len(value) > MAX_IDENTIFIER_CHARS:
        raise DraftStoreLimitError("账号或联系人标识过长。")


def _remark(value: str) -> None:
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("备注必须是文本，且不能包含空字符。")
    if len(value) > MAX_REMARK_CHARS:
        raise DraftStoreLimitError("备注草稿过长。")


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        return datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class DraftStore:
    """A bounded JSON store with nonblocking OS locks and atomic replacement.

    Contacts passed to ``reconcile`` must contain ``username`` and ``remark``.
    Missing or malformed contacts cannot verify or discard a saved draft.
    ``list_for`` and ``get`` return independent dictionaries.
    """

    def __init__(self, path: Path | None = None):
        if path is None:
            local = os.environ.get("LOCALAPPDATA")
            base = Path(local) if local else Path.home() / "AppData" / "Local"
            path = base / "WChatDesk" / "remark-drafts.json"
        self.path = Path(path)
        # Keep this file: deleting it would permit a lock-inode race on POSIX.
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise DraftStoreError("无法打开本地备注草稿锁文件。") from exc
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise DraftStoreBusyError("本地备注草稿正被其他操作使用，请重试。") from exc
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise DraftStoreBusyError("本地备注草稿正被其他操作使用，请重试。") from exc
            acquired = True
            yield
        finally:
            try:
                if acquired:
                    if os.name == "nt":
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _load(self) -> dict:
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return {"version": 1, "purpose": _PURPOSE, "accounts": {}}
        except OSError as exc:
            raise DraftStoreError("无法读取本地备注草稿文件。") from exc
        if len(raw) > MAX_FILE_BYTES:
            raise DraftStoreLimitError("本地备注草稿文件超出大小限制，未作修改。")
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            self._validate(data)
        except DraftStoreLimitError:
            raise
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise CorruptDraftStoreError(
                "本地备注草稿文件损坏或格式不受支持；已保留原文件，请先修复或另存。"
            ) from exc
        return data

    @staticmethod
    def _validate(data: object) -> None:
        if not isinstance(data, dict) or set(data) != {"version", "purpose", "accounts"}:
            raise ValueError("invalid document")
        if type(data["version"]) is not int or data["version"] != 1 or data["purpose"] != _PURPOSE:
            raise ValueError("unsupported document")
        accounts = data["accounts"]
        if not isinstance(accounts, dict):
            raise ValueError("invalid accounts")
        count = 0
        for account, drafts in accounts.items():
            _identifier(account)
            if not isinstance(drafts, dict) or not drafts:
                raise ValueError("invalid account drafts")
            count += len(drafts)
            if count > MAX_DRAFTS:
                raise DraftStoreLimitError("本地备注草稿数量超出限制，未作修改。")
            for username, draft in drafts.items():
                _identifier(username)
                if not isinstance(draft, dict) or set(draft) != _FIELDS:
                    raise ValueError("invalid draft")
                _remark(draft["old_remark"])
                _remark(draft["new_remark"])
                if draft["status"] not in ("pending", "verified"):
                    raise ValueError("invalid status")
                if not _valid_timestamp(draft["updated_at"]):
                    raise ValueError("invalid timestamp")
                if draft["status"] == "pending" and draft["verified_at"] is not None:
                    raise ValueError("pending draft has verification timestamp")
                if draft["status"] == "verified" and not _valid_timestamp(draft["verified_at"]):
                    raise ValueError("verified draft has no verification timestamp")

    def _save(self, data: dict) -> None:
        self._validate(data)
        payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        if len(payload) > MAX_FILE_BYTES:
            raise DraftStoreLimitError("本地备注草稿将超出文件大小限制，未保存本次修改。")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=self.path.name + ".", suffix=".tmp",
                dir=self.path.parent, delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise DraftStoreError("无法原子保存本地备注草稿，原文件保持不变。") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def get(self, account: str, username: str) -> dict | None:
        _identifier(account)
        _identifier(username)
        with self._locked():
            draft = self._load()["accounts"].get(account, {}).get(username)
            return deepcopy(draft)

    def set(self, account: str, username: str, old_remark: str, new_remark: str) -> dict:
        _identifier(account)
        _identifier(username)
        _remark(old_remark)
        _remark(new_remark)
        with self._locked():
            data = self._load()
            draft = {
                "old_remark": old_remark, "new_remark": new_remark,
                "status": "pending", "updated_at": _now(), "verified_at": None,
            }
            data["accounts"].setdefault(account, {})[username] = draft
            self._save(data)
            return deepcopy(draft)

    def remove(self, account: str, username: str) -> None:
        _identifier(account)
        _identifier(username)
        with self._locked():
            data = self._load()
            drafts = data["accounts"].get(account, {})
            if username not in drafts:
                return
            del drafts[username]
            if not drafts:
                del data["accounts"][account]
            self._save(data)

    def list_for(self, account: str) -> dict[str, dict]:
        _identifier(account)
        with self._locked():
            return deepcopy(self._load()["accounts"].get(account, {}))

    def reconcile(self, account: str, contacts: list[dict]) -> list[str]:
        """Verify exact matches and return only usernames newly verified.

        A previously verified draft becomes pending if an observed remark changes.
        Conflicting duplicate observations are ignored, as are contacts not present
        in this snapshot. No contact details are copied into the draft file.
        """
        _identifier(account)
        if not isinstance(contacts, list):
            raise TypeError("contacts must be a list")
        with self._locked():
            data = self._load()
            drafts = data["accounts"].get(account, {})
            if not drafts:
                return []
            observed: dict[str, str] = {}
            conflicts: set[str] = set()
            for contact in contacts:
                if not isinstance(contact, dict):
                    continue
                username, remark = contact.get("username"), contact.get("remark")
                if not isinstance(username, str) or username not in drafts or not isinstance(remark, str):
                    continue
                if username in observed and observed[username] != remark:
                    conflicts.add(username)
                observed[username] = remark
            verified = []
            changed = False
            now = _now()
            for username, remark in observed.items():
                if username in conflicts:
                    continue
                draft = drafts[username]
                status = "verified" if remark == draft["new_remark"] else "pending"
                if status == draft["status"]:
                    continue
                draft["status"] = status
                draft["updated_at"] = now
                draft["verified_at"] = now if status == "verified" else None
                changed = True
                if status == "verified":
                    verified.append(username)
            if changed:
                self._save(data)
            return verified
