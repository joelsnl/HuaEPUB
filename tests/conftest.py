import gc
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Make the repo root importable (core/, parsers/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES = Path(__file__).parent / 'fixtures'

# Keep QApplication alive until after thread/session cleanup. Interpreter
# shutdown otherwise destroys Qt while leftover QThreads / curl_cffi handles
# are still live (Linux/macOS exit 139 after "N passed").
_QAPP = None


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


def _close_obj(obj) -> None:
    closer = getattr(obj, "close", None)
    if callable(closer):
        closer()


def _close_http_sessions() -> None:
    """Close impersonated curl_cffi sessions before libcurl unloads."""
    try:
        from core import epub_builder
    except Exception:
        epub_builder = None
    if epub_builder is not None:
        sess = getattr(epub_builder, "_http_session", None)
        if sess is not None:
            try:
                _close_obj(sess)
            except Exception:
                pass
            try:
                epub_builder._http_session = None
            except Exception:
                pass

    session_types = []
    try:
        from curl_cffi.requests import Session as CurlSession

        session_types.append(CurlSession)
    except Exception:
        pass
    try:
        import requests

        session_types.append(requests.Session)
    except Exception:
        pass
    if not session_types:
        return
    gc.collect()
    for obj in gc.get_objects():
        try:
            if isinstance(obj, tuple(session_types)):
                _close_obj(obj)
        except Exception:
            continue


def _quit_lingering_qthreads() -> None:
    try:
        from PySide6.QtCore import QCoreApplication, QThread
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance() or QCoreApplication.instance()
    if app is None:
        return
    try:
        app.processEvents()
    except Exception:
        pass
    try:
        closer = getattr(app, "closeAllWindows", None)
        if callable(closer):
            closer()
        app.processEvents()
    except Exception:
        pass

    main = None
    try:
        main = app.thread()
    except Exception:
        pass

    threads = []

    def _remember(th) -> None:
        try:
            if main is not None and (th is main or th == main):
                return
            if th.isRunning() and th not in threads:
                threads.append(th)
        except Exception:
            return

    try:
        for child in app.findChildren(QThread):
            _remember(child)
    except Exception:
        pass

    gc.collect()
    for obj in gc.get_objects():
        try:
            if isinstance(obj, QThread):
                _remember(obj)
        except Exception:
            continue

    for th in threads:
        try:
            th.quit()
        except Exception:
            pass
    for th in threads:
        try:
            if th.isRunning():
                th.wait(2000)
        except Exception:
            pass
    try:
        app.processEvents()
    except Exception:
        pass


def _drain_pytest_runtime() -> None:
    _quit_lingering_qthreads()
    _close_http_sessions()
    _quit_lingering_qthreads()
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            for _ in range(10):
                app.processEvents()
    except Exception:
        pass


def pytest_sessionfinish(session, exitstatus):
    _drain_pytest_runtime()


def pytest_unconfigure(config):
    _drain_pytest_runtime()


@pytest.fixture(scope="session")
def qapp():
    # PySide6 is installed, but importing QtGui still needs OS GL/EGL libs.
    # Headless Linux CI without those packages must skip, not fail collection.
    global _QAPP
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        pytest.skip(f"Qt GUI unavailable: {exc}")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _QAPP = app
    yield app
    _drain_pytest_runtime()
