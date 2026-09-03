"""Y/N shortcuts on Yes/No dialogs only (underlines are not only Alt+Y / Alt+N)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# PySide6 is installed, but importing QtGui still needs OS GL/EGL libs.
# Headless Linux CI without those packages must skip, not fail collection.
try:
    from PySide6.QtGui import QShortcut
    from PySide6.QtWidgets import QMessageBox
    from gui.dialogs import bind_letter_shortcuts, pick_item
except ImportError as exc:
    pytest.skip(f"Qt GUI unavailable: {exc}", allow_module_level=True)


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


def test_ok_does_not_bind_o(qapp):
    box = QMessageBox()
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    bind_letter_shortcuts(box)
    assert "O" not in _shortcut_keys(box)


def test_preview_button_does_not_bind_p(qapp):
    box = QMessageBox()
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.addButton("Preview", QMessageBox.ButtonRole.ActionRole)
    bind_letter_shortcuts(box)
    keys = _shortcut_keys(box)
    assert "O" not in keys
    assert "P" not in keys


def test_accept_all_does_not_bind_letter_keys(qapp):
    box = QMessageBox()
    box.addButton("Accept all", QMessageBox.ButtonRole.YesRole)
    box.addButton("Discard", QMessageBox.ButtonRole.NoRole)
    bind_letter_shortcuts(box)
    keys = _shortcut_keys(box)
    assert "A" not in keys
    assert "D" not in keys


def test_custom_yes_not_now_binds_y_and_n_not_d(qapp):
    box = QMessageBox()
    box.addButton("Yes", QMessageBox.ButtonRole.YesRole)
    box.addButton("Not now", QMessageBox.ButtonRole.NoRole)
    box.addButton("Don't ask", QMessageBox.ButtonRole.ActionRole)
    bind_letter_shortcuts(box)
    keys = _shortcut_keys(box)
    assert "Y" in keys
    assert "N" in keys
    assert "D" not in keys


def test_pick_item_single_skips_dialog(qapp):
    assert pick_item(None, "Preview", "Which novel?", ["Only book"]) == 0
    assert pick_item(None, "Preview", "Which novel?", []) is None
