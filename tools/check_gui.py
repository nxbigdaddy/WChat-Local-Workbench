"""Show and exercise the real Qt GUI with synthetic data; save only its preview."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from wchat.gui import MainWindow

app = QApplication([])
app.setFont(QFont("Microsoft YaHei UI", 10))
window = MainWindow(demo=True)
window.show()
result = {"mode": "synthetic", "native_qt": True, "steps": []}
errors = []
sys.excepthook = lambda kind, value, trace: errors.append(kind.__name__)


def completed(method, data):
    if "error" in data:
        result.update(status="failed", error=data["error"])
        finish()
    elif method == "start":
        result["contacts"] = window.model.rowCount()
        result["steps"].append("contacts_loaded")
        window.contact_search.setText("陈晓")
        window.table.selectRow(0)
        window.contact_search.clear()
        QTimer.singleShot(100, window.load_messages)
    else:
        result["messages"] = len(window.messages_data)
        result["steps"].append("messages_rendered")
        QTimer.singleShot(300, finish)


def finish():
    target = ROOT / "reports" / "gui-demo.png"
    target.parent.mkdir(exist_ok=True)
    result["screenshot_saved"] = window.grab().save(str(target))
    result["uncaught_errors"] = errors
    result.setdefault("status", "passed" if result.get("contacts") == 4 and result.get("messages") == 5 and not errors else "failed")
    (ROOT/"reports"/"gui-smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    window.close()
    app.quit()


window.operation_finished.connect(completed)
QTimer.singleShot(100, window.connect_reader)
QTimer.singleShot(15000, app.quit)
app.exec()
raise SystemExit(0 if result.get("status") == "passed" else 1)
