"""Main window offscreen: tabs, fetch click, chapter selection, reader font."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QPushButton
    from core.parser import Chapter, NovelInfo
    from gui.main_window import MainWindow
except ImportError as exc:
    pytest.skip(f"Qt GUI unavailable: {exc}", allow_module_level=True)


def _button(widget, text: str) -> QPushButton:
    for button in widget.findChildren(QPushButton):
        if button.text() == text:
            return button
    raise AssertionError(text)


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("core.settings.Path.home", lambda: tmp_path)
    monkeypatch.setattr("gui.main_window.QTimer.singleShot", lambda *_a, **_k: None)
    monkeypatch.setattr("gui.main_window.setup_logging", lambda *_a, **_k: None)
    win = MainWindow()
    win._clipboard_timer.stop()
    win.show()
    qapp.processEvents()
    yield win
    try:
        win._clipboard_timer.stop()
    except Exception:
        pass
    try:
        http = getattr(win, "_http", None)
        if http is not None:
            http.close()
    except Exception:
        pass
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_window_opens_on_four_tabs(window, qapp):
    assert window.windowTitle().startswith("HuaEPUB")
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        "Single",
        "Multi",
        "Library",
        "Read",
    ]
    window.tabs.setCurrentWidget(window.library)
    qapp.processEvents()
    assert window.tabs.currentWidget() is window.library
    window.tabs.setCurrentWidget(window.reader)
    qapp.processEvents()
    assert "Open a novel" in window.reader.title_lbl.text()


def test_fetch_click_sends_the_url(window, qapp):
    sent = []
    window.single.fetch_requested.connect(sent.append)
    window.tabs.setCurrentWidget(window.single)
    _button(window.single, "Fetch Chapters").click()
    qapp.processEvents()
    assert sent == []
    window.single.url_edit.setText("https://example.test/book")
    _button(window.single, "Fetch Chapters").click()
    qapp.processEvents()
    assert sent == ["https://example.test/book"]


def test_select_none_clears_chapters(window, qapp):
    window.single.show_novel(
        NovelInfo(title="Tale"),
        [
            Chapter(title="One", url="https://example.test/1"),
            Chapter(title="Two", url="https://example.test/2"),
        ],
        None,
    )
    qapp.processEvents()
    assert window.single.selected_label.text() == "Selected: 2"
    assert window.single.read_btn.isEnabled()
    _button(window.single, "Select None").click()
    qapp.processEvents()
    assert window.single.selected_label.text() == "Selected: 0"


def test_reader_font_button_and_translate_checkbox(window, qapp):
    sizes = []
    window.reader.font_changed.connect(sizes.append)
    window.tabs.setCurrentWidget(window.reader)
    _button(window.reader, "A+").click()
    qapp.processEvents()
    assert sizes == [19]
    assert window.reader.font_slider.value() == 19

    window.options.translate_cb.click()
    qapp.processEvents()
    assert window.options.translate_cb.isChecked() is False


def test_silent_drive_sync_waits_until_update_is_declined(window, monkeypatch):
    window.library.drive_enabled.setChecked(True)
    bound = []
    monkeypatch.setattr(
        window, "_bind_and_run", lambda *a, **k: bound.append(True) or True
    )
    window._app_update_checking = True
    window._start_drive_sync(silent=True)
    assert bound == []
    assert window._pending_drive_sync is True

    window._app_update_checking = False
    window._app_update_pending = True
    window._start_drive_sync(silent=True)
    assert bound == []

    window._app_update_pending = False
    window._release_deferred_drive_sync()
    assert bound == [True]

    window._app_update_installing = True
    window._pending_drive_sync = True
    window._start_drive_sync(silent=True)
    assert bound == [True]
    assert window._pending_drive_sync is False


def test_accepting_update_hides_the_app_until_it_fails(window, qapp, monkeypatch):
    from PySide6.QtCore import Qt

    captured = {}

    def fake_download(progress_callback=None, completion_callback=None):
        captured["progress"] = progress_callback
        captured["done"] = completion_callback

    monkeypatch.setattr("gui.main_window.download_update_async", fake_download)
    monkeypatch.setattr("gui.main_window.ask_yes_no", lambda *_a, **_k: True)
    warnings = []
    monkeypatch.setattr(
        "gui.main_window.show_warning", lambda *a, **k: warnings.append(a)
    )
    window._pending_drive_sync = True
    try:
        window._on_update_check_ready(True, "9.9.9", "A new version is available.")
        qapp.processEvents()
        assert window._app_update_installing
        assert window.isVisible() is False
        assert window._pending_drive_sync is False
        dlg = window._update_progress_dlg
        assert dlg is not None and dlg.isVisible()
        assert dlg.windowModality() == Qt.WindowModality.ApplicationModal
        captured["progress"](20, 100, "Downloading HuaEPUB-windows.zip...")
        qapp.processEvents()
        assert dlg.bar.value() == 20
        assert "Downloading" in dlg.label.text()
        window.close()
        qapp.processEvents()
        assert window._app_update_installing
        captured["done"](False, "checksum mismatch")
        qapp.processEvents()
        assert window.isVisible()
        assert window._app_update_installing is False
        assert window._update_progress_dlg is None
        assert warnings
    finally:
        window._app_update_installing = False
        window._close_update_progress()


def test_declining_update_runs_the_deferred_drive_sync(window, monkeypatch):
    started = []
    monkeypatch.setattr("gui.main_window.ask_yes_no", lambda *_a, **_k: False)
    monkeypatch.setattr(
        window, "_start_drive_sync", lambda silent=True: started.append(silent)
    )
    window._pending_drive_sync = True
    window._app_update_checking = True
    window._on_update_check_ready(True, "9.9.9", "A new version is available.")
    assert started == [True]
    assert window._app_update_pending is False
    assert window._app_update_installing is False


def test_successful_update_closes_progress_and_exits(window, qapp, monkeypatch):
    import gui.main_window as mw

    captured = {}

    def fake_download(progress_callback=None, completion_callback=None):
        captured["done"] = completion_callback

    class FakeApp:
        quit_calls = 0

        def quit(self):
            FakeApp.quit_calls += 1

    class FakeThread:
        started = []

        def __init__(self, target=None, daemon=None, **_k):
            self.target = target

        def start(self):
            FakeThread.started.append(self.target)

    infos = []
    closes = []
    monkeypatch.setattr(mw, "download_update_async", fake_download)
    monkeypatch.setattr(mw, "ask_yes_no", lambda *_a, **_k: True)
    monkeypatch.setattr(mw, "show_info", lambda *a, **k: infos.append(a))
    monkeypatch.setattr(mw.QApplication, "instance", lambda: FakeApp())
    monkeypatch.setattr(mw.threading, "Thread", FakeThread)
    monkeypatch.setattr(window, "close", lambda: closes.append(True))
    try:
        window._on_update_check_ready(True, "9.9.9", "A new version is available.")
        qapp.processEvents()
        dlg = window._update_progress_dlg
        assert dlg is not None
        captured["done"](True, "Update installed.")
        qapp.processEvents()
        assert infos and infos[0][1] == "Update ready"
        assert window._update_progress_dlg is None
        assert dlg.isVisible() is False
        assert window._exiting_for_update is True
        assert closes == [True]
        assert FakeApp.quit_calls == 1
        assert len(FakeThread.started) == 1
    finally:
        window._exiting_for_update = False
        window._app_update_installing = False
        window._close_update_progress()
