"""Tests for core.settings (redirected to a temp directory)."""

import json

import pytest

import core.settings as settings


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path, monkeypatch):
    """Point both data and app dirs at a temp folder; reset migration flag."""
    monkeypatch.setattr(settings, 'get_data_dir', lambda: tmp_path)
    monkeypatch.setattr(settings, 'get_app_dir', lambda: tmp_path / 'install')
    (tmp_path / 'install').mkdir(exist_ok=True)
    settings._migration_done = False
    return tmp_path


class TestSettings:
    def test_defaults_when_no_file(self):
        loaded = settings.load_settings()
        assert loaded['translate'] is True
        assert loaded['workers'] == 200
        assert loaded['translation_backend'] == 'google'
        assert loaded['drive_sync_enabled'] is False
        assert loaded['drive_sync_library'] is True
        assert loaded['drive_sync_epubs'] is True

    def test_default_books_dir(self, temp_data_dir):
        books = settings.get_default_books_dir()
        assert books == temp_data_dir / 'books'
        assert books.is_dir()

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

    def test_corrupt_file_falls_back_to_defaults(self, temp_data_dir):
        (temp_data_dir / settings.SETTINGS_FILE).write_text('{not json')
        loaded = settings.load_settings()
        assert loaded['workers'] == 200

    def test_migrates_legacy_updater_settings(self, temp_data_dir):
        legacy = temp_data_dir / settings.LEGACY_UPDATER_SETTINGS_FILE
        legacy.write_text(json.dumps({'auto_check_updates': False}))
        loaded = settings.load_settings()
        assert loaded['auto_check_updates'] is False

    def test_migrates_files_from_install_dir(self, tmp_path, monkeypatch):
        install = tmp_path / 'old_install'
        data = tmp_path / 'new_data'
        install.mkdir()
        data.mkdir()
        (install / 'settings.json').write_text(json.dumps({'workers': 33}))
        (install / 'library.json').write_text('{"history":[],"library":[]}')
        (install / 'cache.db').write_bytes(b'sqlite')

        settings._migration_done = False
        monkeypatch.setattr(settings, 'get_app_dir', lambda: install)
        # Avoid picking up a real ~/.noveldownloader on the developer machine
        monkeypatch.setattr(settings, 'LEGACY_DATA_DIR_NAME', '.__huaepub_legacy_absent__')

        # Call real get_data_dir logic via migration helper
        settings._migrate_legacy_data(data)

        assert (data / 'settings.json').exists()
        assert json.loads((data / 'settings.json').read_text())['workers'] == 33
        assert (data / 'library.json').exists()
        assert (data / 'cache.db').read_bytes() == b'sqlite'
        # Does not overwrite existing
        (data / 'settings.json').write_text(json.dumps({'workers': 99}))
        settings._migration_done = False
        settings._migrate_legacy_data(data)
        assert json.loads((data / 'settings.json').read_text())['workers'] == 99

    def test_migrates_from_legacy_home_data_dir(self, tmp_path, monkeypatch):
        home = tmp_path / 'fake_home'
        legacy = home / '.noveldownloader'
        data = tmp_path / '.huaepub'
        legacy.mkdir(parents=True)
        data.mkdir()
        (legacy / 'settings.json').write_text(json.dumps({'workers': 77}))
        (legacy / 'library.json').write_text('{"history":[],"library":[]}')

        settings._migration_done = False
        monkeypatch.setattr(settings.Path, 'home', staticmethod(lambda: home))
        monkeypatch.setattr(settings, 'get_app_dir', lambda: tmp_path / 'empty_install')
        (tmp_path / 'empty_install').mkdir()

        settings._migrate_legacy_data(data)
        assert json.loads((data / 'settings.json').read_text())['workers'] == 77
        assert (data / 'library.json').exists()
