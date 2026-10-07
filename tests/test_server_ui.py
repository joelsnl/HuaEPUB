"""Desktop side of server mode and the Slips look (offscreen)."""

from __future__ import annotations

import os
import socket

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from gui import theme
    from gui.main_window import MainWindow
    from gui.widgets.server_screen import ServerScreen, format_code, task_line
    from gui.widgets.slips import DONE, FETCHED, WAITING, slip_states
except ImportError as exc:
    pytest.skip(f"Qt GUI unavailable: {exc}", allow_module_level=True)

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ----------------------------------------------------------------------
# Look
# ----------------------------------------------------------------------


def test_every_palette_renders_a_complete_stylesheet(qapp):
    for palette in theme.PALETTES.values():
        qss = theme.render_qss(palette)
        assert "${" not in qss, palette.id
        assert palette.accent in qss and palette.ground in qss


def test_auto_follows_the_system_and_named_looks_are_fixed(qapp):
    assert theme.resolve("auto", system_dark=True).id == "graphite"
    assert theme.resolve("auto", system_dark=False).id == "celadon"
    assert theme.resolve("light").id == "celadon"
    assert theme.resolve("gold").id == "gold"
    assert theme.resolve("not-a-look", system_dark=True).id == "graphite"
    assert theme.resolve("random").id in theme.PALETTES


def test_web_and_desktop_share_the_same_palettes():
    from pathlib import Path

    css = (Path(__file__).resolve().parents[1] / "web" / "static" / "app.css").read_text(
        encoding="utf-8")
    for palette in theme.PALETTES.values():
        for colour in (palette.ground, palette.ink, palette.accent, palette.flag):
            assert colour.lower() in css.lower(), (palette.id, colour)


def test_slip_states():
    assert slip_states(4, 0.5, "fetching") == [FETCHED, FETCHED, WAITING, WAITING]
    assert slip_states(4, 0.5, "translating") == [DONE, DONE, FETCHED, FETCHED]
    assert slip_states(3, 0.1, "writing", finished=True) == [DONE, DONE, DONE]
    assert slip_states(0, 1.0, "fetching") == []


def test_progress_panel_marks_a_translated_finish(qapp):
    from core.utils import pipeline_phase
    from gui.widgets.progress_panel import ProgressPanel

    panel = ProgressPanel()
    panel.set_progress(0.3, "Fetching chapters 3/10")
    assert panel._phase == "fetching"
    panel.mark_finished(translated=True)
    assert not panel.seal.isHidden()
    panel.set_progress(0.0, "Starting download…")
    assert panel.seal.isHidden()
    assert pipeline_phase("Writing EPUB…") == "writing"
    assert pipeline_phase("Polishing English 3/9") == "polishing"
    assert pipeline_phase("Translating · Google · 10/40") == "translating"


# ----------------------------------------------------------------------
# Server screen
# ----------------------------------------------------------------------


def test_code_and_task_lines():
    assert format_code("abcd2345") == "abcd 2345"
    assert "Nothing running" in task_line(None)
    line = task_line({"state": "running", "label": "Sword", "phase_label": "Fetching chapters",
                      "message": "Fetching chapter 3/40\nmore"})
    assert line.startswith("Fetching chapters · Sword") and "3/40" in line and "more" not in line
    assert task_line({"state": "paused", "label": "Sword"}) == "Paused · Sword"


def test_server_screen_shows_code_or_password(qapp):
    from web.host import ServerStatus

    screen = ServerScreen()
    assert "status area" in screen.minimize_note.text()
    assert not screen.minimize_note.isHidden()
    lan = ServerStatus(running=True, mode="lan", port=8765, lan_ip="192.168.1.20")
    screen.show_status(lan, code="abcd2345", qr_text="http://192.168.1.20:8765/?code=abcd2345")
    assert screen.code.text() == "abcd 2345"
    assert "192.168.1.20:8765" in screen.addresses.text()
    assert not screen.new_code_btn.isHidden() and screen.password_btn.isHidden()

    remote = ServerStatus(running=True, mode="remote", port=8765, https=True,
                          hostname="example.duckdns.org", fingerprint="AA:BB")
    screen.show_status(remote, code="abcd2345", qr_text="https://example.duckdns.org:8765/")
    assert "abcd" not in screen.code.text()
    assert "AA:BB" in screen.fingerprint.text()
    assert "example.duckdns.org" in screen.addresses.text()
    assert screen.new_code_btn.isHidden() and not screen.password_btn.isHidden()


