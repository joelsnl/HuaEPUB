# Author: joelsnl and Anthropic Claude
"""File → Server mode…: where to serve, the port, and remote-mode security."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)

MIN_PASSWORD = 10


@dataclass
class ServerChoice:
    mode: str
    port: int
    hostname: str
    cert_path: str
    key_path: str
    password: str  # '' = keep the one already set


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("hintLabel")
    lbl.setWordWrap(True)
    return lbl


class ServerModeDialog(QDialog):
    """Pick This network / Anywhere before serving. Returns a ServerChoice."""

    _sig_public = Signal(str, str)

    def __init__(self, parent=None, *, settings: dict, has_password: bool,
                 find_public_address=None):
        super().__init__(parent)
        self.setWindowTitle("Server mode")
        self.setModal(True)
        self.setMinimumWidth(540)
        self.choice: ServerChoice | None = None
        self._has_password = bool(has_password)
        self._find_public = find_public_address
        self._sig_public.connect(self._on_public, Qt.ConnectionType.QueuedConnection)

        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        head = QLabel("Serve HuaEPUB to a web browser")
        head.setObjectName("serverTitle")
        lay.addWidget(head)
        lay.addWidget(_hint(
            "While serving, this window shows the address and sign-in details instead of the "
            "tabs. Use HuaEPUB from a browser on this PC, a phone or another computer. "
            "Downloads, the library, reading position and settings are the same ones this app uses."
        ))

        self.lan_rb = QRadioButton("This network (home Wi-Fi or office network)")
        self.remote_rb = QRadioButton("Anywhere (over the internet)")
        group = QButtonGroup(self)
        group.addButton(self.lan_rb)
        group.addButton(self.remote_rb)
        lay.addWidget(self.lan_rb)
        lay.addWidget(_hint("    Devices on your network sign in with an access code shown on this PC."))
        lay.addWidget(self.remote_rb)
        lay.addWidget(_hint(
            "    HTTPS with a password. Your router must forward the port to this PC. "
            "Any device on the internet can reach the sign-in page."
        ))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(settings.get("server_port") or 8765))
        form.addRow("Port", self.port)
        lay.addLayout(form)

        self.remote_box = QWidget()
        rf = QFormLayout(self.remote_box)
        rf.setContentsMargins(0, 0, 0, 0)
        self.pw_state = QLabel("A password is set. Leave these empty to keep it."
                               if self._has_password else
                               f"Set a password (at least {MIN_PASSWORD} characters).")
        self.pw_state.setObjectName("hintLabel")
        self.pw_state.setWordWrap(True)
        rf.addRow(self.pw_state)
        self.pw1 = QLineEdit()
        self.pw1.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.EchoMode.Password)
        rf.addRow("Password", self.pw1)
        rf.addRow("Again", self.pw2)

        host_row = QHBoxLayout()
        self.hostname = QLineEdit(str(settings.get("server_hostname") or ""))
        self.hostname.setPlaceholderText("Optional: your public address or domain name")
        self.find_btn = QPushButton("Find my public address")
        self.find_btn.setObjectName("secondaryBtn")
        self.find_btn.clicked.connect(self._find)
        self.find_btn.setVisible(find_public_address is not None)
        host_row.addWidget(self.hostname, 1)
        host_row.addWidget(self.find_btn)
        rf.addRow("Address", host_row)
        self.find_note = _hint("Shown on this PC so you know what to type elsewhere. "
                               "Find asks api.ipify.org once.")
        rf.addRow(self.find_note)

        self.cert = QLineEdit(str(settings.get("server_cert_path") or ""))
        self.key = QLineEdit(str(settings.get("server_key_path") or ""))
        self.cert.setPlaceholderText("Optional: certificate (.pem)")
        self.key.setPlaceholderText("Optional: private key (.pem)")
        rf.addRow("Certificate", self._with_browse(self.cert, "Choose certificate"))
        rf.addRow("Key", self._with_browse(self.key, "Choose private key"))
        rf.addRow(_hint(
            "Leave both empty and HuaEPUB makes its own certificate. Browsers warn about it "
            "the first time; check that the fingerprint matches the one shown on this PC."
        ))
        lay.addWidget(self.remote_box)

        self.error = QLabel("")
        self.error.setObjectName("flagLabel")
        self.error.setWordWrap(True)
        self.error.hide()
        lay.addWidget(self.error)

        btns = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.setObjectName("secondaryBtn")
        cancel.clicked.connect(self.reject)
        self.start_btn = QPushButton("Start serving")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._accept)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(self.start_btn)
        lay.addLayout(btns)

        if (settings.get("server_mode") or "lan") == "remote":
            self.remote_rb.setChecked(True)
        else:
            self.lan_rb.setChecked(True)
        self.lan_rb.toggled.connect(self._sync)
        self._sync()

    def _with_browse(self, edit: QLineEdit, caption: str) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        btn = QPushButton("Browse…")
        btn.setObjectName("secondaryBtn")
        btn.clicked.connect(lambda: self._browse(edit, caption))
        row.addWidget(btn)
        return box

    def _browse(self, edit: QLineEdit, caption: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, caption, edit.text() or "",
                                              "PEM files (*.pem *.crt *.key);;All files (*)")
        if path:
            edit.setText(path)

    def _sync(self) -> None:
        self.remote_box.setVisible(self.remote_rb.isChecked())
        self.adjustSize()

    def _find(self) -> None:
        if self._find_public is None:
            return
        self.find_btn.setEnabled(False)
        self.find_note.setText("Asking api.ipify.org…")
        finder = self._find_public

        def run():
            try:
                self._sig_public.emit(finder(), "")
            except Exception as exc:
                self._sig_public.emit("", str(exc) or "No answer")

        threading.Thread(target=run, name="public-address", daemon=True).start()

    def _on_public(self, address: str, error: str) -> None:
        self.find_btn.setEnabled(True)
        if address:
            self.hostname.setText(address)
            self.find_note.setText("That is your network's public address. It can change "
                                   "unless your provider gives you a fixed one.")
        else:
            self.find_note.setText(f"Could not find it: {error[:120]}")

    def _fail(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()

    def _accept(self) -> None:
        mode = "remote" if self.remote_rb.isChecked() else "lan"
        password = ""
        cert = self.cert.text().strip()
        key = self.key.text().strip()
        if mode == "remote":
            p1, p2 = self.pw1.text(), self.pw2.text()
            if p1 or p2 or not self._has_password:
                if len(p1) < MIN_PASSWORD:
                    self._fail(f"The password needs at least {MIN_PASSWORD} characters.")
                    return
                if p1 != p2:
                    self._fail("The two passwords are not the same.")
                    return
                password = p1
            if bool(cert) != bool(key):
                self._fail("Pick both the certificate and its key, or leave both empty.")
                return
        self.choice = ServerChoice(mode=mode, port=int(self.port.value()),
                                   hostname=self.hostname.text().strip(),
                                   cert_path=cert, key_path=key, password=password)
        self.accept()


class ChangePasswordDialog(QDialog):
    """Change the remote-mode password while serving."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Change password")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.password = ""
        lay = QVBoxLayout(self)
        lay.addWidget(_hint("Devices signed in with the old password are signed out."))
        form = QFormLayout()
        self.pw1 = QLineEdit()
        self.pw1.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("New password", self.pw1)
        form.addRow("Again", self.pw2)
        lay.addLayout(form)
        self.error = QLabel("")
        self.error.setObjectName("flagLabel")
        self.error.hide()
        lay.addWidget(self.error)
        btns = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.setObjectName("secondaryBtn")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Change password")
        ok.setDefault(True)
        ok.clicked.connect(self._accept)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)

    def _accept(self) -> None:
        p1, p2 = self.pw1.text(), self.pw2.text()
        if len(p1) < MIN_PASSWORD:
            self.error.setText(f"The password needs at least {MIN_PASSWORD} characters.")
            self.error.show()
            return
        if p1 != p2:
            self.error.setText("The two passwords are not the same.")
            self.error.show()
            return
        self.password = p1
        self.accept()
