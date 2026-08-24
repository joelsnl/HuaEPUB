# Author: joelsnl and Anthropic Claude
"""
Persistent application settings.

User data (settings, cache, library, logs) lives in ~/.huaepub/
(migrated automatically from ~/.noveldownloader/ when present).
The install/executable directory is separate (see get_app_dir) and is only
used for the auto-updater and as a migration source for older installs.

settings.json is written atomically (sibling .json.tmp, then replace)
under one lock for the full read-modify-write so concurrent set_setting
calls cannot drop keys.

Replaces the old updater_settings.json (whose auto_check_updates value is
migrated on first load if found).
"""

import os
import sys
import json
import shutil
import threading
from pathlib import Path
from typing import Any, Dict

from core.branding import (
    DATA_DIR_NAME,
    DRIVE_FOLDER_NAME,
    LEGACY_DATA_DIR_NAME,
)

SETTINGS_FILE = "settings.json"
LEGACY_UPDATER_SETTINGS_FILE = "updater_settings.json"

# Files/dirs that used to live next to the app and should move into the data dir
_MIGRATE_FILES = (
    SETTINGS_FILE,
    LEGACY_UPDATER_SETTINGS_FILE,
    "cache.db",
    "cache.db-wal",
    "cache.db-shm",
    "library.json",
)
_MIGRATE_DIRS = ("logs",)

DEFAULTS: Dict[str, Any] = {
    'auto_check_updates': True,
    'translate': True,
    'clean': True,
    'workers': 200,
    # '' means "use ~/.huaepub/books"
    'output_dir': '',
    # 'google', 'libretranslate', or 'ollama'
    'translation_backend': 'google',
    'libretranslate_url': 'https://libretranslate.com',
    'ollama_url': 'http://127.0.0.1:11434',
    'ollama_model': 'qwen2.5:3b',
    # After Google/LibreTranslate, optional local grammar pass (not a translator swap)
    'ollama_polish': False,
    # Use cached chapters from previous runs (resume support)
    'use_chapter_cache': True,
    # Watch system clipboard for novel URLs and queue them
    'clipboard_watcher': False,
    # Optional Google Drive sync (offline-first; off by default)
    'drive_sync_enabled': False,
    'drive_sync_library': True,
    'drive_sync_epubs': True,
    # Visible My Drive folder: create/reuse by name, or pin a folder id/URL
    'drive_folder_name': DRIVE_FOLDER_NAME,
    'drive_folder_id': '',
    'drive_library_hash': '',
    # last seen Drive library.json headRevisionId (empty = first sync / old client)
    'drive_library_revision': '',
    'drive_last_synced_at': 0.0,
    'drive_last_sync_summary': '',
    # Library shelf: 'grid' | 'list'
    'library_view': 'grid',
    # Library filter: 'all' | 'updates'
    'library_filter': 'all',
    # Drive options panel expanded under Library
    'drive_panel_expanded': False,
    # Local cache.db cap in MiB. 0 = unlimited. Oldest chapter HTML is
    # deleted first when the file is over this size (translations kept).
    'cache_max_mb': 2048,
    # One-shot honesty dialog the first time Polish English is checked.
    'polish_notice_shown': False,
    # In-app reader font size (points).
    'reader_font_pt': 18,
    # Main window geometry. 0 width/height means "use the built-in default".
    'window_x': 0,
    'window_y': 0,
    'window_w': 0,
    'window_h': 0,
}

_lock = threading.Lock()
_migration_done = False


def is_frozen() -> bool:
    """Check if running as a compiled executable (PyInstaller)."""
    return getattr(sys, 'frozen', False)


def get_app_dir() -> Path:
    """
    Install / executable directory (code lives here).
    Not for user data — use get_data_dir() for settings, cache, etc.
    """
    if is_frozen():
        return Path(sys.executable).parent
    return Path(os.path.dirname(os.path.abspath(__file__))).parent


