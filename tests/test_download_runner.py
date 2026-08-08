"""Tests for core.download_runner helpers."""

from pathlib import Path

from core.download_runner import DownloadControl, DownloadCancelled, epub_path, downloads_folder
from core.parser import Chapter


def test_epub_path_preferred_strips_copy_suffix(tmp_path):
    p = epub_path(tmp_path, "Title", preferred_name="Book (1).epub")
    assert Path(p).name == "Book.epub"


def test_downloads_folder_custom(tmp_path):
    d = tmp_path / "out"
    assert downloads_folder(str(d)) == d
    assert d.is_dir()


def test_pause_and_cancel():
    ctrl = DownloadControl()
    ctrl.is_paused = True
    ctrl.cancel_requested = True
    try:
        ctrl.wait_while_paused()
        assert False, "expected cancel"
    except DownloadCancelled:
        pass
