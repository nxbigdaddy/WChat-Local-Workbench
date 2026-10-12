"""Run with python app.py; --demo opens synthetic data only."""

import argparse
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from wchat.gui import MainWindow


def main():
    parser = argparse.ArgumentParser(description="微信本地通讯录与聊天工作台")
    parser.add_argument("--demo", action="store_true", help="使用完全虚构的数据")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("微信本地工作台")
    app.setOrganizationName("WChatDesk")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    window = MainWindow(demo=args.demo)
    window.show()
    if args.demo:
        QTimer.singleShot(150, window.connect_reader)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
