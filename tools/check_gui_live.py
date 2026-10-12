"""Verify native GUI contact rendering with the active account; aggregate output only."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from wchat.gui import MainWindow

parser = argparse.ArgumentParser()
parser.add_argument("--keep-open", action="store_true")
args = parser.parse_args()
app = QApplication([])
app.setFont(QFont("Microsoft YaHei UI", 10))
window = MainWindow()
window.show()
errors = []
sys.excepthook = lambda kind, value, trace: errors.append(kind.__name__)
result = {"status": "running"}


def completed(method, data):
    if method != "start":
        return
    window.operation_finished.disconnect(completed)
    count = window.model.rowCount()
    result.update(status="passed" if count > 0 and not errors and "error" not in data else "failed",
                  contact_rows=count, visible_rows=window.proxy.rowCount(),
                  gui_thread_idle=window.thread is None, account_bound=bool(window.account),
                  uncaught_error_types=errors, private_values_output=False, screenshot_saved=False)
    (ROOT/"reports"/"gui-live-ui.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if not args.keep_open:
        window.close()


window.operation_finished.connect(completed)
QTimer.singleShot(100, window.connect_reader)
app.exec()
raise SystemExit(0 if result["status"] == "passed" else 1)
