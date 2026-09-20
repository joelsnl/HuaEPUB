# Author: joelsnl and Anthropic Claude
"""Window, taskbar, and status-row icon for HuaEPUB."""

from __future__ import annotations

import sys
from pathlib import Path

# Must be set before QApplication on Windows or the taskbar stays python.exe.
WINDOWS_APP_ID = "joelsnl.HuaEPUB"

_PNG_NAME = "huaepub.png"
_ICO_NAME = "huaepub.ico"


def apply_windows_app_id() -> None:
    """Pin this process to HuaEPUB so the taskbar does not use the Python glyph."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except Exception:
        pass


def asset_dir() -> Path:
    """Folder that holds huaepub.png / huaepub.ico (dev tree or PyInstaller)."""
    here = Path(__file__).resolve().parent / "assets"
    if (here / _PNG_NAME).is_file() or (here / _ICO_NAME).is_file():
        return here
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", sys.executable))
        for candidate in (
            meipass / "gui" / "assets",
            Path(sys.executable).parent / "gui" / "assets",
        ):
            if (candidate / _PNG_NAME).is_file() or (candidate / _ICO_NAME).is_file():
                return candidate
    return here


def icon_path() -> Path | None:
    """Prefer the multi-size .ico on Windows; PNG everywhere else."""
    folder = asset_dir()
    ico = folder / _ICO_NAME
    png = folder / _PNG_NAME
    if sys.platform == "win32" and ico.is_file():
        return ico
    if png.is_file():
        return png
    if ico.is_file():
        return ico
    return None


def load_app_icon():
    """QIcon with 16–256px sizes, or a null icon if the asset is missing."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon, QPixmap

    icon = QIcon()
    path = icon_path()
    if path is None:
        return icon
    if path.suffix.lower() == ".ico":
        icon.addFile(str(path))
        if not icon.isNull():
            return icon
        png = path.with_name(_PNG_NAME)
        if not png.is_file():
            return icon
        path = png
    pix = QPixmap(str(path))
    if pix.isNull():
        return icon
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(
            pix.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
    return icon


def apply_app_icon(app, window=None) -> None:
    """Set the application (and optional window) icon. No-op if assets are missing."""
    icon = load_app_icon()
    if icon.isNull():
        return
    if app is not None:
        app.setWindowIcon(icon)
    if window is not None:
        window.setWindowIcon(icon)


def load_app_pixmap(size: int):
    """Square pixmap for in-window chrome, or a null pixmap if missing."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap

    png = asset_dir() / _PNG_NAME
    path = png if png.is_file() else icon_path()
    if path is None:
        return QPixmap()
    pix = QPixmap(str(path))
    if pix.isNull():
        return pix
    return pix.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
