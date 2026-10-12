from __future__ import annotations

from datetime import datetime
import html
from pathlib import Path
import tempfile

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QSortFilterProxyModel, Qt, QThread, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QSplitter, QTableView, QTextBrowser,
    QVBoxLayout, QWidget)


STYLE = """
QMainWindow, QDialog { background: #f3f6f5; color: #182c27; }
QWidget { font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QFrame#card { background: white; border: 1px solid #dfe7e3; border-radius: 10px; }
QLabel#title { font-size: 24px; font-weight: 650; color: #163e30; }
QLabel#section { font-size: 19px; font-weight: 650; }
QLabel#muted { color: #65776f; }
QLabel#badge { color: #16724f; background: #e4f2eb; border-radius: 12px; padding: 5px 12px; }
QPushButton { background: white; border: 1px solid #cbd8d1; border-radius: 6px; padding: 8px 14px; color: #254b3c; }
QPushButton:hover { background: #edf5f0; border-color: #92b9a4; }
QPushButton:disabled { background: #f2f4f3; color: #a0aaa5; border-color: #e1e6e3; }
QPushButton#primary { background: #187953; color: white; border-color: #187953; }
QPushButton#primary:hover { background: #126642; }
QPushButton#primary:disabled { background: #b7cfc2; border-color: #b7cfc2; }
QLineEdit, QComboBox { background: #fbfcfb; border: 1px solid #dbe3df; border-radius: 6px; padding: 9px; }
QLineEdit:focus { border: 1px solid #368c65; }
QTableView { background: white; border: none; gridline-color: #edf1ef; selection-background-color: #e6f2eb; selection-color: #174d33; }
QTableView::item { padding: 8px; border-bottom: 1px solid #edf1ef; }
QHeaderView::section { background: #f7f9f8; color: #65776f; border: none; padding: 9px; }
QTextBrowser { background: #f8faf9; border: 1px solid #e5ece8; border-radius: 8px; padding: 12px; }
QProgressBar { border: none; background: #e8efeb; border-radius: 2px; min-height: 4px; max-height: 4px; }
QProgressBar::chunk { background: #348c63; }
QSplitter::handle { background: transparent; width: 14px; }
QStatusBar { color: #607369; background: #f3f6f5; }
"""


def contact_kind(contact):
    name = contact.get("username", "")
    if name.endswith("@chatroom"):
        return "群聊"
    if name.startswith("gh_"):
        return "公众号"
    return "联系人缓存"


def display_name(contact):
    return contact.get("remark") or contact.get("nick_name") or contact.get("username") or "未命名"


def plain_label(text="", name=None):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if name:
        label.setObjectName(name)
    return label


class ContactModel(QAbstractTableModel):
    headers = ("联系人", "微信备注", "微信号 / 标识", "类别")

    def __init__(self):
        super().__init__()
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        c = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return (c.get("nick_name") or c["username"], c.get("remark") or "—",
                    c.get("alias") or c["username"], contact_kind(c))[index.column()]
        if role == Qt.ItemDataRole.UserRole:
            return c
        if role == Qt.ItemDataRole.ForegroundRole and c.get("delete_flag"):
            return QColor("#87918c")
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{display_name(c)}\n{c['username']}"

    def replace(self, rows):
        self.beginResetModel()
        self.rows = sorted(rows, key=lambda c: display_name(c).casefold())
        self.endResetModel()


class ContactFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.query = ""
        self.category = 0
        self.pending = set()

    def filterAcceptsRow(self, source_row, source_parent):
        c = self.sourceModel().rows[source_row]
        haystack = " ".join(str(c.get(k) or "") for k in ("username", "nick_name", "remark", "alias", "description")).casefold()
        if self.query not in haystack:
            return False
        return (self.category == 0 or
                self.category == 1 and bool(c.get("remark")) or
                self.category == 2 and c.get("username", "").endswith("@chatroom") or
                self.category == 3 and c.get("username") in self.pending)


class ReaderJob(QObject):
    progress = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, reader, method, kwargs):
        super().__init__()
        self.reader, self.method, self.kwargs = reader, method, kwargs

    @Slot()
    def run(self):
        try:
            result = getattr(self.reader, self.method)(progress=self.progress.emit, **self.kwargs)
            self.succeeded.emit(result)
        except Exception as error:
            # The reader reports redacted errors; never log message bodies or keys.
            self.failed.emit(str(error))
        finally:
            self.finished.emit()


