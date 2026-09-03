import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Make the repo root importable (core/, parsers/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES = Path(__file__).parent / 'fixtures'


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


@pytest.fixture(scope="module")
def qapp():
    # PySide6 is installed, but importing QtGui still needs OS GL/EGL libs.
    # Headless Linux CI without those packages must skip, not fail collection.
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        pytest.skip(f"Qt GUI unavailable: {exc}")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app
