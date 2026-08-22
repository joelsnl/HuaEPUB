# Author: joelsnl and Anthropic Claude
from __future__ import annotations

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget


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
        lay.addWidget(self.bar)
        lay.addWidget(self.status)

        btns = QHBoxLayout()
        self.download_btn = QPushButton("Download EPUB")
        self.download_btn.setEnabled(False)
        self.download_btn.setToolTip(
            "Disabled while a download is already running. Ctrl+Enter starts "
            "the download when this button is enabled."
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

    def set_progress(self, fraction: float, status: str | None = None):
        self.bar.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
        if status is not None:
            self.status.setText(status)

    @Slot(str)
    def set_status(self, text: str):
        self.status.setText(text)

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
            self.pause_btn.setText("Pause")
            self.pause_btn.setObjectName("secondaryBtn")
        # Force style refresh after objectName change
        self.pause_btn.style().unpolish(self.pause_btn)
        self.pause_btn.style().polish(self.pause_btn)