def get_data_dir() -> Path:
    """
    Per-user data directory: ~/.huaepub/
    Created on first use. Migrates from ~/.noveldownloader/ and from the
    install dir once.
    """
    data_dir = Path.home() / DATA_DIR_NAME
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _migrate_legacy_data(data_dir)
    return data_dir


def get_default_books_dir() -> Path:
    """Default EPUB output folder: ~/.huaepub/books/"""
    books = get_data_dir() / "books"
    try:
        books.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return books


def _migrate_legacy_data(data_dir: Path):
    """
    One-time copy of settings/cache/library/logs from:
      1) ~/.noveldownloader/ (pre-HuaEPUB data dir)
      2) the old install-dir location
    Never overwrites files that already exist in the new data dir.
    """
    global _migration_done
    if _migration_done:
        return
    _migration_done = True

    try:
        legacy_home = Path.home() / LEGACY_DATA_DIR_NAME
        if legacy_home.is_dir() and legacy_home.resolve() != data_dir.resolve():
            _copy_migrate(legacy_home, data_dir)

        old_dir = get_app_dir()
        if old_dir.resolve() != data_dir.resolve():
            _copy_migrate(old_dir, data_dir)
    except Exception:
        pass


def _copy_migrate(src_root: Path, data_dir: Path):
    for name in _MIGRATE_FILES:
        src = src_root / name
        dst = data_dir / name
        if src.is_file() and not dst.exists():
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass

    for name in _MIGRATE_DIRS:
        src = src_root / name
        dst = data_dir / name
        if src.is_dir() and not dst.exists():
            try:
                shutil.copytree(src, dst)
            except Exception:
                pass


def get_settings_path() -> Path:
    return get_data_dir() / SETTINGS_FILE


def _load_settings_unlocked() -> Dict[str, Any]:
    """Load settings merged over defaults. Caller must hold _lock."""
    settings = dict(DEFAULTS)
    path = get_settings_path()
    try:
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                settings.update(stored)
        else:
            # Migrate auto_check_updates from the legacy updater settings file
            # (may already have been copied into the data dir by _migrate_legacy_data)
            for legacy_dir in (get_data_dir(), get_app_dir()):
                legacy = legacy_dir / LEGACY_UPDATER_SETTINGS_FILE
                if legacy.exists():
                    try:
                        with open(legacy, 'r', encoding='utf-8') as f:
                            old = json.load(f)
                        if isinstance(old, dict) and 'auto_check_updates' in old:
                            settings['auto_check_updates'] = bool(old['auto_check_updates'])
                        break
                    except Exception:
                        pass
    except Exception:
        pass
    return settings


def _write_settings_unlocked(settings: Dict[str, Any]) -> None:
    """Write settings.json via tmp+replace. Caller must hold _lock."""
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / SETTINGS_FILE
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(settings, indent=2)
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(payload)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    tmp.replace(path)
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass


def load_settings() -> Dict[str, Any]:
    """Load settings merged over defaults. Never raises."""
    try:
        with _lock:
            return _load_settings_unlocked()
    except Exception:
        return dict(DEFAULTS)


def save_settings(settings: Dict[str, Any]):
    """Persist settings atomically. Never raises."""
    try:
        with _lock:
            _write_settings_unlocked(dict(settings))
    except Exception:
        pass


def get_setting(key: str) -> Any:
    return load_settings().get(key, DEFAULTS.get(key))


def set_setting(key: str, value: Any):
    """Read-modify-write under one lock so concurrent keys are not dropped."""
    try:
        with _lock:
            settings = _load_settings_unlocked()
            settings[key] = value
            _write_settings_unlocked(settings)
    except Exception:
        pass


def update_settings(**kwargs):
    """Set several settings at once under one lock."""
    try:
        with _lock:
            settings = _load_settings_unlocked()
            settings.update(kwargs)
            _write_settings_unlocked(settings)
    except Exception:
        pass
