"""Offline tests for Drive sync helpers (no network)."""

import os
import time
from datetime import datetime, timezone

import pytest

from core.drive_sync import (
    DriveRevisionConflict,
    DriveSync,
    RemoteEpubInfo,
    local_epub_needs_push,
    oauth_client_path,
    oauth_setup_instructions,
    SCOPES,
)
from core.library import LibraryData, LibraryEntry
import core.settings as settings


class TestDriveSyncHelpers:
    def test_scopes_are_drive_file(self):
        assert any("drive.file" in s for s in SCOPES)
        assert not any("appdata" in s for s in SCOPES)

    def test_setup_instructions_mention_path(self):
        text = oauth_setup_instructions()
        assert "google_oauth_client.json" in text
        assert str(oauth_client_path()) in text

    def test_not_connected_by_default(self):
        sync = DriveSync()
        assert sync.is_connected() is False

    def test_location_description(self, monkeypatch):
        monkeypatch.setattr(
            "core.drive_sync.get_setting",
            lambda key, *a, **k: "HuaEPUB" if key == "drive_folder_name" else "",
        )
        sync = DriveSync()
        assert "HuaEPUB" in sync.location_description()

    def test_parse_folder_id(self):
        assert DriveSync.parse_folder_id(
            "https://drive.google.com/drive/folders/1AbCDefGhIJ_klmn0123456789"
        ) == "1AbCDefGhIJ_klmn0123456789"
        assert DriveSync.parse_folder_id("1AbCDefGhIJ_klmn0123456789") == "1AbCDefGhIJ_klmn0123456789"
        assert DriveSync.parse_folder_id("not a folder") == ""

    def test_pick_best_sync_folder_prefers_library(self, monkeypatch):
        sync = DriveSync()
        monkeypatch.setattr(
            sync,
            "_folder_has_library",
            lambda folder_id: folder_id == "with-lib",
        )
        picked = sync._pick_best_sync_folder(
            [
                {"id": "empty-a", "name": "HuaEPUB"},
                {"id": "with-lib", "name": "HuaEPUB"},
                {"id": "empty-b", "name": "HuaEPUB"},
            ]
        )
        assert picked == "with-lib"

    def test_escape_query_name(self):
        sync = DriveSync()
        assert "\\'" in sync._escape_query_name("O'Brien")


class TestLocalEpubNeedsPush:
    def test_missing_remote_uploads(self, tmp_path):
        local = tmp_path / "book.epub"
        local.write_bytes(b"epub-bytes")
        assert local_epub_needs_push(local, None) is True

    def test_size_change_uploads(self, tmp_path):
        local = tmp_path / "book.epub"
        local.write_bytes(b"x" * 1000)
        remote = RemoteEpubInfo(
            id="abc",
            size=500,
            modified_time="2020-01-01T00:00:00.000Z",
        )
        assert local_epub_needs_push(local, remote) is True

    def test_same_size_same_age_skips(self, tmp_path):
        local = tmp_path / "book.epub"
        local.write_bytes(b"x" * 500)
        # Use UTC epoch matching the remote RFC3339 timestamp
        remote_ts = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        os.utime(local, (remote_ts, remote_ts))
        remote = RemoteEpubInfo(
            id="abc",
            size=500,
            modified_time="2024-06-01T12:00:00.000Z",
        )
        assert local_epub_needs_push(local, remote) is False

    def test_remote_newer_skips_even_if_size_differs(self, tmp_path):
        local = tmp_path / "book.epub"
        local.write_bytes(b"old")
        old = time.time() - 3600
        os.utime(local, (old, old))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        remote = RemoteEpubInfo(id="abc", size=9999, modified_time=now)
        assert local_epub_needs_push(local, remote) is False

    def test_missing_local_file(self, tmp_path):
        assert local_epub_needs_push(tmp_path / "missing.epub", None) is False


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeFiles:
    def __init__(self, revisions):
        self._revisions = list(revisions)
        self.updated = 0
        self.created = 0

    def _next_rev(self):
        if self._revisions:
            return self._revisions.pop(0)
        return "r-final"

    def get(self, fileId, fields="", supportsAllDrives=True):
        return _Call({"id": fileId, "headRevisionId": self._next_rev()})

    def update(self, fileId, media_body=None, fields="", supportsAllDrives=True):
        self.updated += 1
        return _Call({
            "id": fileId,
            "modifiedTime": "2026-01-01T00:00:00.000Z",
            "size": 12,
            "headRevisionId": "r-after-update",
        })

    def create(self, body=None, media_body=None, fields="", supportsAllDrives=True):
        self.created += 1
        return _Call({
            "id": "new-lib",
            "modifiedTime": "2026-01-01T00:00:00.000Z",
            "headRevisionId": "r-created",
        })

    def get_media(self, fileId):
        return _Call(b'{"library":[],"history":[],"removed":[]}')


