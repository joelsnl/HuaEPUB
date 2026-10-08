# Author: joelsnl and Anthropic Claude
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QSlider, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

from core.reader import KIND_CACHE, KIND_EPUB, ReaderBook, wrap_reader_html
from core.utils import format_count
from gui import theme

_PAPER = {
    "paper": ("#1b1612", "#f7f1e8"),
    "sepia": ("#3a2a18", "#f3e2c4"),
    "night": ("#e7e2d6", "#14181b"),
}
_LEADING = {"tight": 1.45, "normal": 1.7, "loose": 2.05}


class ReaderPage(QWidget):
    back_requested = Signal()
    chapter_requested = Signal(int)
    font_changed = Signal(int)
    prefs_changed = Signal(str, str)
    bookmark_requested = Signal(int)
    place_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.book: Optional[ReaderBook] = None
        self._index = 0
        self._font_pt = 18
        self._theme = "paper"
        self._mode = "pages"
        self._face = "serif"
        self._leading = "normal"
        self._align = "justify"
        self._marks = set()
        self._filling = False
        self._pending_scroll = None
        self._press = None
        self._place_timer = QTimer(self)
        self._place_timer.setSingleShot(True)
        self._place_timer.timeout.connect(self.place_changed.emit)

        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self.back_btn = QPushButton("Back")
        self.back_btn.setObjectName("secondaryBtn")
        self.back_btn.clicked.connect(self.back_requested.emit)
        self.title_lbl = QLabel("Open a novel from Library or Single to read.")
        self.title_lbl.setWordWrap(True)
        self.source_lbl = QLabel("")
        self.source_lbl.setObjectName("mutedLabel")
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

        look = QHBoxLayout()
        self._theme_btns = {}
        for key, label in (("paper", "Paper"), ("sepia", "Sepia"), ("night", "Night")):
            btn = QPushButton(label)
            btn.setObjectName("secondaryBtn")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked=False, name=key: self._choose_theme(name))
            self._theme_btns[key] = btn
            look.addWidget(btn)
        self.mode_btn = QPushButton("Pages")
        self.mode_btn.setObjectName("secondaryBtn")
        self.mode_btn.setCheckable(True)
        self.mode_btn.clicked.connect(self._toggle_mode)
        self.mark_btn = QPushButton("Bookmark")
        self.mark_btn.setObjectName("secondaryBtn")
        self.mark_btn.setCheckable(True)
        self.mark_btn.clicked.connect(self._toggle_mark)
        look.addWidget(self.mode_btn)
        look.addWidget(self.mark_btn)
        look.addStretch(1)
        root.addLayout(look)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.toc = QListWidget()
        self.toc.setMinimumWidth(180)
        self.toc.currentRowChanged.connect(self._on_toc_row)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setOpenLinks(False)
        self.view.setReadOnly(True)
        self.view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.view.setObjectName("readerView")
        self.view.viewport().installEventFilter(self)
        split.addWidget(self.toc)
        split.addWidget(self.view)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 700])
        root.addWidget(split, 1)

        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("mutedLabel")
        root.addWidget(self.status_lbl)

        self._set_nav_enabled(False)

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

    def load_book(self, book: ReaderBook, *, index: int = 0, scroll: float = 0.0, font_pt: int = 18,
                  theme: str = "paper", mode: str = "pages", face: str = "serif",
                  leading: str = "normal", align: str = "justify", marks=None):
        self.book = book
        self._theme = theme if theme in _PAPER else "paper"
        self._mode = "scroll" if mode == "scroll" else "pages"
        self._face = "sans" if face == "sans" else "serif"
        self._leading = leading if leading in _LEADING else "normal"
        self._align = "left" if align == "left" else "justify"
        self._marks = set()
        for row in marks or []:
            if not isinstance(row, dict):
                continue
            try:
                self._marks.add((int(row["chapter_index"]), round(float(row.get("scroll") or 0), 2)))
            except (TypeError, ValueError, KeyError):
                continue
        self._sync_look_buttons()
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
            n = format_count(ch.index + 1)
            label = f"{n}. {ch.title}" if ch.title else f"Chapter {n}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, ch.index)
            self.toc.addItem(item)
        self._filling = False
        self._set_nav_enabled(bool(book.chapters))
        self.show_chapter(index, scroll=scroll, emit=False)

    def set_chapter_title(self, index: int, title: str):
        if self.book is None or not (0 <= index < len(self.book.chapters)):
            return
        self.book.chapters[index].title = title
        item = self.toc.item(index)
        if item is None:
            return
        n = format_count(index + 1)
        item.setText(f"{n}. {title}" if title else f"Chapter {n}")

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
        if self._pending_scroll is None:
            ratio = self.scroll_ratio()
        else:
            ratio = self._pending_scroll
            self._pending_scroll = None
        ch = self.book.chapters[self._index]
        body = ch.html or "<p>This chapter is not on this PC yet.</p>"
        ink, paper = _PAPER[self._theme]
        family = theme.SANS if self._face == "sans" else theme.serif_family()
        self.view.setHtml(wrap_reader_html(
            body, font_pt=self._font_pt, color=ink, background=paper, font_family=family,
            line_height=_LEADING[self._leading], align=self._align,
        ))
        self._sync_mark(ratio)
        if ratio:

            def apply(kept=ratio):
                bar = self.view.verticalScrollBar()
                bar.setValue(int(bar.maximum() * kept))
                self._sync_mark(kept)

            QTimer.singleShot(0, apply)

    def _set_nav_enabled(self, on: bool):
        n = len(self.book.chapters) if self.book else 0
        if self._mode == "pages":
            self.prev_btn.setEnabled(on and n > 0)
            self.next_btn.setEnabled(on and n > 0)
        else:
            self.prev_btn.setEnabled(on and self._index > 0)
            self.next_btn.setEnabled(on and self._index + 1 < n)
        self.mark_btn.setEnabled(on and n > 0)

    def _on_toc_row(self, row: int):
        if self._filling or row < 0:
            return
        if row == self._index:
            return
        self.show_chapter(row)

    def _prev(self):
        if self._mode == "pages" and self._shift_page(-1):
            return
        if self._index > 0:
            self.show_chapter(self._index - 1, scroll=1.0 if self._mode == "pages" else 0.0)

    def _next(self):
        if self._mode == "pages" and self._shift_page(1):
            return
        if self.book and self._index + 1 < len(self.book.chapters):
            self.show_chapter(self._index + 1)

    def _page_step(self) -> int:
        return max(1, self.view.viewport().height() - 36)

    def _shift_page(self, direction: int) -> bool:
        """Move one screen. False when the chapter has no more room that way."""
        bar = self.view.verticalScrollBar()
        if direction < 0:
            if bar.value() <= 0:
                return False
            bar.setValue(max(0, bar.value() - self._page_step()))
            self._note_place()
            return True
        if bar.maximum() <= 0 or bar.value() >= bar.maximum() - 2:
            return False
        bar.setValue(min(bar.maximum(), bar.value() + self._page_step()))
        self._note_place()
        return True

    def _sync_look_buttons(self):
        for key, btn in self._theme_btns.items():
            btn.setChecked(key == self._theme)
        self.mode_btn.setChecked(self._mode == "pages")
        self.mode_btn.setText("Pages" if self._mode == "pages" else "Scroll")

    def _choose_theme(self, name: str):
        self._theme = name if name in _PAPER else "paper"
        self._sync_look_buttons()
        self._render()
        self.prefs_changed.emit("reader_theme", self._theme)

    def _toggle_mode(self):
        self._mode = "pages" if self.mode_btn.isChecked() else "scroll"
        self._sync_look_buttons()
        self._set_nav_enabled(bool(self.book and self.book.chapters))
        self.prefs_changed.emit("reader_mode", self._mode)

    def _note_place(self):
        self._sync_mark()
        self._place_timer.start(350)

    def _sync_mark(self, ratio=None):
        if ratio is None:
            ratio = self.scroll_ratio()
        on = (self._index, round(float(ratio), 2)) in self._marks
        self.mark_btn.blockSignals(True)
        self.mark_btn.setChecked(on)
        self.mark_btn.setText("Bookmarked" if on else "Bookmark")
        self.mark_btn.blockSignals(False)

    def set_bookmarks(self, rows):
        self._marks = set()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            try:
                self._marks.add((int(row["chapter_index"]), round(float(row.get("scroll") or 0), 2)))
            except (TypeError, ValueError, KeyError):
                continue
        self._sync_mark()

    def _toggle_mark(self):
        self.bookmark_requested.emit(self._index)
        self._sync_mark()

    def eventFilter(self, obj, event):
        if obj is self.view.viewport() and self.book and self._mode == "pages":
            kind = event.type()
            if kind == QEvent.Type.Wheel:
                self._prev() if event.angleDelta().y() > 0 else self._next()
                return True
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._press = event.position()
            elif (
                kind == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and self._press is not None
            ):
                moved = (event.position() - self._press).manhattanLength()
                self._press = None
                if moved < 8:
                    width = max(1, self.view.viewport().width())
                    x = event.position().x() / width
                    if x < 0.28:
                        self._prev()
                    elif x > 0.72:
                        self._next()
                    return True
        return super().eventFilter(obj, event)

    def _nudge_font(self, delta: int):
        self.set_font_pt(self._font_pt + delta)
        self.font_changed.emit(self._font_pt)

    @Slot(int)
    def _on_slider(self, value: int):
        self.set_font_pt(value)
        self.font_changed.emit(self._font_pt)
