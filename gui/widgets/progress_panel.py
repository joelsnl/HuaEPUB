# Author: joelsnl and Anthropic Claude
from __future__ import annotations

import re

from PySide6.QtCore import QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

_COUNT_RE = re.compile(r"\d+\s*/\s*\d+")


def _status_has_work_count(status: str) -> bool:
    """True when the line already has a real n/N (bar must leave 0%)."""
    if not status or "Starting download" in status:
        return False
    return bool(_COUNT_RE.search(status))


class ProgressPanel(QWidget):
    pause_clicked = Signal()
    cancel_clicked = Signal()
    download_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        lay.addWidget(self.bar)
        lay.addWidget(self.status)

        btns = QHBoxLayout()
        self.download_btn = QPushButton("Download EPUB")
        self.download_btn.setEnabled(False)
        self.download_btn.setToolTip(
            "Disabled while a download is already running."
        )
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setObjectName("secondaryBtn")
        self.pause_btn.setEnabled(False)
        self.pause_btn.setToolTip(
            "Stop between chapters. Safe to close the app while paused."
        )
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("dangerBtn")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setToolTip(
            "Clears the resume point (Esc). Cached chapter text stays. "
            "Cancel during translation writes no EPUB; "
            "cancel during polish still saves the machine-translated EPUB."
        )
        self.download_btn.clicked.connect(self.download_clicked.emit)
        self.pause_btn.clicked.connect(self.pause_clicked.emit)
        self.cancel_btn.clicked.connect(self.cancel_clicked.emit)
        btns.addStretch(1)
        btns.addWidget(self.download_btn)
        btns.addWidget(self.pause_btn)
        btns.addWidget(self.cancel_btn)
        btns.addStretch(1)
        lay.addLayout(btns)

    def _on_gui(self, fn) -> None:
        """QLabel.setText from a worker/pool thread is ignored on Windows."""
        if QThread.currentThread() == self.thread():
            fn()
            return
        QTimer.singleShot(0, self, fn)

    @Slot(float)
    @Slot(float, str)
    def set_progress(self, fraction: float, status: str | None = None):
        frac = float(fraction)
        text = status
        self._on_gui(lambda f=frac, s=text: self._apply_progress(f, s))

    def _apply_progress(self, fraction: float, status: str | None):
        scaled = max(0.0, min(1.0, fraction)) * 1000
        value = int(scaled)
        # Multi-download maps one chapter of four 500-ch books to ~0.00025
        # (int → 0). Show a sliver once work has started so the bar moves.
        if scaled > 0 and value == 0:
            value = 1
        if value == 0 and status and _status_has_work_count(status):
            value = 1
        self.bar.setValue(value)
        if status:
            self.status.setText(status)

    @Slot(str)
    def set_status(self, text: str):
        self._on_gui(lambda t=text: self.status.setText(t))

    def set_download_enabled(self, on: bool):
        self.download_btn.setEnabled(on)

    def set_controls_active(self, active: bool, paused: bool = False):
        self.pause_btn.setEnabled(active)
        self.cancel_btn.setEnabled(active)
        if not active:
            self.pause_btn.setText("Pause")
            self.pause_btn.setObjectName("secondaryBtn")
        elif paused:
            self.pause_btn.setText("Resume")
            self.pause_btn.setObjectName("successBtn")
        else:
            # Default blue QPushButton — #secondaryBtn is the same grey as
            # :disabled, so Pause looked off while Cancel (red) looked on.
            self.pause_btn.setText("Pause")
            self.pause_btn.setObjectName("")
        # Force style refresh after objectName change
        self.pause_btn.style().unpolish(self.pause_btn)
        self.pause_btn.style().polish(self.pause_btn)
