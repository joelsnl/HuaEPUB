# Author: joelsnl and Anthropic Claude
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut, QTextOption
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QSlider, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

from core.reader import KIND_CACHE, KIND_EPUB, ReaderBook, wrap_reader_html


class ReaderPage(QWidget):
    back_requested = Signal()
    chapter_requested = Signal(int)
    font_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.book: Optional[ReaderBook] = None
        self._index = 0
        self._font_pt = 18
        self._filling = False
        self._pending_scroll = 0.0

        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self.back_btn = QPushButton("Back")
        self.back_btn.setObjectName("secondaryBtn")
        self.back_btn.clicked.connect(self.back_requested.emit)
        self.title_lbl = QLabel("Open a novel from Library or Single to read.")
        self.title_lbl.setWordWrap(True)
        self.source_lbl = QLabel("")
        self.source_lbl.setStyleSheet("color:#aaa;")
        self.prev_btn = QPushButton("Prev")
        self.prev_btn.setObjectName("secondaryBtn")
        self.next_btn = QPushButton("Next")
        self.prev_btn.clicked.connect(self._prev)
        self.next_btn.clicked.connect(self._next)
        minus = QPushButton("A-")
        plus = QPushButton("A+")
        minus.setObjectName("secondaryBtn")
        plus.setObjectName("secondaryBtn")
        minus.setFixedWidth(40)
        plus.setFixedWidth(40)
        minus.clicked.connect(lambda: self._nudge_font(-1))
        plus.clicked.connect(lambda: self._nudge_font(1))
        self.font_slider = QSlider(Qt.Orientation.Horizontal)
        self.font_slider.setRange(12, 32)
        self.font_slider.setValue(18)
        self.font_slider.setFixedWidth(120)
        self.font_slider.valueChanged.connect(self._on_slider)
        top.addWidget(self.back_btn)
        top.addWidget(self.title_lbl, 1)
        top.addWidget(self.source_lbl)
        top.addWidget(self.prev_btn)
        top.addWidget(self.next_btn)
        top.addWidget(minus)
        top.addWidget(self.font_slider)
        top.addWidget(plus)
        root.addLayout(top)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.toc = QListWidget()
        self.toc.setMinimumWidth(180)
        self.toc.currentRowChanged.connect(self._on_toc_row)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setOpenLinks(False)
        self.view.setReadOnly(True)
        self.view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.view.setStyleSheet(
            "QTextBrowser { background:#2b2b2b; color:#e8e8e8; border:1px solid #444; }"
        )
        split.addWidget(self.toc)
        split.addWidget(self.view)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 700])
        root.addWidget(split, 1)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color:#aaa;")
        root.addWidget(self.status_lbl)

        self._bind_keys()
        self._set_nav_enabled(False)

    def _bind_keys(self):
        def add(seq, slot):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)

        add(Qt.Key.Key_Left, self._prev)
        add(Qt.Key.Key_Right, self._next)
        add("J", self._next)
        add("K", self._prev)
        add(QKeySequence.StandardKey.ZoomIn, lambda: self._nudge_font(1))
        add(QKeySequence.StandardKey.ZoomOut, lambda: self._nudge_font(-1))
        add("+", lambda: self._nudge_font(1))
        add("-", lambda: self._nudge_font(-1))

    def set_font_pt(self, pt: int):
        size = max(12, min(32, int(pt or 18)))
        self._font_pt = size
        self.font_slider.blockSignals(True)
        self.font_slider.setValue(size)
        self.font_slider.blockSignals(False)
        self._render()

    def current_index(self) -> int:
        return self._index

    def scroll_ratio(self) -> float:
        bar = self.view.verticalScrollBar()
        maximum = bar.maximum()
        if maximum <= 0:
            return 0.0
        return min(1.0, max(0.0, bar.value() / maximum))

    def set_status(self, text: str):
        self.status_lbl.setText(text or "")

    def show_empty(self, message: str = ""):
        self.book = None
        self._index = 0
        self._filling = True
        self.toc.clear()
        self._filling = False
        self.title_lbl.setText(message or "Open a novel from Library or Single to read.")
        self.source_lbl.setText("")
        self.view.clear()
        self._set_nav_enabled(False)

    def load_book(self, book: ReaderBook, *, index: int = 0, scroll: float = 0.0, font_pt: int = 18):
        self.book = book
        self._font_pt = max(12, min(32, int(font_pt or 18)))
        self.font_slider.blockSignals(True)
        self.font_slider.setValue(self._font_pt)
        self.font_slider.blockSignals(False)
        self.title_lbl.setText(book.title or "Untitled")
        if book.kind == KIND_EPUB:
            self.source_lbl.setText("EPUB")
        elif book.kind == KIND_CACHE:
            self.source_lbl.setText("Cached")
        else:
            self.source_lbl.setText(book.kind)
        self._filling = True
        self.toc.clear()
        for ch in book.chapters:
            label = f"{ch.index + 1}. {ch.title}" if ch.title else f"Chapter {ch.index + 1}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, ch.index)
            self.toc.addItem(item)
        self._filling = False
        self._set_nav_enabled(bool(book.chapters))
        self.show_chapter(index, scroll=scroll, emit=False)

    def show_chapter(self, index: int, *, scroll: float = 0.0, emit: bool = True):
        if not self.book or not self.book.chapters:
            return
        idx = max(0, min(int(index), len(self.book.chapters) - 1))
        self._index = idx
        self._pending_scroll = min(1.0, max(0.0, float(scroll or 0.0)))
        self._filling = True
        self.toc.setCurrentRow(idx)
        self._filling = False
        self._render()
        self._set_nav_enabled(True)
        if emit:
            self.chapter_requested.emit(idx)

    def update_chapter_html(self, index: int, html: str):
        if not self.book or not (0 <= index < len(self.book.chapters)):
            return
        self.book.chapters[index].html = html or ""
        if index == self._index:
            self._render()

    def _render(self):
        if not self.book or not self.book.chapters:
            self.view.clear()
            return
        ch = self.book.chapters[self._index]
        body = ch.html or "<p>This chapter is not on this PC yet.</p>"
        self.view.setHtml(wrap_reader_html(body, font_pt=self._font_pt))
        if self._pending_scroll:
            ratio = self._pending_scroll
            self._pending_scroll = 0.0

            def apply():
                bar = self.view.verticalScrollBar()
                bar.setValue(int(bar.maximum() * ratio))

            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, apply)

    def _set_nav_enabled(self, on: bool):
        n = len(self.book.chapters) if self.book else 0
        self.prev_btn.setEnabled(on and self._index > 0)
        self.next_btn.setEnabled(on and self._index + 1 < n)

    def _on_toc_row(self, row: int):
        if self._filling or row < 0:
            return
        if row == self._index:
            return
        self.show_chapter(row)

    def _prev(self):
        if self._index > 0:
            self.show_chapter(self._index - 1)

    def _next(self):
        if self.book and self._index + 1 < len(self.book.chapters):
            self.show_chapter(self._index + 1)

    def _nudge_font(self, delta: int):
        self.set_font_pt(self._font_pt + delta)
        self.font_changed.emit(self._font_pt)

    @Slot(int)
    def _on_slider(self, value: int):
        self.set_font_pt(value)
        self.font_changed.emit(self._font_pt)
