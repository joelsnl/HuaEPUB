# Author: joelsnl and Anthropic Claude
from __future__ import annotations

import re

from PySide6.QtCore import QThread, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from core.utils import pipeline_phase
from gui.icon import load_app_pixmap
from gui.widgets.slips import SealMark, SlipStrip

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

        self._value = 0
        self._phase = ""
        self.slips = SlipStrip(height=46)
        self.percent = QLabel("")
        self.percent.setObjectName("eyebrow")
        self.percent.setMinimumWidth(48)
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.seal = SealMark(size=34)
        self.seal.hide()
        strip_row = QHBoxLayout()
        strip_row.setContentsMargins(0, 0, 0, 0)
        strip_row.setSpacing(12)
        strip_row.addWidget(self.slips, 1)
        strip_row.addWidget(self.percent)
        strip_row.addWidget(self.seal)
        self.logo = QLabel()
        self.logo.setObjectName("appLogo")
        self.logo.setFixedSize(20, 20)
        pix = load_app_pixmap(20)
        if pix.isNull():
            self.logo.hide()
        else:
            self.logo.setPixmap(pix)
            self.logo.setScaledContents(True)
        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)
        status_row.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignTop)
        status_row.addWidget(self.status, 1)
        lay.addLayout(strip_row)
        lay.addLayout(status_row)

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
        # (int → 0). Show a sliver once work has started so the strip moves.
        if scaled > 0 and value == 0:
            value = 1
        if value == 0 and status and _status_has_work_count(status):
            value = 1
        self._value = value
        if status:
            phase = pipeline_phase(status)
            if phase:
                self._phase = phase
        if value <= 0:
            self._phase = pipeline_phase(status or "") if status else ""
        self.seal.hide()
        self.slips.set_progress(value / 1000.0, self._phase)
        self.percent.setText(self.percent_text())
        if status:
            self.status.setText(status)

    def value(self) -> int:
        """Progress in tenths of a percent (0–1000)."""
        return self._value

    def percent_text(self) -> str:
        """The percent painted next to the slips: '' at 0, one decimal under 10%."""
        pct = self._value / 10.0
        if self._value <= 0:
            return ""
        if pct < 10:
            return f"{pct:.1f}%"
        return f"{pct:.0f}%"

    def mark_finished(self, *, translated: bool, flagged: bool = False) -> None:
        """All slips filled; the 译 seal when the build was translated."""
        self._value = 1000
        self.slips.set_finished(flagged=flagged)
        self.percent.setText("")
        self.seal.setVisible(bool(translated))

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
