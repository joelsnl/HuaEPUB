"""Offline tests for Drive sync helpers (no network)."""

from core.drive_sync import (
    DriveSync,
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
