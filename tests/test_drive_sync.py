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

    def test_location_description(self):
        sync = DriveSync()
        assert "NovelDownloader" in sync.location_description()

    def test_parse_folder_id(self):
        assert DriveSync.parse_folder_id(
            "https://drive.google.com/drive/folders/1AbCDefGhIJ_klmn0123456789"
        ) == "1AbCDefGhIJ_klmn0123456789"
        assert DriveSync.parse_folder_id("1AbCDefGhIJ_klmn0123456789") == "1AbCDefGhIJ_klmn0123456789"
        assert DriveSync.parse_folder_id("not a folder") == ""
