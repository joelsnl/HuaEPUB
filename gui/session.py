# Author: joelsnl and Anthropic Claude
"""Shared app state for the Qt GUI (settings, cache, library, download control)."""

from __future__ import annotations

from typing import Any, Dict

from core.cache import NovelCache
from core.download_runner import DownloadControl
from core.drive_sync import get_drive_sync
from core.library import LibraryStore
from core.settings import get_data_dir, load_settings, save_settings


class AppSession:
    def __init__(self):
        self.data_dir = get_data_dir()
        self.settings: Dict[str, Any] = load_settings()
        self.cache = NovelCache(self.data_dir / "cache.db")
        self.library_store = LibraryStore(self.data_dir / "library.json")
        self.drive_sync = get_drive_sync()
        self.control = DownloadControl(data_dir=self.data_dir)
        self.output_dir: str = self.settings.get("output_dir", "") or ""

    def save_settings_from_options(
        self,
        *,
        translate: bool,
        clean: bool,
        use_cache: bool,
        clipboard: bool,
        workers: int,
        backend: str,
        translation_glossary: str = "auto",
        drive_enabled: bool,
        drive_library: bool,
        drive_epubs: bool,
        library_view: str,
        library_filter: str,
        drive_panel_expanded: bool,
        ollama_model: str = "qwen2.5:3b",
        ollama_url: str = "http://127.0.0.1:11434",
        ollama_polish: bool = False,
    ):
        self.settings["translate"] = translate
        self.settings["clean"] = clean
        self.settings["use_chapter_cache"] = use_cache
        self.settings["clipboard_watcher"] = clipboard
        self.settings["workers"] = workers
        self.settings["translation_backend"] = backend
        self.settings["translation_glossary"] = translation_glossary
        self.settings["ollama_model"] = ollama_model
        self.settings["ollama_url"] = ollama_url
        self.settings["ollama_polish"] = bool(ollama_polish)
        self.settings["output_dir"] = self.output_dir
        self.settings["drive_sync_enabled"] = drive_enabled
        self.settings["drive_sync_library"] = drive_library
        self.settings["drive_sync_epubs"] = drive_epubs
        self.settings["library_view"] = library_view
        self.settings["library_filter"] = library_filter
        self.settings["drive_panel_expanded"] = drive_panel_expanded
        save_settings(self.settings)

    def close(self):
        try:
            if self.control.is_downloading and self.control.active_job:
                self.control.active_job["status"] = "paused"
                self.control.persist_job(force=True)
        except Exception:
            pass
        try:
            self.cache.close()
        except Exception:
            pass