class _FakeService:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


class _FakeStore:
    def __init__(self, data):
        self.data = data

    def get_data(self):
        return self.data

    def replace_data(self, data):
        self.data = data


def _sample_library() -> LibraryData:
    return LibraryData(
        library=[
            LibraryEntry(source_url="https://demo.test/book/1", title="Demo"),
        ]
    )


class TestDriveLibraryRevision:
    @pytest.fixture(autouse=True)
    def temp_settings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(settings, "get_app_dir", lambda: tmp_path / "install")
        (tmp_path / "install").mkdir(exist_ok=True)
        settings._migration_done = False
        return tmp_path

    def _sync(self, files, monkeypatch):
        sync = DriveSync()
        monkeypatch.setattr(sync, "_require_service", lambda: _FakeService(files))
        monkeypatch.setattr(sync, "ensure_folder_layout", lambda: ("root", "books"))
        monkeypatch.setattr(sync, "_find_library_file", lambda root: "lib-id")
        monkeypatch.setattr(sync, "list_remote_books", lambda: {})
        monkeypatch.setattr(sync, "purge_removed_epubs", lambda data: None)
        return sync

    def test_push_aborts_on_revision_mismatch(self, monkeypatch):
        files = _FakeFiles(["r-remote"])
        sync = self._sync(files, monkeypatch)
        settings.set_setting("drive_library_revision", "r-local")
        with pytest.raises(DriveRevisionConflict):
            sync.push_library(_sample_library())
        assert files.updated == 0

    def test_empty_stored_revision_allows_first_push(self, monkeypatch):
        files = _FakeFiles(["r-remote"])
        sync = self._sync(files, monkeypatch)
        settings.set_setting("drive_library_revision", "")
        sync.push_library(_sample_library())
        assert files.updated == 1
        assert settings.get_setting("drive_library_revision") == "r-after-update"

    def test_matching_revision_updates(self, monkeypatch):
        files = _FakeFiles(["r1"])
        sync = self._sync(files, monkeypatch)
        settings.set_setting("drive_library_revision", "r1")
        sync.push_library(_sample_library())
        assert files.updated == 1
        assert settings.get_setting("drive_library_revision") == "r-after-update"

    def test_pull_stores_revision(self, monkeypatch):
        files = _FakeFiles(["r-pulled"])
        sync = self._sync(files, monkeypatch)
        data = sync.pull_library()
        assert data is not None
        assert settings.get_setting("drive_library_revision") == "r-pulled"

    def test_sync_retries_after_conflict(self, monkeypatch):
        # pull A → push sees B (conflict) → pull B → push sees B
        files = _FakeFiles(["A", "B", "B", "B"])
        sync = self._sync(files, monkeypatch)
        settings.set_setting("drive_library_revision", "stale")
        merged = sync.sync_library_with_store(_FakeStore(LibraryData()))
        assert merged is not None
        assert files.updated == 1
        assert settings.get_setting("drive_library_revision") == "r-after-update"
