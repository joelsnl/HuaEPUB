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
        assert loaded['translation_glossary'] == 'auto'
        assert loaded['ollama_url'] == 'http://127.0.0.1:11434'
        assert loaded['ollama_model'] == 'qwen2.5:3b'
        assert loaded['ollama_polish'] is False
        assert loaded['drive_sync_enabled'] is False
        assert loaded['drive_sync_library'] is True
        assert loaded['drive_sync_epubs'] is True
        assert loaded['drive_library_revision'] == ''
        assert loaded['polish_notice_shown'] is False
        assert loaded['nmt_notice_shown'] is False
        assert loaded['glossary_qwen_ask'] is True
        assert loaded['glossary_qwen_last_at'] == 0.0
        assert loaded['window_w'] == 0
        assert loaded['window_h'] == 0
        assert loaded['window_x'] == 0
        assert loaded['window_y'] == 0
        assert loaded['reader_font_pt'] == 18

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

    def test_window_geometry_and_polish_notice_persist(self):
        settings.update_settings(
            polish_notice_shown=True,
            window_x=40,
            window_y=50,
            window_w=1100,
            window_h=800,
        )
        loaded = settings.load_settings()
        assert loaded['polish_notice_shown'] is True
        assert loaded['window_x'] == 40
        assert loaded['window_y'] == 50
        assert loaded['window_w'] == 1100
        assert loaded['window_h'] == 800

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


class TestAtomicSettings:
    def test_writes_temp_then_replace(self, temp_data_dir, monkeypatch):
        replaced = []
        original = settings.Path.replace

        def tracking_replace(self, target):
            replaced.append((self.name, settings.Path(target).name))
            return original(self, target)

        monkeypatch.setattr(settings.Path, "replace", tracking_replace)
        settings.set_setting("workers", 42)
        assert any(src.endswith(".tmp") for src, _dst in replaced)
        assert (temp_data_dir / settings.SETTINGS_FILE).is_file()
        assert not (temp_data_dir / "settings.json.tmp").exists()
        assert settings.get_setting("workers") == 42

    def test_failed_replace_leaves_previous_file(self, temp_data_dir, monkeypatch):
        settings.set_setting("workers", 11)
        original = settings.Path.replace

        def boom(self, target):
            raise OSError("disk full")

        monkeypatch.setattr(settings.Path, "replace", boom)
        settings.set_setting("workers", 99)
        monkeypatch.setattr(settings.Path, "replace", original)
        assert settings.get_setting("workers") == 11

    def test_concurrent_set_setting_does_not_drop_keys(self, temp_data_dir):
        import threading

        settings.set_setting("workers", 1)
        settings.set_setting("translate", True)

        def set_workers():
            for _ in range(40):
                settings.set_setting("workers", 50)

        def set_translate():
            for _ in range(40):
                settings.set_setting("translate", False)

        t1 = threading.Thread(target=set_workers)
        t2 = threading.Thread(target=set_translate)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        loaded = settings.load_settings()
        assert loaded["workers"] == 50
        assert loaded["translate"] is False
        assert loaded["cache_max_mb"] == 2048
