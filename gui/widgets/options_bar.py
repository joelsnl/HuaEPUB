# Author: joelsnl and Anthropic Claude
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from core.settings import get_default_books_dir


class OptionsBar(QWidget):
    """Shared download/translate options."""
    options_changed = Signal()

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        s = session.settings

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        row1 = QHBoxLayout()
        row2 = QHBoxLayout()

        self.clean_cb = QCheckBox("Remove watermarks & ads")
        self.clean_cb.setChecked(bool(s.get("clean", True)))
        self.translate_cb = QCheckBox("Translate to English")
        self.translate_cb.setChecked(bool(s.get("translate", True)))
        self.cache_cb = QCheckBox("Use chapter cache (resume)")
        self.cache_cb.setChecked(bool(s.get("use_chapter_cache", True)))
        self.clipboard_cb = QCheckBox("Watch clipboard for URLs")
        self.clipboard_cb.setChecked(bool(s.get("clipboard_watcher", False)))

        for cb in (self.clean_cb, self.translate_cb, self.cache_cb, self.clipboard_cb):
            row1.addWidget(cb)
            # Widgets emit a value; ignore it — options_changed takes no args
            cb.stateChanged.connect(self._emit_options)
        row1.addStretch(1)

        row2.addWidget(QLabel("Translator:"))
        self.backend = QComboBox()
        self.backend.addItems(["Google", "LibreTranslate"])
        self.backend.setCurrentText(
            "LibreTranslate" if s.get("translation_backend") == "libretranslate" else "Google"
        )
        self.backend.currentTextChanged.connect(self._emit_options)
        row2.addWidget(self.backend)

        row2.addWidget(QLabel("Workers:"))
        self.workers = QSpinBox()
        self.workers.setRange(1, 500)
        # Native spin arrows disappear under our dark QSS — use explicit +/− buttons
        self.workers.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.workers.setMinimumWidth(64)
        self.workers.setAlignment(Qt.AlignCenter)
        self.workers.setValue(int(s.get("workers", 200) or 200))
        self.workers.valueChanged.connect(self._emit_options)

        minus = QPushButton("−")
        plus = QPushButton("+")
        for b in (minus, plus):
            b.setObjectName("secondaryBtn")
            b.setFixedSize(32, 32)
            b.setStyleSheet("font-size: 16px; font-weight: bold; padding: 0;")
        minus.clicked.connect(self.workers.stepDown)
        plus.clicked.connect(self.workers.stepUp)
        row2.addWidget(minus)
        row2.addWidget(self.workers)
        row2.addWidget(plus)

        row2.addWidget(QLabel("Save to:"))
        self.folder_label = QLabel()
        self.folder_label.setMinimumWidth(200)
        row2.addWidget(self.folder_label, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("secondaryBtn")
        browse.clicked.connect(self._browse)
        reset = QPushButton("Default")
        reset.setObjectName("secondaryBtn")
        reset.clicked.connect(self._reset_folder)
        row2.addWidget(browse)
        row2.addWidget(reset)

        root.addLayout(row1)
        root.addLayout(row2)
        self._refresh_folder_label()

    def _emit_options(self, *_args):
        self.options_changed.emit()

    def _refresh_folder_label(self):
        if self.session.output_dir:
            self.folder_label.setText(self.session.output_dir)
        else:
            self.folder_label.setText(str(get_default_books_dir()) + " (default)")

    def _browse(self):
        start = self.session.output_dir or str(get_default_books_dir())
        path = QFileDialog.getExistingDirectory(self, "Choose output folder", start)
        if path:
            self.session.output_dir = path
            self._refresh_folder_label()
            self.options_changed.emit()

    def _reset_folder(self):
        self.session.output_dir = ""
        self._refresh_folder_label()
        self.options_changed.emit()

    def snapshot(self) -> dict:
        return {
            "translate": self.translate_cb.isChecked(),
            "clean": self.clean_cb.isChecked(),
            "use_cache": self.cache_cb.isChecked(),
            "clipboard": self.clipboard_cb.isChecked(),
            "workers": self.workers.value(),
            "backend": (
                "libretranslate"
                if self.backend.currentText() == "LibreTranslate"
                else "google"
            ),
            "output_dir": self.session.output_dir or "",
        }

    def apply_snapshot(self, options: dict):
        if not options:
            return
        if "translate" in options:
            self.translate_cb.setChecked(bool(options["translate"]))
        if "clean" in options:
            self.clean_cb.setChecked(bool(options["clean"]))
        if "use_cache" in options:
            self.cache_cb.setChecked(bool(options.get("use_cache", True)))
        if "workers" in options:
            self.workers.setValue(int(options["workers"]))
        if "translation_backend" in options or "backend" in options:
            b = options.get("backend") or options.get("translation_backend")
            self.backend.setCurrentText(
                "LibreTranslate" if b == "libretranslate" else "Google"
            )
        if "output_dir" in options and options["output_dir"] is not None:
            self.session.output_dir = options["output_dir"] or ""
            self._refresh_folder_label()
