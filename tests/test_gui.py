"""Synthetic interaction tests using the native Qt event loop."""

import tempfile
import sys
import time
import unittest
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from wchat.demo import DemoReader
from wchat.drafts import DraftStore
from wchat.gui import MainWindow, RemarkDialog


APP = QApplication.instance() or QApplication([])


def wait_until(predicate, timeout=5):
    until = time.monotonic() + timeout
    while not predicate():
        APP.processEvents()
        if time.monotonic() > until:
            raise AssertionError("Qt operation timed out")
        time.sleep(.005)
    APP.processEvents()


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.errors = []
        self.old_hook = sys.excepthook
        sys.excepthook = lambda kind, value, trace: self.errors.append(kind.__name__)
        self.temp = tempfile.TemporaryDirectory()
        self.store = DraftStore(Path(self.temp.name)/"drafts.json")
        self.window = MainWindow(demo=True, reader=DemoReader(), store=self.store)
        self.window.show()
        self.window.connect_reader()
        wait_until(lambda: self.window.thread is None)

    def tearDown(self):
        self.window.close()
        APP.processEvents()
        self.temp.cleanup()
        sys.excepthook = self.old_hook
        self.assertEqual(self.errors, [])

    def test_filter_select_and_read(self):
        w = self.window
        self.assertEqual(w.proxy.rowCount(), 4)
        w.contact_search.setText("研发")
        self.assertEqual(w.proxy.rowCount(), 1)
        w.table.selectRow(0)
        self.assertEqual(w.current_contact["username"], "demo_lin")
        w.load_messages()
        wait_until(lambda: w.thread is None)
        self.assertEqual(len(w.messages_data), 5)
        w.message_search.setText("备注")
        w.load_messages()
        wait_until(lambda: w.thread is None)
        self.assertEqual(len(w.messages_data), 2)

    def test_draft_is_separate_from_actual_remark_and_verifies_on_refresh(self):
        w = self.window
        c = w.model.rows[0]
        original = c["remark"]
        dialog = RemarkDialog(c, self.store, w.account, w)
        dialog.edit.setText("新备注（虚构）")
        dialog.save()
        self.assertEqual(c["remark"], original)
        self.assertEqual(self.store.get(w.account, c["username"])["status"], "pending")
        self.assertEqual(QApplication.clipboard().text(), "新备注（虚构）")
        w.refresh_drafts()
        w.category.setCurrentIndex(3)
        self.assertEqual(w.proxy.rowCount(), 1)
        contacts = [dict(c, remark="新备注（虚构）")]
        w._apply_result("start", {"account": "demo", "contacts": contacts, "version": "demo"})
        self.assertEqual(self.store.get("demo", c["username"])["status"], "verified")
        dialog.close()

    def test_untrusted_message_html_and_media_are_not_loaded(self):
        w = self.window
        payload = '<img src="https://example.invalid/private">'
        w.messages_data = [{"create_time": 0, "local_type": 1, "text": payload, "sender_name": "<sender>"},
                           {"create_time": 0, "local_type": 3, "type_name": "图片", "text": "PRIVATE_MEDIA_XML"}]
        w.render_messages()
        self.assertIn(payload, w.browser.toPlainText())
        self.assertIn("【图片】", w.browser.toPlainText())
        self.assertNotIn("PRIVATE_MEDIA_XML", w.browser.toPlainText())
        self.assertNotIn('<img src="https:', w.browser.toHtml())
        self.assertFalse(w.browser.openExternalLinks())

    def test_clearing_filter_with_selected_contact_does_not_recurse(self):
        w = self.window
        w.contact_search.setText("陈晓")
        w.table.selectRow(0)
        w.contact_search.clear()
        APP.processEvents()
        self.assertEqual(w.current_contact["username"], "demo_chen")
        self.assertEqual(w.proxy.rowCount(), 4)
        self.assertEqual(self.errors, [])

    def test_corrupt_drafts_do_not_block_contacts(self):
        self.store.path.write_text("broken", encoding="utf-8")
        w = self.window
        w.connect_reader()
        wait_until(lambda: w.thread is None)
        self.assertEqual(w.model.rowCount(), 4)
        w.table.selectRow(0)
        self.assertFalse(w.remark_button.isEnabled())
        self.assertTrue(w.search_button.isEnabled())
        self.assertIn("暂不可用", w.remark_status.text())

    def test_failed_new_query_does_not_commit_keyword_or_mix_pagination(self):
        class FailingMessages(DemoReader):
            def messages(self, **kwargs):
                raise RuntimeError("读取失败（合成测试）")
        w = self.window
        w.table.selectRow(0)
        w.last_keyword = "旧查询"
        w.cursor = {"time": 10, "shard": "demo", "local_id": 1, "rowid": 1}
        w.messages_data = [{"text": "旧结果"}]
        w.reader = FailingMessages()
        w.message_search.setText("新查询")
        w.load_messages()
        wait_until(lambda: w.thread is None)
        self.assertEqual(w.last_keyword, "旧查询")
        w.load_messages(append=True)
        self.assertFalse(w._append)
        wait_until(lambda: w.thread is None)

    def test_failed_reconnect_clears_previous_account_selection(self):
        class FailingStart(DemoReader):
            def start(self, **kwargs):
                raise RuntimeError("新账号读取失败（合成测试）")
        w = self.window
        w.table.selectRow(0)
        w.reader = FailingStart()
        w.connect_reader()
        wait_until(lambda: w.thread is None)
        self.assertEqual(w.model.rowCount(), 0)
        self.assertIsNone(w.current_contact)
        self.assertEqual(w.account, "")
        self.assertFalse(w.search_button.isEnabled())

    def test_failed_reconcile_never_reuses_old_verified_claim(self):
        w = self.window
        c = w.model.rows[0]
        self.store.set("demo", c["username"], "old", "new")
        self.store.reconcile("demo", [{"username": c["username"], "remark": "new"}])
        def fail(*args):
            raise RuntimeError("保存失败（合成测试）")
        self.store.reconcile = fail
        w._apply_result("start", {"account": "demo", "contacts": [dict(c, remark="changed")], "version": "demo"})
        w.table.selectRow(0)
        self.assertIn("核验未完成", w.remark_status.text())
        self.assertNotIn("核对一致", w.remark_status.text())

    def test_cancellation_during_close_waits_for_worker(self):
        import threading
        class WaitingReader(DemoReader):
            def __init__(self):
                self.stopped = threading.Event()
                self.closed = False
            def reset_cancel(self):
                self.stopped.clear()
            def start(self, progress=None):
                self.stopped.wait(2)
                raise RuntimeError("读取已取消")
            def cancel(self):
                self.stopped.set()
            def close(self):
                self.closed = True
        w = self.window
        reader = WaitingReader()
        w.reader = reader
        w.connect_reader()
        w.close()
        wait_until(lambda: w.thread is None)
        self.assertTrue(reader.closed)
        self.assertFalse(w.isVisible())

    def test_partial_has_no_repeating_pagination(self):
        w = self.window
        w.table.selectRow(0)
        result = {"messages": [], "has_more": True, "next_before": None, "coverage": {"partial": True}}
        w._apply_result("messages", result)
        self.assertIn("不完整", w.progress_label.text())
        before = w.thread
        w.load_messages(append=True)
        self.assertIs(w.thread, before)


if __name__ == "__main__":
    unittest.main()
