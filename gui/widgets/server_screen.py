# Author: joelsnl and Anthropic Claude
"""What the desktop window shows while server mode is on.

The tabs are replaced by this screen: where to point a browser, how to sign
in, what the browser is doing right now, and the switch to stop serving.
Nothing here starts work; the main window owns the ServerHost.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from gui import theme
from gui.widgets.qr import qr_pixmap
from gui.widgets.slips import SlipStrip

QR_SIZE = 216


def _eyebrow(text: str) -> QLabel:
    lbl = QLabel(text.upper())
    lbl.setObjectName("eyebrow")
    return lbl


def _value(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("serverValue")
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lbl.setWordWrap(True)
    return lbl


def _rule() -> QFrame:
    line = QFrame()
    line.setObjectName("rule")
    line.setFrameShape(QFrame.Shape.NoFrame)
    return line


def format_code(code: str) -> str:
    """'abcd2345' → 'abcd 2345' so it is easy to read out loud."""
    code = (code or "").strip()
    return f"{code[:4]} {code[4:]}" if len(code) == 8 else code


def task_line(task: dict | None) -> str:
    """One line about the browser's current job, or that nothing runs."""
    if not task:
        return "Nothing running. Start a download from the browser."
    state = task.get("state") or ""
    label = task.get("label") or "Job"
    message = (task.get("message") or "").split("\n")[0]
    if state == "running":
        phase = task.get("phase_label") or ""
        head = f"{phase} · {label}" if phase else label
        return f"{head}\n{message}" if message else head
    if state == "paused":
        return f"Paused · {label}"
    if state == "done":
        return f"Finished · {label}"
    if state == "cancelled":
        return f"Cancelled · {label}"
    if state == "error":
        return f"Stopped with an error · {label}\n{task.get('error') or ''}".rstrip()
    return label


