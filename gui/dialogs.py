# Author: joelsnl and Anthropic Claude
"""
QMessageBox helpers with letter-key shortcuts.

Fusion + QSS draws mnemonic underlines on &Yes / &No, but those are Alt+Y /
Alt+N. Users expect Y and N the way a native Windows MessageBox works.
These helpers bind the first letter of each standard (and custom) button.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QAbstractButton, QMessageBox

_STANDARD_LETTERS = (
    (QMessageBox.StandardButton.Yes, "Y"),
    (QMessageBox.StandardButton.No, "N"),
    (QMessageBox.StandardButton.Ok, "O"),
    (QMessageBox.StandardButton.Cancel, "C"),
    (QMessageBox.StandardButton.Retry, "R"),
    (QMessageBox.StandardButton.Ignore, "I"),
    (QMessageBox.StandardButton.Abort, "A"),
    (QMessageBox.StandardButton.Close, "C"),
)


def _letter_key(letter: str) -> Qt.Key | None:
    ch = (letter or "").strip().upper()[:1]
    if not ch.isalpha():
        return None
    return getattr(Qt.Key, f"Key_{ch}", None)


def bind_letter_shortcuts(box: QMessageBox) -> QMessageBox:
    """Bind Y/N/O/… on an already-configured QMessageBox. Safe to call twice."""
    claimed: set[str] = set()
    bound_buttons: set[int] = set()

    def add(btn: QAbstractButton | None, letter: str) -> None:
        if btn is None:
            return
        key = _letter_key(letter)
        if key is None:
            return
        mark = letter.upper()[:1]
        if mark in claimed:
            return
        claimed.add(mark)
        bound_buttons.add(id(btn))
        shortcut = QShortcut(QKeySequence(key), box)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.activated.connect(btn.click)

    for std, letter in _STANDARD_LETTERS:
        add(box.button(std), letter)

    for btn in box.buttons():
        if id(btn) in bound_buttons:
            continue
        label = (btn.text() or "").replace("&", "").strip()
        if label:
            add(btn, label[0])
    return box


def exec_box(box: QMessageBox) -> int:
    """Bind letter shortcuts, then run the modal loop."""
    bind_letter_shortcuts(box)
    return box.exec()


def _standard_box(
    parent,
    icon: QMessageBox.Icon,
    title: str,
    text: str,
    buttons: QMessageBox.StandardButton,
    default: QMessageBox.StandardButton | None = None,
) -> QMessageBox:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(buttons)
    if default is not None:
        box.setDefaultButton(default)
    bind_letter_shortcuts(box)
    return box


def ask_yes_no(
    parent,
    title: str,
    text: str,
    *,
    default_yes: bool = True,
) -> bool:
    """Yes/No question. Y and N both work (not only Alt+Y / Alt+N)."""
    default = (
        QMessageBox.StandardButton.Yes
        if default_yes
        else QMessageBox.StandardButton.No
    )
    box = _standard_box(
        parent,
        QMessageBox.Icon.Question,
        title,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        default,
    )
    return box.exec() == QMessageBox.StandardButton.Yes


def show_info(parent, title: str, text: str) -> None:
    _standard_box(
        parent,
        QMessageBox.Icon.Information,
        title,
        text,
        QMessageBox.StandardButton.Ok,
        QMessageBox.StandardButton.Ok,
    ).exec()


def show_warning(parent, title: str, text: str) -> None:
    _standard_box(
        parent,
        QMessageBox.Icon.Warning,
        title,
        text,
        QMessageBox.StandardButton.Ok,
        QMessageBox.StandardButton.Ok,
    ).exec()


def show_error(parent, title: str, text: str) -> None:
    _standard_box(
        parent,
        QMessageBox.Icon.Critical,
        title,
        text,
        QMessageBox.StandardButton.Ok,
        QMessageBox.StandardButton.Ok,
    ).exec()
