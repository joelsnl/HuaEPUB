"""HuaEPUB window / taskbar icon assets load and are non-empty."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui.icon import (
    apply_windows_app_id,
    asset_dir,
    icon_path,
    load_app_icon,
    load_app_pixmap,
)


def test_icon_files_exist():
    folder = asset_dir()
    assert (folder / "huaepub.png").is_file()
    assert (folder / "huaepub.ico").is_file()
    path = icon_path()
    assert path is not None
    assert path.is_file()
    assert path.stat().st_size > 1000


def test_apply_windows_app_id_does_not_raise():
    apply_windows_app_id()


def test_load_app_icon_not_null(qapp):
    icon = load_app_icon()
    assert not icon.isNull()
    pix = load_app_pixmap(32)
    assert not pix.isNull()
    assert pix.width() == 32
    assert pix.height() == 32


def test_progress_panel_shows_logo(qapp):
    from gui.widgets.progress_panel import ProgressPanel

    panel = ProgressPanel()
    assert not panel.logo.isHidden()
    assert panel.logo.pixmap() is not None
    assert not panel.logo.pixmap().isNull()