class ServerScreen(QWidget):
    open_browser = Signal()
    new_code = Signal()
    change_password = Signal()
    stop_serving = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("serverScreen")
        self._qr_text = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 24)
        root.setSpacing(18)

        self.title = QLabel("Serving")
        self.title.setObjectName("serverTitle")
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("mutedLabel")
        self.subtitle.setWordWrap(True)
        root.addWidget(self.title)
        root.addWidget(self.subtitle)
        root.addWidget(_rule())

        body = QHBoxLayout()
        body.setSpacing(36)
        left = QGridLayout()
        left.setHorizontalSpacing(20)
        left.setVerticalSpacing(14)
        left.setColumnStretch(1, 1)

        row = 0
        left.addWidget(_eyebrow("Address"), row, 0, Qt.AlignmentFlag.AlignTop)
        self.addresses = _value()
        left.addWidget(self.addresses, row, 1)
        row += 1

        self.code_eyebrow = _eyebrow("Access code")
        left.addWidget(self.code_eyebrow, row, 0, Qt.AlignmentFlag.AlignTop)
        code_box = QVBoxLayout()
        code_box.setSpacing(6)
        self.code = QLabel("")
        self.code.setObjectName("serverCode")
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_hint = QLabel("")
        self.code_hint.setObjectName("hintLabel")
        self.code_hint.setWordWrap(True)
        code_btns = QHBoxLayout()
        self.new_code_btn = QPushButton("New code")
        self.new_code_btn.setObjectName("secondaryBtn")
        self.new_code_btn.clicked.connect(self.new_code.emit)
        self.password_btn = QPushButton("Change password…")
        self.password_btn.setObjectName("secondaryBtn")
        self.password_btn.clicked.connect(self.change_password.emit)
        code_btns.addWidget(self.new_code_btn)
        code_btns.addWidget(self.password_btn)
        code_btns.addStretch(1)
        code_box.addWidget(self.code)
        code_box.addWidget(self.code_hint)
        code_box.addLayout(code_btns)
        left.addLayout(code_box, row, 1)
        row += 1

        self.fp_eyebrow = _eyebrow("Certificate")
        self.fingerprint = _value()
        left.addWidget(self.fp_eyebrow, row, 0, Qt.AlignmentFlag.AlignTop)
        left.addWidget(self.fingerprint, row, 1)
        row += 1

        left.addWidget(_eyebrow("Now"), row, 0, Qt.AlignmentFlag.AlignTop)
        now_box = QVBoxLayout()
        now_box.setSpacing(8)
        self.task = QLabel("")
        self.task.setWordWrap(True)
        self.slips = SlipStrip(height=40)
        self.slips.hide()
        now_box.addWidget(self.task)
        now_box.addWidget(self.slips)
        left.addLayout(now_box, row, 1)
        row += 1
        left.setRowStretch(row, 1)

        right = QVBoxLayout()
        right.setSpacing(8)
        self.qr = QLabel()
        self.qr.setObjectName("qrLabel")
        self.qr.setFixedSize(QR_SIZE + 16, QR_SIZE + 16)
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_caption = QLabel("")
        self.qr_caption.setObjectName("hintLabel")
        self.qr_caption.setWordWrap(True)
        self.qr_caption.setFixedWidth(QR_SIZE + 16)
        right.addWidget(self.qr)
        right.addWidget(self.qr_caption)
        right.addStretch(1)

        body.addLayout(left, 1)
        body.addLayout(right, 0)
        root.addLayout(body, 1)

        root.addWidget(_rule())
        foot = QHBoxLayout()
        self.note = QLabel("The desktop tabs are paused while serving. "
                           "Closing HuaEPUB stops the server.")
        self.note.setObjectName("hintLabel")
        self.note.setWordWrap(True)
        self.open_btn = QPushButton("Open in browser")
        self.open_btn.clicked.connect(self.open_browser.emit)
        self.stop_btn = QPushButton("Stop serving")
        self.stop_btn.setObjectName("dangerBtn")
        self.stop_btn.clicked.connect(self.stop_serving.emit)
        foot.addWidget(self.note, 1)
        foot.addWidget(self.open_btn)
        foot.addWidget(self.stop_btn)
        root.addLayout(foot)

    # -- content -------------------------------------------------------------
    def show_status(self, status, *, code: str, qr_text: str) -> None:
        remote = status.mode == "remote"
        self.title.setText("Serving anywhere" if remote else "Serving on your network")
        if remote:
            self.subtitle.setText(
                "HTTPS on port {0}. Forward this port on your router to reach it from outside "
                "your network.".format(status.port))
        else:
            self.subtitle.setText("Open the address on a phone or computer connected to the "
                                  "same network, or scan the code.")
        lines = []
        if status.public_url:
            lines.append(f"{status.public_url}   (internet)")
        if status.lan_url:
            lines.append(f"{status.lan_url}   (this network)")
        lines.append(f"{status.local_url}   (this PC)")
        self.addresses.setText("\n".join(lines))

        self.code_eyebrow.setText("ACCESS CODE" if not remote else "SIGN-IN")
        self.new_code_btn.setVisible(not remote)
        self.password_btn.setVisible(remote)
        if remote:
            self.code.setText("Password")
            self.code_hint.setText("Sign in with the password you set. After five wrong tries, "
                                   "that address has to wait up to 15 minutes.")
        else:
            self.code.setText(format_code(code))
            self.code_hint.setText("A new code signs out every other device.")
        self.fp_eyebrow.setVisible(remote)
        self.fingerprint.setVisible(remote)
        if remote:
            kind = "Your certificate" if status.own_certificate else "Made by HuaEPUB"
            self.fingerprint.setText(f"{kind}\nSHA-256 {status.fingerprint}")
        self._qr_text = qr_text
        self.qr_caption.setText(
            "Scan with your phone's camera. The code is inside, so keep it to yourself."
            if not remote else "Scan to open the address. You still need the password.")
        self.repaint_qr()

    def repaint_qr(self) -> None:
        pal = theme.current()
        ratio = self.devicePixelRatioF() if self.isVisible() else (
            QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0)
        # Dark modules on a light square read best with every phone camera.
        dark, light = ("#0F1214", pal.ink) if pal.dark else (pal.ink, pal.ground)
        pix = qr_pixmap(self._qr_text, QR_SIZE, dark=dark, light=light, ratio=ratio)
        if pix.isNull():
            self.qr.setText("Install the qrcode package to show a QR code here.")
            self.qr.setWordWrap(True)
        else:
            self.qr.setPixmap(pix)

    def show_task(self, task: dict | None) -> None:
        self.task.setText(task_line(task))
        running = bool(task) and task.get("state") in ("running", "paused")
        if task and task.get("kind") in ("single", "multi", "library_update",
                                         "library_update_all"):
            if task.get("state") == "done":
                self.slips.set_finished(flagged=bool((task.get("result") or {}).get("warnings")))
            else:
                self.slips.set_progress(float(task.get("fraction") or 0.0), task.get("phase") or "")
            self.slips.setVisible(True)
        else:
            self.slips.setVisible(running)
            if running:
                self.slips.set_progress(float(task.get("fraction") or 0.0), task.get("phase") or "")
