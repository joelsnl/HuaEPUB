# Author: joelsnl and Anthropic Claude
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QPlainTextEdit, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.utils import extract_urls


class MultiPage(QWidget):
    fetch_all_requested = Signal()
    download_all_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.novels = []  # list of dicts

        root = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel("Paste novel URLs (one per line or mixed text)"))
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("secondaryBtn")
        self.fetch_btn = QPushButton("Fetch All")
        self.fetch_btn.setToolTip("Fetch every pasted URL")
        self.clear_btn.clicked.connect(self.clear)
        self.fetch_btn.clicked.connect(self.fetch_all_requested.emit)
        header.addStretch(1)
        header.addWidget(self.clear_btn)
        header.addWidget(self.fetch_btn)
        root.addLayout(header)

        self.url_text = QPlainTextEdit()
        self.url_text.setPlaceholderText("https://twkan.com/book/...\nhttps://uukanshu.cc/book/...")
        self.url_text.setFixedHeight(110)
        root.addWidget(self.url_text)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Title", "Chapters", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(2, 90)
        root.addWidget(self.table, 1)

        self.download_btn = QPushButton("Download All")
        self.download_btn.setObjectName("successBtn")
        self.download_btn.setEnabled(False)
        self.download_btn.setToolTip("Download every fetched novel")
        self.download_btn.clicked.connect(self.download_all_requested.emit)
        root.addWidget(self.download_btn)

    def clear(self):
        self.url_text.clear()
        self.table.setRowCount(0)
        self.novels = []
        self.download_btn.setEnabled(False)

    def get_urls(self):
        return extract_urls(self.url_text.toPlainText())

    def append_urls(self, urls):
        current = self.url_text.toPlainText().strip()
        block = "\n".join(urls)
        if current:
            self.url_text.appendPlainText("\n" + block)
        else:
            self.url_text.setPlainText(block)

    def begin_fetch(self, urls):
        self.novels = []
        self.table.setRowCount(len(urls))
        for i, url in enumerate(urls):
            self.table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.table.setItem(i, 1, QTableWidgetItem(url[:60]))
            self.table.setItem(i, 2, QTableWidgetItem("-"))
            self.table.setItem(i, 3, QTableWidgetItem("Fetching…"))
        self.fetch_btn.setEnabled(False)
        self.download_btn.setEnabled(False)

    def set_row(self, idx: int, title: str, chapters: int, status: str, novel=None):
        if idx >= self.table.rowCount():
            return
        self.table.setItem(idx, 1, QTableWidgetItem(title))
        self.table.setItem(idx, 2, QTableWidgetItem(f"{chapters} ch." if chapters else "-"))
        self.table.setItem(idx, 3, QTableWidgetItem(status))
        if novel is not None:
            while len(self.novels) <= idx:
                self.novels.append(None)
            self.novels[idx] = novel

    def set_status(self, idx: int, status: str):
        if 0 <= idx < self.table.rowCount():
            self.table.setItem(idx, 3, QTableWidgetItem(status))

    def fetched_novels(self):
        return [n for n in self.novels if n and n.get("status") == "fetched"]

    def set_busy(self, busy: bool):
        self.fetch_btn.setEnabled(not busy)
        self.clear_btn.setEnabled(not busy)
        if not busy:
            self.download_btn.setEnabled(bool(self.fetched_novels()))
        else:
            self.download_btn.setEnabled(False)
