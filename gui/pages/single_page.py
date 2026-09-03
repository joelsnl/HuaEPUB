# Author: joelsnl and Anthropic Claude
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from core.parser import Chapter, NovelInfo


class SinglePage(QWidget):
    fetch_requested = Signal(str)
    recent_requested = Signal()
    read_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.novel_info: Optional[NovelInfo] = None
        self.chapters: List[Chapter] = []
        self.parser = None
        self.translated_title: Optional[str] = None

        root = QVBoxLayout(self)

        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Enter novel URL (e.g. https://twkan.com/book/12345.html)")
        self.recent_btn = QPushButton("Recent")
        self.recent_btn.setObjectName("secondaryBtn")
        self.fetch_btn = QPushButton("Fetch Chapters")
        self.fetch_btn.setToolTip("Fetch the chapter list")
        self.read_btn = QPushButton("Read")
        self.read_btn.setObjectName("secondaryBtn")
        self.read_btn.setEnabled(False)
        self.read_btn.setToolTip("Preview from the local EPUB or cached chapters")
        self.recent_btn.clicked.connect(self.recent_requested.emit)
        self.fetch_btn.clicked.connect(self._on_fetch)
        self.read_btn.clicked.connect(self.read_requested.emit)
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(self.recent_btn)
        url_row.addWidget(self.fetch_btn)
        url_row.addWidget(self.read_btn)
        root.addLayout(url_row)

        info = QHBoxLayout()
        self.cover_label = QLabel("No Cover")
        self.cover_label.setFixedSize(100, 140)
        self.cover_label.setAlignment(Qt.AlignCenter)
        self.cover_label.setStyleSheet("background:#3a3a3a;border-radius:4px;")
        info.addWidget(self.cover_label)
        meta = QVBoxLayout()
        self.title_label = QLabel("Title: -")
        self.title_label.setWordWrap(True)
        self.author_label = QLabel("Author: -")
        self.chapters_label = QLabel("Chapters: 0")
        self.eng_title_label = QLabel("English Title: -")
        self.eng_title_label.setStyleSheet("color:#aaa;")
        for w in (self.title_label, self.author_label, self.chapters_label, self.eng_title_label):
            meta.addWidget(w)
        meta.addStretch(1)
        info.addLayout(meta, 1)
        root.addLayout(info)

        sel = QHBoxLayout()
        for text, slot in (
            ("Select All", self.select_all),
            ("Select None", self.select_none),
            ("Invert", self.invert_selection),
        ):
            b = QPushButton(text)
            b.setObjectName("secondaryBtn")
            b.clicked.connect(slot)
            sel.addWidget(b)
        sel.addWidget(QLabel("Range:"))
        self.range_from = QLineEdit()
        self.range_from.setPlaceholderText("from")
        self.range_from.setFixedWidth(60)
        self.range_to = QLineEdit()
        self.range_to.setPlaceholderText("to")
        self.range_to.setFixedWidth(60)
        sel.addWidget(self.range_from)
        sel.addWidget(QLabel("-"))
        sel.addWidget(self.range_to)
        range_btn = QPushButton("Select Range")
        range_btn.setObjectName("secondaryBtn")
        range_btn.clicked.connect(self.select_range)
        sel.addWidget(range_btn)
        self.selected_label = QLabel("Selected: 0")
        sel.addStretch(1)
        sel.addWidget(self.selected_label)
        root.addLayout(sel)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._update_selected_count)
        root.addWidget(self.tree, 1)

    def _on_fetch(self):
        url = self.url_edit.text().strip()
        if url:
            self.fetch_requested.emit(url)

    def set_fetch_enabled(self, on: bool):
        self.fetch_btn.setEnabled(on)

    def show_novel(self, info: NovelInfo, chapters: List[Chapter], parser, cover_pix: Optional[QPixmap] = None):
        self.novel_info = info
        self.chapters = chapters
        self.parser = parser
        self.title_label.setText(f"Title: {info.title}")
        self.author_label.setText(f"Author: {info.author or '-'}")
        self.chapters_label.setText(f"Chapters: {len(chapters)}")
        if self.translated_title:
            self.eng_title_label.setText(f"English Title: {self.translated_title}")
        else:
            self.eng_title_label.setText("English Title: -")
        if cover_pix and not cover_pix.isNull():
            self.cover_label.setPixmap(
                cover_pix.scaled(100, 140, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        else:
            self.cover_label.setText("No Cover")
            self.cover_label.setPixmap(QPixmap())
        self.populate_chapters(select_all=True)
        self.read_btn.setEnabled(bool(chapters))

    def populate_chapters(self, select_all: bool = True):
        self.tree.clear()
        for i, ch in enumerate(self.chapters):
            item = QTreeWidgetItem([f"{i + 1}. {ch.title}"])
            item.setData(0, Qt.UserRole, i)
            self.tree.addTopLevelItem(item)
        if select_all:
            self.select_all()
        self._update_selected_count()

    def selected_chapters(self) -> List[Chapter]:
        indices = sorted({
            it.data(0, Qt.UserRole)
            for it in self.tree.selectedItems()
            if it.data(0, Qt.UserRole) is not None
        })
        return [self.chapters[i] for i in indices if 0 <= i < len(self.chapters)]

    def select_all(self):
        self.tree.selectAll()

    def select_none(self):
        self.tree.clearSelection()

    def invert_selection(self):
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            item.setSelected(not item.isSelected())

    def select_range(self):
        try:
            a = int(self.range_from.text().strip())
            b = int(self.range_to.text().strip())
        except ValueError:
            return
        if a > b:
            a, b = b, a
        self.tree.clearSelection()
        for i in range(self.tree.topLevelItemCount()):
            idx = self.tree.topLevelItem(i).data(0, Qt.UserRole)
            if a <= idx + 1 <= b:
                self.tree.topLevelItem(i).setSelected(True)

    def _update_selected_count(self):
        self.selected_label.setText(f"Selected: {len(self.tree.selectedItems())}")

    def set_url(self, url: str):
        self.url_edit.setText(url)
