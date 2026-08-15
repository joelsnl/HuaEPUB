"""Offline tests for Drive sync helpers (no network)."""

import os
import time
from datetime import datetime, timezone

from core.drive_sync import (
    DriveSync,
    RemoteEpubInfo,
    local_epub_needs_push,
    oauth_client_path,
    oauth_setup_instructions,
    SCOPES,
)


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
