"""Tests for core.settings (redirected to a temp directory)."""

import json

import pytest

import core.settings as settings


@pytest.fixture(autouse=True)
def temp_app_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'get_app_dir', lambda: tmp_path)
    return tmp_path


class TestSettings:
    def test_defaults_when_no_file(self):
        loaded = settings.load_settings()
        assert loaded['translate'] is True
        assert loaded['workers'] == 200
        assert loaded['translation_backend'] == 'google'

    def test_roundtrip(self):
        settings.set_setting('workers', 50)
        settings.set_setting('output_dir', '/tmp/books')
        assert settings.get_setting('workers') == 50
        assert settings.get_setting('output_dir') == '/tmp/books'
        # Untouched keys keep their defaults
        assert settings.get_setting('translate') is True

    def test_update_many(self):
        settings.update_settings(translate=False, clean=False)
        loaded = settings.load_settings()
        assert loaded['translate'] is False
        assert loaded['clean'] is False

    def test_corrupt_file_falls_back_to_defaults(self, temp_app_dir):
        (temp_app_dir / settings.SETTINGS_FILE).write_text('{not json')
        loaded = settings.load_settings()
        assert loaded['workers'] == 200

    def test_migrates_legacy_updater_settings(self, temp_app_dir):
        legacy = temp_app_dir / settings.LEGACY_UPDATER_SETTINGS_FILE
        legacy.write_text(json.dumps({'auto_check_updates': False}))
        loaded = settings.load_settings()
        assert loaded['auto_check_updates'] is False
