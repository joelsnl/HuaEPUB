"""Library tab multi-select (Select All / Invert / batch action payloads)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from core.library import LibraryStore
    from gui.pages.library_page import LibraryPage
except ImportError as exc:
    pytest.skip(f"Qt GUI unavailable: {exc}", allow_module_level=True)


class _FakeCache:
    def get_cover(self, **kwargs):
        return None


def _session(tmp_path):
    store = LibraryStore(tmp_path / "library.json")
    for i, title in enumerate(("Alpha", "Beta", "Gamma"), 1):
        store.upsert_library(
            source_url=f"http://example.test/book{i}",
            title=title,
            chapter_count=i,
        )

    class Session:
        settings = {
            "library_view": "list",
            "library_filter": "all",
            "drive_sync_enabled": False,
            "drive_sync_library": True,
            "drive_sync_epubs": True,
        }
        library_store = store
        cache = _FakeCache()

    return Session()


@pytest.fixture
def page(qapp, tmp_path):
    widget = LibraryPage(_session(tmp_path))
    widget.refresh()
    qapp.processEvents()
    yield widget
    try:
        widget.deleteLater()
    except RuntimeError:
        pass
    qapp.processEvents()


def test_select_all_and_none(page, qapp):
    expected = [e.source_url for e in page.session.library_store.get_library()]
    page.select_all()
    qapp.processEvents()
    assert page.selected_urls() == expected
    assert page.update_btn.text() == "Update (3)"
    assert page.remove_btn.text() == "Remove (3)"
    page.select_none()
    qapp.processEvents()
    assert page.selected_urls() == []
    assert page.update_btn.text() == "Update"
    assert not page.update_btn.isEnabled()


def test_invert_selection(page, qapp):
    page.select_all()
    qapp.processEvents()
    page.invert_selection()
    qapp.processEvents()
    assert page.selected_urls() == []
    page.invert_selection()
    qapp.processEvents()
    assert len(page.selected_urls()) == 3


def test_refresh_keeps_multi_select(page, qapp):
    page.select_all()
    qapp.processEvents()
    page.refresh()
    qapp.processEvents()
    assert len(page.selected_urls()) == 3


def test_view_switch_keeps_selection(page, qapp):
    page.select_all()
    qapp.processEvents()
    page._set_view("grid")
    qapp.processEvents()
    assert len(page.selected_urls()) == 3
    page._set_view("list")
    qapp.processEvents()
    assert len(page.selected_urls()) == 3


def test_update_and_remove_emit_batch_payloads(page, qapp):
    updates = []
    removed = []
    page.update_selected.connect(updates.append)
    page.remove_selected.connect(removed.append)
    page.select_all()
    qapp.processEvents()
    page._emit_update()
    page._emit_remove()
    expected = [e.source_url for e in page.session.library_store.get_library()]
    assert len(updates) == 1
    assert isinstance(updates[0], list)
    assert [e.source_url for e in updates[0]] == expected
    assert removed == [expected]


def test_single_update_emits_entry_not_list(page, qapp):
    updates = []
    page.update_selected.connect(updates.append)
    page.table.selectRow(0)
    qapp.processEvents()
    page._emit_update()
    assert len(updates) == 1
    assert not isinstance(updates[0], list)
    first = page.session.library_store.get_library()[0]
    assert updates[0].source_url == first.source_url