# ----------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("core.settings.Path.home", lambda: tmp_path)
    monkeypatch.setattr("gui.main_window.QTimer.singleShot", lambda *_a, **_k: None)
    monkeypatch.setattr("gui.window.server_actions.QTimer.singleShot", lambda *_a, **_k: None)
    monkeypatch.setattr("gui.main_window.setup_logging", lambda *_a, **_k: None)
    errors = []
    monkeypatch.setattr("gui.window.server_actions.show_error",
                        lambda _p, title, text: errors.append(text))
    win = MainWindow()
    win._clipboard_timer.stop()
    win.errors = errors
    win.show()
    qapp.processEvents()
    yield win
    try:
        if win._serving():
            win._stop_serving(remember_off=False)
    except Exception:
        pass
    win._force_close = True
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_serve_chip_sits_on_the_tab_bar(window):
    assert window.tabs.cornerWidget() is window.serve_chip
    assert window.serve_chip.text() == "SERVE"


def test_serving_swaps_the_tabs_for_the_server_screen_and_remembers(window, qapp):
    s = window.session.settings
    s["server_mode"] = "lan"
    s["server_port"] = _free_port()
    assert window._start_serving(announce_errors=False)
    qapp.processEvents()
    assert window._server_stack.currentWidget() is window.server_screen
    assert s["server_enabled"] is True
    assert all(not a.isEnabled() for a in window._desktop_only_actions())
    assert window.windowTitle().endswith("Serving")
    window._stop_serving(remember_off=True)
    assert window._server_stack.currentIndex() == 0
    assert s["server_enabled"] is False
    assert all(a.isEnabled() for a in window._desktop_only_actions())


def test_settings_changed_in_the_browser_show_after_stopping(window):
    s = window.session.settings
    s["server_port"] = _free_port()
    assert window._start_serving(announce_errors=False)
    s["translate"] = not window.options.translate_cb.isChecked()
    s["workers"] = 77
    window._stop_serving(remember_off=True)
    assert window.options.translate_cb.isChecked() == s["translate"]
    assert window.options.workers.value() == 77


def test_minimize_while_serving_hides_in_the_status_area(window, qapp, monkeypatch):
    pending = []
    monkeypatch.setattr(
        "gui.window.server_actions.QTimer.singleShot",
        lambda _ms, fn: pending.append(fn),
    )
    monkeypatch.setattr(
        "gui.window.server_actions.QSystemTrayIcon.isSystemTrayAvailable",
        staticmethod(lambda: True),
    )
    window.session.settings["server_port"] = _free_port()
    assert window._start_serving(announce_errors=False)

    def minimize():
        pending.clear()
        window.showMinimized()
        qapp.processEvents()
        assert window.isMinimized()
        assert pending, "minimize did not ask to hide"
        pending[-1]()

    minimize()
    assert not window.isVisible()
    assert window._status_tray is not None and window._status_tray.isVisible()
    assert window._serving()
    window._restore_from_status()
    qapp.processEvents()
    assert window.isVisible()
    assert not window.isMinimized()
    assert not window._status_tray.isVisible()
    minimize()
    assert not window.isVisible()
    window._stop_serving(remember_off=True)
    assert window.isVisible()
    assert window._server_stack.currentIndex() == 0


def test_closing_while_serving_keeps_it_on_for_next_launch(window):
    s = window.session.settings
    s["server_port"] = _free_port()
    assert window._start_serving(announce_errors=False)
    window._shutdown_server_for_close()
    assert not window._serving()
    assert s["server_enabled"] is True


def test_launch_falls_back_to_the_desktop_when_the_port_is_taken(window):
    with socket.socket() as busy:
        busy.bind(("0.0.0.0", 0))
        busy.listen(1)
        s = window.session.settings
        s["server_enabled"] = True
        s["server_mode"] = "lan"
        s["server_port"] = busy.getsockname()[1]
        assert window._maybe_start_server_on_launch() is False
    assert window._server_stack.currentIndex() == 0
    assert s["server_enabled"] is False
    assert window.errors and "could not start again" in window.errors[0]


def test_remote_without_password_does_not_start(window):
    s = window.session.settings
    s["server_mode"] = "remote"
    s["server_port"] = _free_port()
    assert window._start_serving(announce_errors=True) is False
    assert window.errors and "password" in window.errors[-1].lower()


def test_look_menu_saves_the_choice(window):
    window._set_look("gold")
    assert window.session.settings["ui_look"] == "gold"
    assert theme.current().id == "gold"
    window._set_look("auto")
    assert theme.current().id in ("graphite", "celadon")
    assert QApplication.instance().styleSheet()
