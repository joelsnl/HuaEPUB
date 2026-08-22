"""Letter shortcuts on Yes/No dialogs (underlines are not only Alt+Y / Alt+N)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QApplication, QMessageBox

from gui.dialogs import bind_letter_shortcuts


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _shortcut_keys(box: QMessageBox) -> set[str]:
    return {
        sc.key().toString().upper()
        for sc in box.findChildren(QShortcut)
        if sc.key().toString()
    }


def test_yes_no_binds_y_and_n(qapp):
    box = QMessageBox()
    box.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    bind_letter_shortcuts(box)
    keys = _shortcut_keys(box)
    assert "Y" in keys
    assert "N" in keys


def test_ok_binds_o(qapp):
    box = QMessageBox()
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    bind_letter_shortcuts(box)
    assert "O" in _shortcut_keys(box)
