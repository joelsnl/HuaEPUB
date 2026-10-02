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
