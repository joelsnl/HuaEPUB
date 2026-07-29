# Author: joelsnl and Anthropic Claude
"""
Persistent application settings.

Stored as a single settings.json in the app directory. Replaces the old
updater_settings.json (whose auto_check_updates value is migrated on first
load if found).
"""

import os
import sys
import json
import threading
from pathlib import Path
from typing import Any, Dict

SETTINGS_FILE = "settings.json"
LEGACY_UPDATER_SETTINGS_FILE = "updater_settings.json"

DEFAULTS: Dict[str, Any] = {
    'auto_check_updates': True,
    'translate': True,
    'clean': True,
    'workers': 200,
    # '' means "use the system Downloads folder"
    'output_dir': '',
    # 'google' or 'libretranslate'
    'translation_backend': 'google',
    'libretranslate_url': 'https://libretranslate.com',
    # Use cached chapters from previous runs (resume support)
    'use_chapter_cache': True,
}

_lock = threading.Lock()


def is_frozen() -> bool:
    """Check if running as a compiled executable (PyInstaller)."""
    return getattr(sys, 'frozen', False)


def get_app_dir() -> Path:
    """Get the application directory (next to the exe when frozen)."""
    if is_frozen():
        return Path(sys.executable).parent
    return Path(os.path.dirname(os.path.abspath(__file__))).parent


def get_settings_path() -> Path:
    return get_app_dir() / SETTINGS_FILE


def load_settings() -> Dict[str, Any]:
    """Load settings merged over defaults. Never raises."""
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
            legacy = get_app_dir() / LEGACY_UPDATER_SETTINGS_FILE
            if legacy.exists():
                try:
                    with open(legacy, 'r', encoding='utf-8') as f:
                        old = json.load(f)
                    if isinstance(old, dict) and 'auto_check_updates' in old:
                        settings['auto_check_updates'] = bool(old['auto_check_updates'])
                except Exception:
                    pass
    except Exception:
        pass
    return settings


def save_settings(settings: Dict[str, Any]):
    """Persist settings. Never raises."""
    try:
        with _lock:
            with open(get_settings_path(), 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2)
    except Exception:
        pass


def get_setting(key: str) -> Any:
    return load_settings().get(key, DEFAULTS.get(key))


def set_setting(key: str, value: Any):
    settings = load_settings()
    settings[key] = value
    save_settings(settings)


def update_settings(**kwargs):
    """Set several settings at once."""
    settings = load_settings()
    settings.update(kwargs)
    save_settings(settings)
