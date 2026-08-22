# Author: joelsnl and Anthropic Claude
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from core.download_job import job_chapter_urls, job_display_title


class ResumeBanner(QFrame):
    resume_clicked = Signal()
    discard_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("resumeBanner")
        self.setVisible(False)
        lay = QHBoxLayout(self)
        self.label = QLabel()
        self.label.setWordWrap(True)
        lay.addWidget(self.label, 1)
        resume = QPushButton("Resume")
        resume.setObjectName("successBtn")
        discard = QPushButton("Discard")
        discard.setObjectName("secondaryBtn")
        resume.clicked.connect(self.resume_clicked.emit)
        discard.clicked.connect(self.discard_clicked.emit)
        lay.addWidget(resume)
        lay.addWidget(discard)

    def show_job(self, job: dict, cache):
        urls = job_chapter_urls(job)
        cached = cache.count_cached_urls(urls) if urls else 0
        total = len(urls)
        title = job_display_title(job)
        detail = f"{cached}/{total} chapters cached" if total else "cached chapters will be reused"
        self.label.setText(
            f"Incomplete download: {title}\n{detail} — resume anytime (saved locally, not on Drive)."
        )
        self.setVisible(True)

    def hide_banner(self):
        self.setVisible(False)