class RemarkDialog(QDialog):
    """A transparent manual handoff; saving a draft is never reported as a WeChat write."""

    def __init__(self, contact, store, account, parent=None):
        super().__init__(parent)
        self.setWindowTitle("修改微信备注 · 辅助流程")
        self.setMinimumWidth(570)
        self.contact, self.store, self.account = contact, store, account
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.addWidget(plain_label("修改微信备注", "section"))
        layout.addWidget(plain_label("当前版本采用辅助修改：先编辑备注，再到微信保存，最后刷新核对。", "muted"))
        form = QFormLayout()
        form.addRow("联系人", plain_label(display_name(contact)))
        identifier = plain_label(contact.get("alias") or contact["username"])
        identifier.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("微信号 / 标识", identifier)
        form.addRow("当前微信备注", plain_label(contact.get("remark") or "（空）"))
        draft = store.get(account, contact["username"])
        self.edit = QLineEdit(draft["new_remark"] if draft and draft["status"] == "pending" else contact.get("remark") or "")
        self.edit.setMaxLength(100)
        self.edit.setPlaceholderText("输入新备注；留空表示准备清除备注")
        form.addRow("新备注", self.edit)
        layout.addLayout(form)
        self.help = plain_label("1. 保存草稿并复制新备注。\n2. 在微信中打开这位联系人的资料 → 备注和标签，核对身份后粘贴并保存。\n3. 回到本工具，点“刷新并核对备注”。", "muted")
        layout.addWidget(self.help)
        self.feedback = plain_label("草稿仅保存在本机；不会直接改动微信数据库。", "muted")
        layout.addWidget(self.feedback)
        buttons = QHBoxLayout()
        save = QPushButton("保存草稿并复制新备注")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        identity = QPushButton("复制联系人标识")
        identity.clicked.connect(lambda: QApplication.clipboard().setText(contact.get("alias") or contact["username"]))
        done = QPushButton("完成")
        done.clicked.connect(self.accept)
        buttons.addWidget(save)
        buttons.addWidget(identity)
        buttons.addWidget(done)
        layout.addLayout(buttons)

    @Slot()
    def save(self):
        try:
            text = self.edit.text()
            self.store.set(self.account, self.contact["username"], self.contact.get("remark") or "", text)
            QApplication.clipboard().setText(text)
            self.feedback.setText("草稿已保存，新备注已复制。请在微信中保存后返回刷新核对。" if text else
                                  "清空备注的草稿已保存。请在微信中清空备注并保存，然后返回刷新核对。")
        except Exception as error:
            self.feedback.setText(f"未保存：{error}")


