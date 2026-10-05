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


def test_close_while_syncing_parses_epub_progress(qapp):
    from gui.dialogs import CloseWhileSyncingDialog

    dlg = CloseWhileSyncingDialog(None, "Uploading EPUB 2/10: foo.epub")
    assert dlg.bar.value() == 2
    assert dlg.bar.maximum() == 10
    dlg.set_status("Syncing library.json in “HuaEPUB”…")
    assert dlg.bar.minimum() == 0
    assert dlg.bar.maximum() == 0
    dlg._wait_then_close()
    assert dlg.choice == CloseWhileSyncingDialog.WAIT
    assert dlg.wait_btn.isEnabled() is False
    dlg.mark_finished("Synced “HuaEPUB”: library (3 novel(s))", "")
    assert dlg._done
    assert dlg.bar.value() == dlg.bar.maximum()
    qapp.processEvents()
    assert dlg.choice == CloseWhileSyncingDialog.WAIT


def test_close_while_syncing_keep_open_and_abort(qapp):
    from gui.dialogs import CloseWhileSyncingDialog

    stay = CloseWhileSyncingDialog(None, "Syncing…")
    stay._keep_open()
    assert stay.choice == CloseWhileSyncingDialog.STAY

    abort = CloseWhileSyncingDialog(None, "Syncing…")
    abort._close_anyway()
    assert abort.choice == CloseWhileSyncingDialog.ABORT
    assert abort._accepting


def test_update_progress_stays_open_until_allowed(qapp):
    from PySide6.QtCore import Qt, QEvent
    from PySide6.QtGui import QKeyEvent
    from gui.dialogs import UpdateProgressDialog

    dlg = UpdateProgressDialog()
    dlg.show()
    qapp.processEvents()
    dlg.set_progress(40, 100, "Verifying download...")
    assert dlg.bar.value() == 40
    assert dlg.label.text() == "Verifying download..."
    dlg.close()
    qapp.processEvents()
    assert dlg.isVisible()
    dlg.keyPressEvent(
        QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier,
        )
    )
    assert dlg.isVisible()
    dlg.allow_close()
    dlg.close()
    qapp.processEvents()
    assert dlg.isVisible() is False


def test_update_progress_ready_state_offers_one_restart_button(qapp):
    from gui.dialogs import UpdateProgressDialog

    dlg = UpdateProgressDialog("9.9.9")
    dlg.show()
    qapp.processEvents()
    assert "9.9.9" in dlg.heading.text()
    assert dlg.restart_button.isVisible() is False
    clicked = []
    dlg.restart_requested.connect(lambda: clicked.append(True))
    dlg.show_ready("HuaEPUB will close and reopen to finish installing.")
    qapp.processEvents()
    assert dlg.restart_button.isVisible()
    assert dlg.restart_button.isDefault()
    assert dlg.bar.value() == 100
    dlg.close()
    qapp.processEvents()
    assert dlg.isVisible()
    dlg.restart_button.click()
    assert clicked == [True]
    dlg.allow_close()
    dlg.close()
