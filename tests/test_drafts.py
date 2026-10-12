"""Regression checks use temporary files and synthetic identities only."""

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from wchat.drafts import (
    CorruptDraftStoreError,
    DraftStore,
    DraftStoreError,
    DraftStoreLimitError,
    MAX_FILE_BYTES,
    MAX_REMARK_CHARS,
)


class DraftStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "drafts.json"
        self.store = DraftStore(self.path)

    def test_default_path_uses_local_app_data(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": self.directory.name}):
            self.assertEqual(
                DraftStore().path,
                Path(self.directory.name) / "WChatDesk" / "remark-drafts.json",
            )

    def test_reads_do_not_create_draft_data(self):
        self.assertIsNone(self.store.get("account-a", "person-a"))
        self.assertEqual(self.store.list_for("account-a"), {})
        self.assertEqual(self.store.reconcile("account-a", [{"username": "person-a", "remark": "示例"}]), [])
        self.assertFalse(self.path.exists())

    def test_round_trip_isolates_accounts_and_returned_objects(self):
        result = self.store.set("account-a", "person-a", "旧示例", "新示例")
        self.assertEqual(result["status"], "pending")
        self.assertIsNone(result["verified_at"])
        result["new_remark"] = "mutated"
        other = DraftStore(self.path)
        other.set("account-b", "person-a", "", "另一账号")
        self.store.set("account-a", "person-b", "", "")
        self.assertEqual(other.get("account-a", "person-a")["new_remark"], "新示例")
        listing = other.list_for("account-a")
        listing["person-a"]["status"] = "verified"
        self.assertEqual(other.get("account-a", "person-a")["status"], "pending")
        self.assertEqual(len(other.list_for("account-a")), 2)
        self.assertEqual(other.get("account-b", "person-a")["new_remark"], "另一账号")
        self.assertIn("待在微信修改", self.path.read_text(encoding="utf-8"))

    def test_only_matching_existing_drafts_are_verified(self):
        self.store.set("account-a", "person-a", "旧示例", "新示例")
        self.store.set("account-a", "person-b", "旧示例", "")
        before = self.path.read_bytes()
        self.assertEqual(self.store.reconcile("account-a", [
            {"username": "person-a", "remark": "新示例 ", "phone": "not-saved"},
            {"username": "person-b"},
            {"username": "unrelated", "remark": "新示例"},
        ]), [])
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.store.reconcile("account-a", [
            {"username": "person-a", "remark": "新示例", "phone": "not-saved"},
            {"username": "person-b", "remark": ""},
        ]), ["person-a", "person-b"])
        draft = self.store.get("account-a", "person-a")
        self.assertEqual(draft["old_remark"], "旧示例")
        self.assertEqual(draft["new_remark"], "新示例")
        self.assertEqual(draft["status"], "verified")
        self.assertIsNotNone(draft["verified_at"])
        before = self.path.read_bytes()
        self.assertEqual(self.store.reconcile("account-a", [{"username": "person-a", "remark": "新示例"}]), [])
        self.assertEqual(before, self.path.read_bytes())
        self.assertNotIn(b"not-saved", before)
        self.assertNotIn(b"unrelated", before)

    def test_verified_draft_returns_to_pending_on_observed_change(self):
        self.store.set("account-a", "person-a", "old", "new")
        self.store.reconcile("account-a", [{"username": "person-a", "remark": "new"}])
        self.store.reconcile("account-a", [])
        self.assertEqual(self.store.get("account-a", "person-a")["status"], "verified")
        self.store.reconcile("account-a", [{"username": "person-a", "remark": "changed"}])
        draft = self.store.get("account-a", "person-a")
        self.assertEqual(draft["status"], "pending")
        self.assertIsNone(draft["verified_at"])
        self.assertEqual(draft["old_remark"], "old")

    def test_conflicting_duplicate_observations_do_not_verify(self):
        self.store.set("account-a", "person-a", "old", "new")
        self.assertEqual(self.store.reconcile("account-a", [
            {"username": "person-a", "remark": "new"},
            {"username": "person-a", "remark": "other"},
            {"username": "person-a", "remark": "new"},
        ]), [])
        self.assertEqual(self.store.get("account-a", "person-a")["status"], "pending")

    def test_editing_verified_draft_requires_new_verification(self):
        self.store.set("account-a", "person-a", "old", "new")
        self.store.reconcile("account-a", [{"username": "person-a", "remark": "new"}])
        result = self.store.set("account-a", "person-a", "new", "next")
        self.assertEqual(result["status"], "pending")
        self.assertIsNone(result["verified_at"])

    def test_remove_preserves_other_accounts(self):
        self.store.set("account-a", "person-a", "", "a")
        self.store.set("account-b", "person-a", "", "b")
        self.store.remove("account-a", "person-a")
        self.store.remove("account-a", "missing")
        self.assertEqual(self.store.list_for("account-a"), {})
        self.assertEqual(self.store.get("account-b", "person-a")["new_remark"], "b")
        self.assertNotIn("account-a", json.loads(self.path.read_text(encoding="utf-8"))["accounts"])

    def test_corruption_is_never_silently_overwritten(self):
        for damaged in (b"", b"{broken", b"[]", b"\xff", b'{"version":1,"version":1}'):
            with self.subTest(case=damaged[:12]):
                self.path.write_bytes(damaged)
                for operation in (
                    lambda: self.store.get("a", "b"),
                    lambda: self.store.set("a", "b", "", "new"),
                    lambda: self.store.remove("a", "b"),
                    lambda: self.store.reconcile("a", []),
                ):
                    with self.assertRaises(CorruptDraftStoreError):
                        operation()
                    self.assertEqual(self.path.read_bytes(), damaged)

    def test_structurally_corrupt_data_is_preserved(self):
        self.store.set("a", "b", "old", "new")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["accounts"]["a"]["b"]["status"] = "verified"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(CorruptDraftStoreError):
            self.store.set("a", "b", "", "replacement")
        self.assertEqual(self.path.read_bytes(), before)

    def test_size_and_text_limits_preserve_previous_file(self):
        self.store.set("a", "b", "old", "new")
        before = self.path.read_bytes()
        with self.assertRaises(DraftStoreLimitError):
            self.store.set("a", "b", "", "x" * (MAX_REMARK_CHARS + 1))
        with patch("wchat.drafts.MAX_FILE_BYTES", len(before) + 20):
            with self.assertRaises(DraftStoreLimitError):
                self.store.set("a", "c", "", "another draft")
        with patch("wchat.drafts.MAX_DRAFTS", 1):
            with self.assertRaises(DraftStoreLimitError):
                self.store.set("a", "c", "", "another draft")
        self.assertEqual(self.path.read_bytes(), before)
        oversized = b" " * (MAX_FILE_BYTES + 1)
        self.path.write_bytes(oversized)
        with self.assertRaises(DraftStoreLimitError):
            self.store.set("a", "b", "", "new")
        self.assertEqual(self.path.stat().st_size, len(oversized))

    def test_failed_atomic_replace_leaves_original_file_and_no_temporary(self):
        self.store.set("a", "b", "old", "new")
        before = self.path.read_bytes()
        with patch("wchat.drafts.os.replace", side_effect=PermissionError("synthetic failure")):
            with self.assertRaises(DraftStoreError):
                self.store.set("a", "b", "old", "other")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.store.set("a", "b", "old", "retry")
        self.assertEqual(self.store.get("a", "b")["new_remark"], "retry")

    def test_other_process_cannot_write_while_lock_is_held(self):
        self.store.set("a", "b", "old", "new")
        before = self.path.read_bytes()
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from wchat.drafts import DraftStore, DraftStoreBusyError\n"
            "try:\n"
            "    DraftStore(Path(sys.argv[1])).set('a', 'b', 'old', 'conflict')\n"
            "except DraftStoreBusyError:\n"
            "    sys.exit(0)\n"
            "sys.exit(2)\n"
        )
        with self.store._locked():
            result = subprocess.run(
                [sys.executable, "-c", script, str(self.path)],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True, timeout=10, check=False,
            )
            self.assertEqual(result.returncode, 0, "child must reject the locked draft store")
        self.assertEqual(self.path.read_bytes(), before)
        self.store.set("a", "b", "old", "after-lock")
        self.assertEqual(self.store.get("a", "b")["new_remark"], "after-lock")


if __name__ == "__main__":
    unittest.main()