class MainWindow(QMainWindow):
    operation_finished = Signal(str, object)

    def __init__(self, *, demo=False, reader=None, store=None):
        super().__init__()
        self.demo = demo
        if reader is None:
            if demo:
                from .demo import DemoReader
                reader = DemoReader()
            else:
                from .reader import WeChatReader
                reader = WeChatReader()
        from .drafts import DraftStore
        self._demo_dir = tempfile.TemporaryDirectory(prefix="wchat-demo-") if demo and store is None else None
        self.store = store or DraftStore(Path(self._demo_dir.name) / "drafts.json" if self._demo_dir else None)
        self.reader = reader
        self.account = ""
        self.drafts_error = None
        self.reconcile_error = None
        self.current_contact = None
        self.messages_data = []
        self.cursor = None
        self.has_more = False
        self.last_keyword = ""
        self.last_coverage = {}
        self.thread = None
        self.job = None
        self._closing = False
        self._job_method = ""
        self._job_result = None
        self._job_error = None
        self._cancel_requested = False
        self._append = False
        self.setWindowTitle("微信本地工作台" + (" · 演示" if demo else ""))
        self.resize(1280, 840)
        self.setMinimumSize(1020, 690)
        self.setStyleSheet(STYLE)
        self._build()

    def _build(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(26, 23, 26, 12)
        outer.setSpacing(18)
        top = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(plain_label("微信本地工作台", "title"))
        titles.addWidget(plain_label("浏览通讯录与聊天记录，让联系人信息更有条理。", "muted"))
        top.addLayout(titles, 1)
        self.badge = plain_label("演示模式" if self.demo else "尚未连接", "badge")
        self.badge.setFixedHeight(32)
        top.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self.connect_button = QPushButton("读取当前微信")
        self.connect_button.setObjectName("primary")
        self.connect_button.clicked.connect(self.connect_reader)
        top.addWidget(self.connect_button)
        outer.addLayout(top)
        split = QSplitter()
        split.setHandleWidth(14)
        left = QFrame()
        left.setObjectName("card")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(17, 17, 17, 12)
        header = QHBoxLayout()
        header.addWidget(plain_label("通讯录", "section"))
        header.addStretch()
        self.contact_count = plain_label("0 条", "muted")
        header.addWidget(self.contact_count)
        lv.addLayout(header)
        self.contact_search = QLineEdit()
        self.contact_search.setClearButtonEnabled(True)
        self.contact_search.setPlaceholderText("搜索昵称、备注、微信号")
        lv.addWidget(self.contact_search)
        self.category = QComboBox()
        self.category.addItems(["全部联系人缓存", "已有微信备注", "群聊", "备注待核对"])
        lv.addWidget(self.category)
        self.model = ContactModel()
        self.proxy = ContactFilter()
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(49)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 140)
        self.table.setColumnWidth(1, 170)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 100)
        self.table.selectionModel().selectionChanged.connect(self.select_contact)
        self.contact_search.textChanged.connect(self.filter_contacts)
        self.category.currentIndexChanged.connect(self.filter_contacts)
        lv.addWidget(self.table, 1)
        lv.addWidget(plain_label("包含群聊、公众号及历史缓存，条数不等于好友数。", "muted"))
        split.addWidget(left)
        right = QFrame()
        right.setObjectName("card")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(22, 20, 22, 16)
        rv.setSpacing(12)
        details = QHBoxLayout()
        labels = QVBoxLayout()
        self.contact_title = plain_label("选择一位联系人", "section")
        self.contact_subtitle = plain_label("从左侧选择联系人，浏览资料和聊天记录。", "muted")
        self.contact_subtitle.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        labels.addWidget(self.contact_title)
        labels.addWidget(self.contact_subtitle)
        details.addLayout(labels, 1)
        self.remark_button = QPushButton("修改备注…")
        self.remark_button.setEnabled(False)
        self.remark_button.clicked.connect(self.edit_remark)
        details.addWidget(self.remark_button)
        rv.addLayout(details)
        self.remark_status = plain_label("备注修改方式：编辑草稿 → 在微信保存 → 刷新核对", "muted")
        rv.addWidget(self.remark_status)
        search = QHBoxLayout()
        self.message_search = QLineEdit()
        self.message_search.setClearButtonEnabled(True)
        self.message_search.setPlaceholderText("在当前联系人的本机聊天记录中搜索")
        self.message_search.returnPressed.connect(self.load_messages)
        search.addWidget(self.message_search, 1)
        self.search_button = QPushButton("读取聊天")
        self.search_button.setEnabled(False)
        self.search_button.clicked.connect(self.load_messages)
        search.addWidget(self.search_button)
        rv.addLayout(search)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.setHtml(self.empty_html("你的聊天记录，留在本机", "读取后选择联系人，再点击“读取聊天”。首次读取历史库可能需要一段时间。"))
        rv.addWidget(self.browser, 1)
        foot = QHBoxLayout()
        self.message_count = plain_label("仅显示这台电脑保存的聊天记录", "muted")
        foot.addWidget(self.message_count, 1)
        self.more_button = QPushButton("加载更早记录")
        self.more_button.setEnabled(False)
        self.more_button.clicked.connect(lambda: self.load_messages(append=True))
        self.coverage_button = QPushButton("读取详情")
        self.coverage_button.clicked.connect(self.show_coverage)
        foot.addWidget(self.coverage_button)
        foot.addWidget(self.more_button)
        rv.addLayout(foot)
        split.addWidget(right)
        split.setSizes([460, 750])
        outer.addWidget(split, 1)
        bottom = QHBoxLayout()
        self.progress_label = plain_label("请先登录电脑微信，再读取当前账号。", "muted")
        bottom.addWidget(self.progress_label, 1)
        self.cancel_button = QPushButton("取消读取")
        self.cancel_button.hide()
        self.cancel_button.clicked.connect(self.cancel_job)
        bottom.addWidget(self.cancel_button)
        outer.addLayout(bottom)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 1)
        outer.addWidget(self.progress_bar)
        self.setCentralWidget(root)

    @staticmethod
    def empty_html(title, subtitle):
        return f'<div style="margin-top:100px; text-align:center; color:#60796b;"><h2 style="color:#345744;">{html.escape(title)}</h2><p>{html.escape(subtitle)}</p></div>'

    @Slot()
    def filter_contacts(self, *_):
        self.proxy.query = self.contact_search.text().strip().casefold()
        self.proxy.category = self.category.currentIndex()
        self.proxy.invalidateFilter()
        self.contact_count.setText(f"{self.proxy.rowCount():,} / {self.model.rowCount():,} 条")

    def refresh_drafts(self, *, update_filter=True):
        try:
            drafts = self.store.list_for(self.account) if self.account else {}
            self.drafts_error = None
        except Exception as error:
            drafts = {}
            self.drafts_error = str(error)
        self.proxy.pending = {u for u, d in drafts.items() if d["status"] == "pending" or self.reconcile_error}
        if update_filter:
            self.filter_contacts()
        if self.drafts_error:
            self.remark_status.setText(f"备注草稿暂不可用：{self.drafts_error}")
            self.remark_button.setEnabled(False)
            return
        if self.current_contact:
            draft = drafts.get(self.current_contact["username"])
            if draft:
                observed_match = (self.current_contact.get("remark") or "") == draft["new_remark"]
                if self.reconcile_error:
                    prefix = "本次备注核验未完成"
                elif draft["status"] == "pending" or not observed_match:
                    prefix = "待在微信保存 / 核对"
                else:
                    prefix = "已从微信读取并核对一致"
                self.remark_status.setText(f"{prefix}：{draft['new_remark'] or '（空备注）'}")
            else:
                self.remark_status.setText("备注修改方式：编辑草稿 → 在微信保存 → 刷新核对")

    @Slot()
    def select_contact(self, *_):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.current_contact = None
            self.contact_title.setText("选择一位联系人")
            self.contact_subtitle.setText("从左侧选择联系人，浏览资料和聊天记录。")
            self.remark_status.setText("备注修改方式：编辑草稿 → 在微信保存 → 刷新核对")
            self.messages_data, self.cursor, self.has_more = [], None, False
            self.last_coverage = {}
            self.browser.setHtml(self.empty_html("选择一位联系人", "可搜索昵称、微信备注或微信号。"))
            self.message_count.setText("尚未读取当前联系人的聊天记录")
            self.remark_button.setEnabled(False)
            self.search_button.setEnabled(False)
            self.more_button.setEnabled(False)
            return
        c = self.model.rows[self.proxy.mapToSource(rows[0]).row()]
        self.current_contact = c
        self.contact_title.setText(display_name(c))
        self.contact_subtitle.setText(f"昵称：{c.get('nick_name') or '—'}\n微信号 / 标识：{c.get('alias') or c['username']}")
        self.messages_data, self.cursor, self.has_more = [], None, False
        self.last_coverage = {}
        self.message_search.clear()
        self.browser.setHtml(self.empty_html("准备查看聊天记录", "点击“读取聊天”，查看此联系人的本机记录。"))
        self.message_count.setText("尚未读取当前联系人的聊天记录")
        self.remark_button.setEnabled(self.thread is None)
        self.search_button.setEnabled(self.thread is None)
        self.more_button.setEnabled(False)
        self.refresh_drafts(update_filter=False)

    @Slot()
    def connect_reader(self):
        self._start_job("start", {})

    @Slot()
    def load_messages(self, checked=False, *, append=False):
        if not self.current_contact or self.thread is not None:
            return
        keyword = self.message_search.text().strip()
        if keyword != self.last_keyword:
            append = False
        if append and (not self.cursor or len(self.messages_data) >= 2000):
            return
        self._append = append
        self._request_keyword = keyword
        self._start_job("messages", {"username": self.current_contact["username"], "limit": 100,
            "keyword": keyword, "before": self.cursor if append else None})

    def _start_job(self, method, kwargs):
        if self.thread is not None:
            return
        self.reader.reset_cancel()
        self._cancel_requested = False
        self._job_method, self._job_result, self._job_error = method, None, None
        self._selection_before = self.current_contact["username"] if self.current_contact else None
        if method == "start":
            # Never retain an old account's selectable contacts after a failed rebind.
            self.model.replace([])
            self.account = ""
            self.reconcile_error = None
            self.current_contact = None
            self.messages_data, self.cursor, self.has_more = [], None, False
            self.last_coverage = {}
            self.contact_count.setText("正在读取…")
            self.badge.setText("演示模式" if self.demo else "正在连接")
        self.thread = QThread(self)
        self.job = ReaderJob(self.reader, method, kwargs)
        self.job.moveToThread(self.thread)
        self.thread.started.connect(self.job.run)
        self.job.progress.connect(self.progress_label.setText)
        self.job.succeeded.connect(self._receive_result)
        self.job.failed.connect(self._receive_error)
        self.job.finished.connect(self.thread.quit)
        self.job.finished.connect(self.job.deleteLater)
        self.thread.finished.connect(self._job_finished)
        self.progress_bar.setRange(0, 0)
        self.cancel_button.show()
        for widget in (self.connect_button, self.table, self.contact_search, self.category,
                       self.remark_button, self.search_button, self.more_button, self.message_search):
            widget.setEnabled(False)
        self.progress_label.setText("正在读取，请稍候…")
        self.thread.start()

    @Slot(object)
    def _receive_result(self, result):
        if not self._cancel_requested:
            self._job_result = result

    @Slot(str)
    def _receive_error(self, error):
        self._job_error = error

    @Slot()
    def _job_finished(self):
        thread = self.thread
        self.thread = self.job = None
        thread.deleteLater()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.cancel_button.hide()
        for widget in (self.connect_button, self.table, self.contact_search, self.category, self.message_search):
            widget.setEnabled(True)
        if self._job_error:
            self.progress_label.setText(f"读取未完成：{self._job_error}")
            if self._job_method == "start":
                self.badge.setText("演示模式" if self.demo else "尚未连接")
                self.contact_count.setText("0 条")
        elif self._job_result is not None:
            try:
                self._apply_result(self._job_method, self._job_result)
            except Exception as error:
                self.progress_label.setText(f"结果处理失败：{error}")
                self._job_error = str(error)
        self.remark_button.setEnabled(self.current_contact is not None and self.drafts_error is None)
        self.search_button.setEnabled(self.current_contact is not None)
        self.more_button.setEnabled(self.current_contact is not None and self.has_more and self.cursor is not None and len(self.messages_data) < 2000)
        self.operation_finished.emit(self._job_method, self._job_result if not self._job_error else {"error": self._job_error})
        if self._closing:
            self.close()

    def _apply_result(self, method, result):
        if method == "start":
            account = result["account"]
            self.account = account["account_hint"] if isinstance(account, dict) else str(account)
            contacts = result["contacts"]
            try:
                verified = self.store.reconcile(self.account, contacts)
                self.reconcile_error = None
            except Exception as error:
                verified = []
                self.reconcile_error = str(error)
            self.model.replace(contacts)
            self.current_contact = None
            self.messages_data, self.cursor, self.has_more = [], None, False
            self.contact_title.setText("选择一位联系人")
            self.contact_subtitle.setText("从左侧选择联系人，浏览资料和聊天记录。")
            self.browser.setHtml(self.empty_html("通讯录已读取", "选择联系人，再点击“读取聊天”。"))
            self.message_count.setText("仅显示这台电脑保存的聊天记录")
            self.badge.setText("演示模式" if self.demo else f"已连接 · {result.get('version', '')}")
            self.connect_button.setText("刷新并核对备注")
            self.refresh_drafts()
            if self._selection_before:
                for row, c in enumerate(self.model.rows):
                    if c["username"] == self._selection_before:
                        mapped = self.proxy.mapFromSource(self.model.index(row, 0))
                        if mapped.isValid():
                            self.table.selectRow(mapped.row())
                        break
            self.progress_label.setText(f"已读取 {len(contacts):,} 条联系人缓存。" + (f" {len(verified)} 条备注已核对一致。" if verified else " 可选择联系人查看聊天。"))
            if self.reconcile_error:
                self.progress_label.setText(f"通讯录已读取；备注核验未完成：{self.reconcile_error}")
        else:
            rows = result.get("messages", [])
            self.last_keyword = getattr(self, "_request_keyword", self.last_keyword)
            self.messages_data = rows + self.messages_data if self._append else rows
            self.cursor, self.has_more = result.get("next_before"), bool(result.get("has_more"))
            self.last_coverage = result.get("coverage", {})
            self.render_messages()
            self.message_count.setText(f"已显示 {len(self.messages_data):,} 条 · 本次检查 {result.get('scanned', 0):,} 条")
            self.progress_label.setText(self._coverage_label(self.last_coverage))
            if len(self.messages_data) >= 2000:
                self.progress_label.setText("当前视图已达 2,000 条上限。请使用关键词缩小结果范围。")

    @staticmethod
    def _coverage_label(coverage):
        if coverage.get("demo"):
            return "演示数据：所有联系人与对话均为虚构。"
        # Keep incomplete results explicit, including reader-version additions.
        incomplete = (coverage.get("complete") is False or coverage.get("partial")
                      or any(coverage.get(k) for k in ("failures", "decode_failures", "budget_exhausted", "errors", "scan_limit_reached")))
        if incomplete:
            return "当前结果不完整，点击“读取详情”查看原因。可重试读取。"
        count = coverage.get("shards_read")
        prefix = f"已读取 {count} 个本机分库。" if count is not None else "已读取本机聊天快照。"
        return prefix + "图片、语音等仅显示类型；手机端独有的记录不包含在内。"

    @Slot()
    def show_coverage(self):
        c = self.last_coverage
        detail = (f"本机分库：{c.get('shards_read', '—')} / {c.get('shards_detected', '—')}\n"
                  f"正文解码失败：{c.get('decode_failures', 0)}\n"
                  f"达到扫描上限：{'是' if c.get('budget_exhausted') else '否'}\n\n"
                  "记录范围仅限当前电脑；不保证涵盖手机上的全部历史。\n"
                  "图片、语音、视频和文件显示消息类型，不读取附件。\n"
                  "快照已做页完整性校验，但未持有微信的数据库事务锁。")
        for failure in c.get("failures", []):
            detail += f"\n{failure['shard']}：{failure['reason']}"
        QMessageBox.information(self, "读取范围与状态", detail)

    def render_messages(self):
        if not self.messages_data:
            self.browser.setHtml(self.empty_html("没有找到聊天记录", "当前联系人可能没有本机记录，或没有匹配的关键词。"))
            return
        parts = ['<html><body style="font-family:Microsoft YaHei UI; font-size:14px; color:#263c31;">']
        for m in self.messages_data:
            try:
                timestamp = datetime.fromtimestamp(m.get("create_time") or 0).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError, OverflowError):
                timestamp = "时间未知"
            sender = m.get("sender_name") or m.get("sender_username") or "发送者未知"
            text = str(m.get("text") or "（空消息）")
            base_type = int(m.get("local_type") or 0) & 0xFFFFFFFF
            if base_type not in (1, 10000):
                text = f"【{m.get('type_name') or '非文本消息'}】"
            elif m.get("decode_error"):
                text = f"【正文未能解码：{m['decode_error']}】"
            if len(text) > 40000:
                text = text[:40000] + "\n…（长消息显示已截断）"
            parts.append(f'<p style="color:#7a8d82;font-size:11px;margin-top:18px;">{html.escape(sender)} &nbsp; {timestamp}</p>'
                         f'<table width="96%" cellpadding="12" cellspacing="0" bgcolor="#ffffff"><tr><td style="white-space:pre-wrap;">{html.escape(text).replace(chr(10), "<br>")}</td></tr></table>')
        parts.append("</body></html>")
        self.browser.setHtml("".join(parts))
        if not self._append:
            self.browser.verticalScrollBar().setValue(self.browser.verticalScrollBar().maximum())

    @Slot()
    def edit_remark(self):
        if not self.current_contact or self.thread is not None:
            return
        try:
            dialog = RemarkDialog(self.current_contact, self.store, self.account, self)
            dialog.exec()
        except Exception as error:
            self.remark_status.setText(f"备注草稿暂不可用：{error}")
        self.refresh_drafts()

    @Slot()
    def cancel_job(self):
        self._cancel_requested = True
        self._job_result = None
        self._job_error = "读取已取消"
        self.reader.cancel()
        self.progress_label.setText("正在取消读取，等待当前安全步骤结束…")

    def closeEvent(self, event):
        if self.thread is not None:
            self._closing = True
            self.cancel_job()
            event.ignore()
            return
        self.reader.close()
        if self._demo_dir:
            self._demo_dir.cleanup()
        event.accept()
