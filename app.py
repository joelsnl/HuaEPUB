#!/usr/bin/env python3
# Author: joelsnl and Anthropic Claude
"""
HuaEPUB
A standalone GUI application for downloading Chinese web novels and building English EPUBs.

Based on WebToEpub extension and fixTranslate.py
"""

import os
import sys

# Add parent directory to path for imports (before sanitizing / other core imports)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Must run before any HTTP/TLS imports: post-update relaunch can inherit a
# dead PyInstaller _MEI* SSL_CERT_FILE from the previous process.
from core.utils import sanitize_runtime_env
sanitize_runtime_env()

import time
import threading
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from typing import List, Optional, Tuple
from io import BytesIO

import customtkinter as ctk
from PIL import Image

from core.parser import (
    Chapter, NovelInfo, get_parser_for_url, cleanup_browser, create_http_session
)
from core.cleaner import ContentCleaner
from core.translator import GoogleTranslator
from core.epub_builder import EPUBBuilder, TranslatedEPUBBuilder
from core.updater import (
    get_current_version, check_for_updates_async, download_update_async,
    get_auto_check_updates, set_auto_check_updates, is_frozen
)
from core.settings import (
    load_settings, save_settings, get_app_dir, get_data_dir, get_default_books_dir,
)
from core.cache import NovelCache
from core.download_job import (
    load_job, save_job, clear_job,
    chapters_to_job, chapters_from_job,
    novel_info_to_job, novel_info_from_job,
    job_display_title, job_chapter_urls,
)
from core.branding import APP_TITLE, DRIVE_FOLDER_NAME, LOG_FILE_NAME
from core.logger import setup_logging
from core.utils import format_eta, safe_filename, extract_urls, looks_like_url
from core.library import LibraryStore, new_chapters_since
from core.notify import notify
from core.drive_sync import (
    get_drive_sync, DriveSyncError, oauth_setup_instructions, oauth_client_path,
)

# Import parsers to register them
import parsers

# Shared session for auxiliary requests (cover preview)
http_session = create_http_session()


# Set appearance
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class _DownloadCancelled(Exception):
    """Raised inside download worker threads when the user cancels."""
    pass


class HuaEPUBApp(ctk.CTk):
    """Main application window."""
    
    def __init__(self):
        super().__init__()
        
        self.title(f"{APP_TITLE} v{get_current_version()}")
        self.geometry("900x700")
        self.minsize(800, 600)
        
        # Get directories: install dir (updater) vs user data (~/.huaepub)
        self.app_dir = get_app_dir()
        self.data_dir = get_data_dir()
        
        # Persistent settings and caches
        self.settings = load_settings()
        self.output_dir = self.settings.get('output_dir', '') or ''
        self.cache = NovelCache(self.data_dir / 'cache.db')
        self.library_store = LibraryStore(self.data_dir / 'library.json')
        self.drive_sync = get_drive_sync()
        
        # State
        self.novel_info: Optional[NovelInfo] = None
        self.chapters: List[Chapter] = []
        self.parser = None
        self.is_downloading = False
        self.cancel_requested = False
        self.is_paused = False
        self.cover_image = None  # Store PhotoImage reference
        self.translated_title = None  # Store translated title
        self._active_job: Optional[dict] = None  # local resume snapshot (not Drive)
        self._job_save_counter = 0
        
        # Generation counters to ignore results from stale background work
        self._fetch_generation = 0   # bumped on each new fetch
        self._list_generation = 0    # bumped each time the chapter list rebuilds
        
        # Multi-download mode state
        self.multi_mode = False
        self.library_mode = False
        self.multi_novels: List[dict] = []  # [{url, parser, info, chapters, status, translated_title}]
        self.multi_result_labels: List[dict] = []  # UI labels for each novel row
        self._library_row_widgets: List[dict] = []
        self._library_cover_images: list = []  # keep CTkImage refs alive
        self._library_check_status: dict = {}  # source_url -> {state, new_count, total, error}
        self._library_checking = False
        self._drive_syncing = False
        self._remote_books: dict = {}  # filename -> drive file id
        self._library_view = self.settings.get('library_view', 'grid') or 'grid'
        self._library_filter = self.settings.get('library_filter', 'all') or 'all'
        self._drive_panel_expanded = bool(self.settings.get('drive_panel_expanded', False))
        self._library_reflow_after = None
        self._library_grid_last_width = 0
        self._library_grid_last_cols = 0
        self._library_wheel_bound = False
        
        # Clipboard watcher
        self._clipboard_last = ""
        self._clipboard_seen_urls = set()
        
        # Coalesced progress/status UI (worker threads → main thread, ~10 Hz)
        self._pending_progress: Optional[float] = None
        self._pending_status: Optional[str] = None
        self._progress_flush_scheduled = False
        self._last_progress_flush = 0.0
        self._PROGRESS_MIN_INTERVAL_MS = 100
        
        # Create UI
        self._create_ui()
        
        # Cleanup browser on close
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        
        # Offer resume of a locally saved incomplete download (after UI is ready)
        self.after(600, self._check_resume_job)
        
        # Auto-check for updates on startup (if enabled)
        if get_auto_check_updates():
            self.after(2000, self._auto_check_updates)  # Check after 2 seconds
        
        # Start clipboard polling loop (no-op while checkbox is off)
        self.after(3000, self._poll_clipboard)
        
        # Restore Drive session / optional startup sync
        if self.settings.get('drive_sync_enabled'):
            self.after(2500, self._drive_startup_sync)
        
        # Auto-check library novels for new chapters shortly after launch
        if self.library_store.get_library():
            self.after(4000, lambda: self._schedule_library_check(reason="startup"))
    
    def _on_close(self):
        """Handle window close - persist settings, keep incomplete download job, clean up."""
        try:
            self._menu_close()
        except Exception:
            pass
        try:
            if self.is_downloading and self._active_job:
                # Keep local resume file; chapter HTML is already in cache.db
                self._active_job["status"] = "paused"
                save_job(self._active_job, self.data_dir)
        except Exception:
            pass
        try:
            self._save_settings()
        except Exception:
            pass
        try:
            self.cache.close()
        except Exception:
            pass
        try:
            cleanup_browser()
        except:
            pass
        self.destroy()
    
    def _save_settings(self):
        """Persist current UI options to settings.json."""
        self.settings['translate'] = bool(self.translate_var.get())
        self.settings['clean'] = bool(self.clean_var.get())
        self.settings['use_chapter_cache'] = bool(self.use_cache_var.get())
        self.settings['clipboard_watcher'] = bool(self.clipboard_var.get())
        self.settings['drive_sync_enabled'] = bool(self.drive_enabled_var.get())
        self.settings['drive_sync_library'] = bool(self.drive_library_var.get())
        self.settings['drive_sync_epubs'] = bool(self.drive_epubs_var.get())
        self.settings['workers'] = self._get_workers()
        self.settings['output_dir'] = self.output_dir
        self.settings['translation_backend'] = (
            'libretranslate' if self.backend_menu.get() == 'LibreTranslate' else 'google'
        )
        self.settings['library_view'] = getattr(self, '_library_view', 'grid')
        self.settings['library_filter'] = getattr(self, '_library_filter', 'all')
        self.settings['drive_panel_expanded'] = bool(getattr(self, '_drive_panel_expanded', False))
        save_settings(self.settings)
    
    def _get_workers(self) -> int:
        """Parse the workers entry, falling back to the default."""
        try:
            return max(1, int(self.workers_entry.get()))
        except ValueError:
            return 200
    
    def _make_translator(self, max_workers: int) -> GoogleTranslator:
        """Create a translator configured from the current UI settings."""
        backend = 'libretranslate' if self.backend_menu.get() == 'LibreTranslate' else 'google'
        return GoogleTranslator(
            max_workers=max_workers,
            backend=backend,
            libretranslate_url=self.settings.get('libretranslate_url', 'https://libretranslate.com'),
            persistent_cache=self.cache,
        )
    
    def _create_ui(self):
        """Create all UI elements."""
        # Configure grid (row 0 = menubar; content starts at row 1)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        
        self._create_menubar()
        
        # === Mode Toggle + URL Input Section ===
        url_frame = ctk.CTkFrame(self)
        url_frame.grid(row=1, column=0, padx=10, pady=(6, 5), sticky="ew")
        url_frame.grid_columnconfigure(1, weight=1)
        
        # Incomplete download resume banner (local only; shown on startup if job exists)
        self.resume_frame = ctk.CTkFrame(url_frame, fg_color=("#E8E0C8", "#3A3420"))
        self.resume_frame.grid_columnconfigure(0, weight=1)
        self.resume_label = ctk.CTkLabel(
            self.resume_frame,
            text="",
            font=("", 12),
            anchor="w",
            justify="left",
        )
        self.resume_label.grid(row=0, column=0, padx=10, pady=8, sticky="ew")
        resume_btns = ctk.CTkFrame(self.resume_frame, fg_color="transparent")
        resume_btns.grid(row=0, column=1, padx=8, pady=6)
        self.resume_continue_btn = ctk.CTkButton(
            resume_btns, text="Resume", width=90, height=28,
            fg_color="#2B7A3E", hover_color="#236332",
            command=self._on_resume_job,
        )
        self.resume_continue_btn.pack(side="left", padx=3)
        self.resume_discard_btn = ctk.CTkButton(
            resume_btns, text="Discard", width=90, height=28,
            fg_color="gray40", hover_color="gray30",
            command=self._on_discard_job,
        )
        self.resume_discard_btn.pack(side="left", padx=3)
        # Hidden until an incomplete job is found
        self.resume_frame.grid_remove()
        
        # Mode toggle
        self.mode_switch = ctk.CTkSegmentedButton(
            url_frame, values=["Single", "Multi", "Library"],
            command=self._on_mode_change, width=220
        )
        self.mode_switch.set("Single")
        self.mode_switch.grid(row=1, column=0, padx=(10, 5), pady=10)
        
        # Single-mode URL entry
        self.single_url_frame = ctk.CTkFrame(url_frame, fg_color="transparent")
        self.single_url_frame.grid(row=1, column=1, columnspan=2, padx=0, pady=0, sticky="ew")
        self.single_url_frame.grid_columnconfigure(0, weight=1)
        
        self.url_entry = ctk.CTkEntry(self.single_url_frame, placeholder_text="Enter novel URL (e.g., https://twkan.com/book/12345.html)")
        self.url_entry.grid(row=0, column=0, padx=5, pady=10, sticky="ew")
        
        self.recent_btn = ctk.CTkButton(
            self.single_url_frame, text="Recent", width=70,
            command=self._show_recent_menu,
            fg_color="gray40", hover_color="gray30"
        )
        self.recent_btn.grid(row=0, column=1, padx=(5, 0), pady=10)
        
        self.fetch_btn = ctk.CTkButton(self.single_url_frame, text="Fetch Chapters", command=self._on_fetch)
        self.fetch_btn.grid(row=0, column=2, padx=(5, 10), pady=10)
        
        # === Single Mode: Novel Info Section (with cover preview) ===
        self.info_frame = ctk.CTkFrame(self)
        self.info_frame.grid(row=2, column=0, padx=10, pady=5, sticky="ew")
        self.info_frame.grid_columnconfigure(1, weight=1)
        
        # Cover image on the left
        self.cover_frame = ctk.CTkFrame(self.info_frame, width=100, height=140)
        self.cover_frame.grid(row=0, column=0, rowspan=3, padx=10, pady=10, sticky="ns")
        self.cover_frame.grid_propagate(False)
        
        self.cover_label = ctk.CTkLabel(self.cover_frame, text="No Cover", width=100, height=140)
        self.cover_label.pack(expand=True, fill="both")
        
        # Info on the right
        info_right = ctk.CTkFrame(self.info_frame, fg_color="transparent")
        info_right.grid(row=0, column=1, rowspan=3, padx=5, pady=5, sticky="nsew")
        info_right.grid_columnconfigure(1, weight=1)
        
        ctk.CTkLabel(info_right, text="Title:", font=("", 12)).grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.title_label = ctk.CTkLabel(info_right, text="-", font=("", 12, "bold"), wraplength=500, justify="left")
        self.title_label.grid(row=0, column=1, columnspan=3, padx=5, pady=5, sticky="w")
        
        ctk.CTkLabel(info_right, text="Author:", font=("", 12)).grid(row=1, column=0, padx=5, pady=5, sticky="w")
        self.author_label = ctk.CTkLabel(info_right, text="-", font=("", 12))
        self.author_label.grid(row=1, column=1, padx=5, pady=5, sticky="w")
        
        ctk.CTkLabel(info_right, text="Chapters:", font=("", 12)).grid(row=1, column=2, padx=(20, 5), pady=5, sticky="w")
        self.chapters_label = ctk.CTkLabel(info_right, text="0", font=("", 12))
        self.chapters_label.grid(row=1, column=3, padx=5, pady=5, sticky="w")
        
        ctk.CTkLabel(info_right, text="English Title:", font=("", 12)).grid(row=2, column=0, padx=5, pady=5, sticky="w")
        self.eng_title_label = ctk.CTkLabel(info_right, text="-", font=("", 11), wraplength=500, justify="left", text_color="gray")
        self.eng_title_label.grid(row=2, column=1, columnspan=3, padx=5, pady=5, sticky="w")
        
        # === Chapter List Section ===
        self.list_frame = ctk.CTkFrame(self)
        self.list_frame.grid(row=3, column=0, padx=10, pady=5, sticky="nsew")
        self.list_frame.grid_columnconfigure(0, weight=1)
        self.list_frame.grid_rowconfigure(1, weight=1)
        
        # Selection buttons
        btn_frame = ctk.CTkFrame(self.list_frame, fg_color="transparent")
        btn_frame.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        
        ctk.CTkButton(btn_frame, text="Select All", width=90, command=self._select_all).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text="Select None", width=90, command=self._select_none).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text="Invert", width=70, command=self._invert_selection).pack(side="left", padx=4)
        
        # Range selection (e.g. chapters 200-450 without scrolling the list)
        ctk.CTkLabel(btn_frame, text="Range:").pack(side="left", padx=(15, 2))
        self.range_from_entry = ctk.CTkEntry(btn_frame, width=55, placeholder_text="from")
        self.range_from_entry.pack(side="left", padx=2)
        ctk.CTkLabel(btn_frame, text="-").pack(side="left")
        self.range_to_entry = ctk.CTkEntry(btn_frame, width=55, placeholder_text="to")
        self.range_to_entry.pack(side="left", padx=2)
        ctk.CTkButton(btn_frame, text="Select Range", width=95, command=self._select_range).pack(side="left", padx=4)
        
        self.selected_label = ctk.CTkLabel(btn_frame, text="Selected: 0")
        self.selected_label.pack(side="right", padx=10)
        
        # Native Treeview for chapters (CTkCheckBox x N destroys resize performance)
        self._setup_chapter_tree_style()
        tree_wrap = tk.Frame(self.list_frame, bg="#2b2b2b", highlightthickness=0)
        tree_wrap.grid(row=1, column=0, padx=5, pady=5, sticky="nsew")
        tree_wrap.grid_columnconfigure(0, weight=1)
        tree_wrap.grid_rowconfigure(0, weight=1)
        
        self.chapter_scroll = ttk.Scrollbar(tree_wrap, orient="vertical")
        self.chapter_tree = ttk.Treeview(
            tree_wrap,
            show="tree",
            selectmode="extended",
            style="Chapter.Treeview",
            yscrollcommand=self.chapter_scroll.set,
        )
        self.chapter_scroll.configure(command=self.chapter_tree.yview)
        self.chapter_tree.grid(row=0, column=0, sticky="nsew")
        self.chapter_scroll.grid(row=0, column=1, sticky="ns")
        self.chapter_tree.bind("<<TreeviewSelect>>", lambda _e: self._update_selected_count())
        self.chapter_tree.bind("<Control-a>", self._tree_select_all_event)
        self.chapter_tree.bind("<Control-A>", self._tree_select_all_event)
        
        # === Multi Mode UI (hidden by default) ===
        self.multi_frame = ctk.CTkFrame(self)
        # Not gridded yet - shown when multi mode is activated
        self.multi_frame.grid_columnconfigure(0, weight=1)
        self.multi_frame.grid_rowconfigure(1, weight=1)
        
        # URL block paste area
        multi_url_section = ctk.CTkFrame(self.multi_frame)
        multi_url_section.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        multi_url_section.grid_columnconfigure(0, weight=1)
        
        multi_url_header = ctk.CTkFrame(multi_url_section, fg_color="transparent")
        multi_url_header.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        
        ctk.CTkLabel(
            multi_url_header,
            text="Paste novel URLs (one per line):",
            font=("", 13, "bold")
        ).pack(side="left", padx=5)
        
        self.multi_clear_btn = ctk.CTkButton(
            multi_url_header, text="Clear", width=70, height=28,
            command=self._multi_clear_urls,
            fg_color="gray40", hover_color="gray30"
        )
        self.multi_clear_btn.pack(side="right", padx=5)
        
        self.multi_fetch_btn = ctk.CTkButton(
            multi_url_header, text="Fetch All", width=100, height=28,
            command=self._on_multi_fetch, fg_color="#2B7A3E", hover_color="#236332"
        )
        self.multi_fetch_btn.pack(side="right", padx=5)
        
        self.multi_url_text = ctk.CTkTextbox(multi_url_section, height=110)
        self.multi_url_text.grid(row=1, column=0, padx=5, pady=(0, 5), sticky="ew")
        
        # Results table
        self.multi_results_frame = ctk.CTkScrollableFrame(self.multi_frame, label_text="Novels")
        self.multi_results_frame.grid(row=1, column=0, padx=5, pady=5, sticky="nsew")
        self.multi_results_frame.grid_columnconfigure(1, weight=1)
        
        # Multi download button
        self.multi_download_btn = ctk.CTkButton(
            self.multi_frame,
            text="Download All",
            font=("", 14, "bold"),
            height=36, width=160,
            command=self._on_multi_download,
            state="disabled",
            fg_color="#2B7A3E", hover_color="#236332"
        )
        self.multi_download_btn.grid(row=2, column=0, pady=(5, 5))
        
        # === Library Mode UI (hidden by default) ===
        self.library_frame = ctk.CTkFrame(self)
        self.library_frame.grid_columnconfigure(0, weight=1)
        self.library_frame.grid_rowconfigure(2, weight=1)
        
        lib_header = ctk.CTkFrame(self.library_frame, fg_color="transparent")
        lib_header.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        
        header_left = ctk.CTkFrame(lib_header, fg_color="transparent")
        header_left.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            header_left,
            text="Your library — covers & TOC snapshots stay on this device; Drive only syncs library.json + EPUBs.",
            font=("", 12),
            text_color="gray",
            wraplength=420,
            justify="left",
        ).pack(side="left", padx=5)
        
        self.library_view_seg = ctk.CTkSegmentedButton(
            lib_header,
            values=["Grid", "List"],
            width=140,
            command=self._on_library_view_change,
        )
        self.library_view_seg.set("Grid" if self._library_view != "list" else "List")
        self.library_view_seg.pack(side="right", padx=3)
        
        self.library_filter_seg = ctk.CTkSegmentedButton(
            lib_header,
            values=["All", "Updates"],
            width=150,
            command=self._on_library_filter_change,
        )
        self.library_filter_seg.set("Updates" if self._library_filter == "updates" else "All")
        self.library_filter_seg.pack(side="right", padx=3)
        
        self.library_update_all_btn = ctk.CTkButton(
            lib_header, text="Update All", width=95, height=28,
            command=self._on_library_update_all,
            state="disabled",
            fg_color="#2B7A3E", hover_color="#236332",
        )
        self.library_update_all_btn.pack(side="right", padx=3)
        self.library_check_btn = ctk.CTkButton(
            lib_header, text="Check updates", width=110, height=28,
            command=lambda: self._schedule_library_check(reason="manual", force=True),
            fg_color="gray40", hover_color="gray30",
        )
        self.library_check_btn.pack(side="right", padx=3)
        self.library_refresh_btn = ctk.CTkButton(
            lib_header, text="Refresh", width=80, height=28,
            command=self._refresh_library_ui,
            fg_color="gray40", hover_color="gray30"
        )
        self.library_refresh_btn.pack(side="right", padx=3)
        self.library_check_status_label = ctk.CTkLabel(
            lib_header, text="", font=("", 11), text_color="gray"
        )
        self.library_check_status_label.pack(side="right", padx=8)
        
        # Compact / expandable Google Drive sync panel
        self.drive_sync_panel = ctk.CTkFrame(self.library_frame)
        self.drive_sync_panel.grid(row=1, column=0, padx=5, pady=(0, 5), sticky="ew")
        
        self.drive_summary_row = ctk.CTkFrame(self.drive_sync_panel, fg_color="transparent")
        self.drive_summary_row.pack(fill="x", padx=8, pady=6)
        
        self.drive_expand_btn = ctk.CTkButton(
            self.drive_summary_row, text="▸ Drive", width=70, height=26,
            command=self._toggle_drive_panel,
            fg_color="gray40", hover_color="gray30",
        )
        self.drive_expand_btn.pack(side="left", padx=(0, 8))
        
        self.drive_enabled_var = ctk.BooleanVar(
            value=bool(self.settings.get('drive_sync_enabled', False))
        )
        ctk.CTkCheckBox(
            self.drive_summary_row,
            text="Sync",
            variable=self.drive_enabled_var,
            command=self._on_drive_enabled_toggle,
            width=60,
        ).pack(side="left", padx=(0, 8))
        
        self.drive_status_label = ctk.CTkLabel(
            self.drive_summary_row, text="Drive sync off", font=("", 11), text_color="gray"
        )
        self.drive_status_label.pack(side="left", padx=5)
        
        self.drive_sync_now_btn = ctk.CTkButton(
            self.drive_summary_row, text="Sync Now", width=90, height=26,
            command=self._on_drive_sync_now,
            fg_color="#2B7A3E", hover_color="#236332",
        )
        self.drive_sync_now_btn.pack(side="right", padx=3)
        self.drive_connect_btn = ctk.CTkButton(
            self.drive_summary_row, text="Connect", width=90, height=26,
            command=self._on_drive_connect,
        )
        self.drive_connect_btn.pack(side="right", padx=3)
        
        self.drive_details = ctk.CTkFrame(self.drive_sync_panel, fg_color="transparent")
        
        sync_row1 = ctk.CTkFrame(self.drive_details, fg_color="transparent")
        sync_row1.pack(fill="x", padx=8, pady=(0, 4))
        self.library_reset_btn = ctk.CTkButton(
            sync_row1, text="Reset library", width=100, height=28,
            command=self._on_reset_library,
            fg_color="gray40", hover_color="gray30",
        )
        self.library_reset_btn.pack(side="right", padx=3)
        
        sync_row2 = ctk.CTkFrame(self.drive_details, fg_color="transparent")
        sync_row2.pack(fill="x", padx=8, pady=(0, 4))
        
        self.drive_library_var = ctk.BooleanVar(
            value=bool(self.settings.get('drive_sync_library', True))
        )
        ctk.CTkCheckBox(
            sync_row2, text="Sync library",
            variable=self.drive_library_var,
            command=self._on_drive_option_change,
        ).pack(side="left", padx=5)
        
        self.drive_epubs_var = ctk.BooleanVar(
            value=bool(self.settings.get('drive_sync_epubs', True))
        )
        ctk.CTkCheckBox(
            sync_row2, text="Sync EPUBs",
            variable=self.drive_epubs_var,
            command=self._on_drive_option_change,
        ).pack(side="left", padx=5)
        
        self.drive_change_folder_btn = ctk.CTkButton(
            sync_row2, text="Change folder", width=110, height=28,
            command=self._on_drive_change_folder,
            fg_color="gray40", hover_color="gray30",
        )
        self.drive_change_folder_btn.pack(side="right", padx=3)
        self.drive_open_folder_btn = ctk.CTkButton(
            sync_row2, text="Open folder", width=95, height=28,
            command=self._on_drive_open_folder,
            fg_color="gray40", hover_color="gray30",
        )
        self.drive_open_folder_btn.pack(side="right", padx=3)
        
        sync_row3 = ctk.CTkFrame(self.drive_details, fg_color="transparent")
        sync_row3.pack(fill="x", padx=8, pady=(0, 8))
        self.drive_folder_help = ctk.CTkLabel(
            sync_row3,
            text="",
            font=("", 11),
            text_color="gray",
            anchor="w",
            justify="left",
            wraplength=780,
        )
        self.drive_folder_help.pack(side="left", fill="x", expand=True)
        self.drive_last_sync_label = ctk.CTkLabel(
            sync_row3,
            text="",
            font=("", 11),
            text_color="gray",
            anchor="e",
        )
        self.drive_last_sync_label.pack(side="right", padx=(10, 0))
        
        self._apply_drive_panel_visibility()
        self._update_drive_sync_controls()
        self._update_drive_folder_help()
        self._update_drive_last_sync_label()
        
        # Shelf body: grid scroll OR tree list
        self.library_body = ctk.CTkFrame(self.library_frame, fg_color="transparent")
        self.library_body.grid(row=2, column=0, padx=5, pady=5, sticky="nsew")
        self.library_body.grid_columnconfigure(0, weight=1)
        self.library_body.grid_rowconfigure(0, weight=1)
        
        self.library_list_frame = ctk.CTkScrollableFrame(
            self.library_body, label_text="Tracked novels", height=360
        )
        self.library_list_frame.grid(row=0, column=0, sticky="nsew")
        # Reflow on window/shelf resize. Do NOT key off the inner canvas width —
        # CTkScrollableFrame's canvas often stays at the old wide size when the
        # window shrinks, which froze the column count.
        self.library_body.bind("<Configure>", self._on_library_shelf_configure)
        self.library_frame.bind("<Configure>", self._on_library_shelf_configure)
        self.bind("<Configure>", self._on_library_window_configure, add="+")
        try:
            # CTk defaults to 1px increments; wheel delta/6 then feels almost stuck
            self.library_list_frame._parent_canvas.configure(yscrollincrement=20)
        except Exception:
            pass
        self._bind_library_scroll_helpers()
        
        self.library_tree_frame = ctk.CTkFrame(self.library_body, fg_color="transparent")
        self.library_tree_frame.grid_columnconfigure(0, weight=1)
        self.library_tree_frame.grid_rowconfigure(0, weight=1)
        
        self._setup_library_tree_style()
        self.library_tree = ttk.Treeview(
            self.library_tree_frame,
            columns=("chapters", "status", "updated"),
            show="headings",
            style="Library.Treeview",
            selectmode="browse",
        )
        self.library_tree.heading("chapters", text="Chapters")
        self.library_tree.heading("status", text="Status")
        self.library_tree.heading("updated", text="Updated")
        # Treeview needs a tree column for title — use show="tree headings"
        self.library_tree.configure(show="tree headings")
        self.library_tree.heading("#0", text="Title")
        self.library_tree.column("#0", width=320, stretch=True)
        self.library_tree.column("chapters", width=80, stretch=False, anchor="center")
        self.library_tree.column("status", width=180, stretch=False)
        self.library_tree.column("updated", width=100, stretch=False, anchor="center")
        self.library_tree_scroll = ttk.Scrollbar(
            self.library_tree_frame, orient="vertical", command=self.library_tree.yview
        )
        self.library_tree.configure(yscrollcommand=self.library_tree_scroll.set)
        self.library_tree.grid(row=0, column=0, sticky="nsew")
        self.library_tree_scroll.grid(row=0, column=1, sticky="ns")
        self.library_tree.bind("<Double-1>", self._on_library_tree_activate)
        self.library_tree.bind("<Button-3>", self._on_library_tree_menu)
        self.library_tree.bind("<<TreeviewSelect>>", self._on_library_tree_select)
        
        self.library_actions_bar = ctk.CTkFrame(self.library_body, fg_color="transparent")
        self.library_actions_bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ctk.CTkButton(
            self.library_actions_bar, text="Update", width=80, height=28,
            fg_color="#2B7A3E", hover_color="#236332",
            command=self._on_library_selected_update,
        ).pack(side="left", padx=3)
        ctk.CTkButton(
            self.library_actions_bar, text="Open URL", width=80, height=28,
            fg_color="gray40", hover_color="gray30",
            command=self._on_library_selected_open,
        ).pack(side="left", padx=3)
        ctk.CTkButton(
            self.library_actions_bar, text="Remove", width=70, height=28,
            fg_color="gray40", hover_color="gray30",
            command=self._on_library_selected_remove,
        ).pack(side="left", padx=3)
        self.library_download_epub_btn = ctk.CTkButton(
            self.library_actions_bar, text="Download EPUB", width=110, height=28,
            command=self._on_library_selected_download_epub,
        )
        self.library_download_epub_btn.pack(side="left", padx=3)
        
        self._selected_library_url: Optional[str] = None
        self._apply_library_view_visibility()
        
        # === Options Section ===
        options_frame = ctk.CTkFrame(self)
        options_frame.grid(row=4, column=0, padx=10, pady=5, sticky="ew")
        
        top_row = ctk.CTkFrame(options_frame, fg_color="transparent")
        top_row.pack(fill="x")
        
        # Left side - checkboxes (initial values from saved settings)
        left_opts = ctk.CTkFrame(top_row, fg_color="transparent")
        left_opts.pack(side="left", padx=10, pady=(10, 5))
        
        self.clean_var = ctk.BooleanVar(value=bool(self.settings.get('clean', True)))
        ctk.CTkCheckBox(left_opts, text="Remove watermarks & ads", variable=self.clean_var).pack(anchor="w", pady=2)
        
        self.translate_var = ctk.BooleanVar(value=bool(self.settings.get('translate', True)))
        ctk.CTkCheckBox(left_opts, text="Translate to English", variable=self.translate_var).pack(anchor="w", pady=2)
        
        self.use_cache_var = ctk.BooleanVar(value=bool(self.settings.get('use_chapter_cache', True)))
        ctk.CTkCheckBox(left_opts, text="Use chapter cache (resume)", variable=self.use_cache_var).pack(anchor="w", pady=2)
        
        self.clipboard_var = ctk.BooleanVar(value=bool(self.settings.get('clipboard_watcher', False)))
        self.clipboard_cb = ctk.CTkCheckBox(
            left_opts,
            text="Watch clipboard for URLs",
            variable=self.clipboard_var,
            command=self._on_clipboard_toggle,
        )
        self.clipboard_cb.pack(anchor="w", pady=2)
        self._refresh_clipboard_label()
        
        # Right side - translator backend + workers
        right_opts = ctk.CTkFrame(top_row, fg_color="transparent")
        right_opts.pack(side="right", padx=10, pady=(10, 5))
        
        ctk.CTkLabel(right_opts, text="Translator:").pack(side="left", padx=(0, 5))
        self.backend_menu = ctk.CTkOptionMenu(right_opts, values=["Google", "LibreTranslate"], width=130)
        self.backend_menu.set(
            "LibreTranslate" if self.settings.get('translation_backend') == 'libretranslate' else "Google"
        )
        self.backend_menu.pack(side="left", padx=(0, 15))
        
        ctk.CTkLabel(right_opts, text="Translation Workers:").pack(side="left", padx=5)
        self.workers_entry = ctk.CTkEntry(right_opts, width=60)
        self.workers_entry.insert(0, str(self.settings.get('workers', 200)))
        self.workers_entry.pack(side="left", padx=5)
        
        # Bottom row - output folder
        folder_row = ctk.CTkFrame(options_frame, fg_color="transparent")
        folder_row.pack(fill="x", padx=10, pady=(0, 10))
        
        ctk.CTkLabel(folder_row, text="Save to:").pack(side="left", padx=(0, 5))
        self.output_dir_label = ctk.CTkLabel(folder_row, text="", text_color="gray", anchor="w")
        self.output_dir_label.pack(side="left", fill="x", expand=True)
        
        ctk.CTkButton(
            folder_row, text="Default", width=70,
            command=self._reset_output_dir,
            fg_color="gray40", hover_color="gray30"
        ).pack(side="right", padx=2)
        ctk.CTkButton(
            folder_row, text="Browse", width=70,
            command=self._choose_output_dir
        ).pack(side="right", padx=2)
        
        self._update_output_dir_label()
        
        # === Progress Section ===
        progress_frame = ctk.CTkFrame(self)
        progress_frame.grid(row=5, column=0, padx=10, pady=5, sticky="ew")
        progress_frame.grid_columnconfigure(0, weight=1)
        
        self.progress_bar = ctk.CTkProgressBar(progress_frame)
        self.progress_bar.grid(row=0, column=0, padx=10, pady=(10, 5), sticky="ew")
        self.progress_bar.set(0)
        
        self.status_label = ctk.CTkLabel(progress_frame, text="Ready")
        self.status_label.grid(row=1, column=0, padx=10, pady=(5, 10))
        
        # === Download Button ===
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=6, column=0, padx=10, pady=10)
        
        self.download_btn = ctk.CTkButton(
            btn_frame, 
            text="Download EPUB", 
            font=("", 14, "bold"),
            height=40,
            width=200,
            command=self._on_download,
            state="disabled"
        )
        self.download_btn.pack(side="left", padx=5)
        
        self.pause_btn = ctk.CTkButton(
            btn_frame,
            text="Pause",
            height=40,
            width=100,
            command=self._on_pause_toggle,
            state="disabled",
            fg_color="gray40",
            hover_color="gray30",
        )
        self.pause_btn.pack(side="left", padx=5)
        
        self.cancel_btn = ctk.CTkButton(
            btn_frame,
            text="Cancel",
            height=40,
            width=100,
            command=self._on_cancel,
            state="disabled",
            fg_color="red",
            hover_color="darkred"
        )
        self.cancel_btn.pack(side="left", padx=5)
        
        # === Footer with Version and Update ===
        footer_frame = ctk.CTkFrame(self, fg_color="transparent")
        footer_frame.grid(row=7, column=0, padx=10, pady=(0, 10), sticky="ew")
        
        # Version label on left
        self.version_label = ctk.CTkLabel(
            footer_frame, 
            text=f"v{get_current_version()}", 
            font=("", 11),
            text_color="gray"
        )
        self.version_label.pack(side="left", padx=10)
        
        # Update section on right
        update_frame = ctk.CTkFrame(footer_frame, fg_color="transparent")
        update_frame.pack(side="right", padx=10)
        
        # Auto-update checkbox
        self.auto_update_var = ctk.BooleanVar(value=get_auto_check_updates())
        self.auto_update_cb = ctk.CTkCheckBox(
            update_frame, 
            text="Auto-check updates",
            variable=self.auto_update_var,
            command=self._on_auto_update_toggle,
            font=("", 11),
            checkbox_width=18,
            checkbox_height=18
        )
        self.auto_update_cb.pack(side="left", padx=(0, 10))
        
        # Check for updates button
        self.update_btn = ctk.CTkButton(
            update_frame,
            text="Check for Updates",
            width=130,
            height=28,
            font=("", 11),
            command=self._on_check_updates
        )
        self.update_btn.pack(side="left")
    
    def _on_fetch(self):
        """Handle fetch button click."""
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showerror("Error", "Please enter a URL")
            return
        
        # Find appropriate parser
        self.parser = get_parser_for_url(url)
        if not self.parser:
            messagebox.showerror("Error", f"Unsupported site. URL: {url}")
            return
        
        # Disable UI
        self.fetch_btn.configure(state="disabled")
        self.status_label.configure(text="Fetching novel info...")
        self.progress_bar.set(0)
        
        # Invalidate any cover/title threads still running from a previous fetch
        self._fetch_generation += 1
        
        # Run in thread
        thread = threading.Thread(target=self._fetch_thread, args=(url,))
        thread.daemon = True
        thread.start()
    
    def _fetch_thread(self, url: str):
        """Fetch novel info in background thread."""
        try:
            # Check if parser supports parallel fetching (faster)
            if hasattr(self.parser, 'fetch_all_parallel'):
                print(f"Fetching novel info and chapters in parallel...")
                self.after(0, lambda: self._update_status("Fetching novel info & chapters (parallel)..."))
                self.novel_info, self.chapters = self.parser.fetch_all_parallel(url)
                print(f"Got novel info: {self.novel_info.title}")
                print(f"Got {len(self.chapters)} chapters")
            else:
                # Fallback to sequential fetching
                print(f"Fetching novel info from: {url}")
                self.novel_info = self.parser.get_novel_info(url)
                print(f"Got novel info: {self.novel_info.title}")
                self.after(0, lambda: self._update_status("Fetching chapter list..."))
                
                print("Fetching chapter list...")
                self.chapters = self.parser.get_chapter_list(url)
                print(f"Got {len(self.chapters)} chapters")
            
            try:
                self.cache.put_chapter_list(url, self.chapters)
            except Exception:
                pass
            
            # Update UI in main thread
            self.after(0, self._update_chapter_list)
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            error_msg = f"Failed to fetch: {str(e)}"
            self.after(0, lambda msg=error_msg: self._show_error(msg))
        finally:
            self.after(0, lambda: self.fetch_btn.configure(state="normal"))
    
    def _setup_chapter_tree_style(self):
        """Dark ttk theme so the chapter list matches CustomTkinter chrome."""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Chapter.Treeview",
            background="#2b2b2b",
            foreground="#e8e8e8",
            fieldbackground="#2b2b2b",
            borderwidth=0,
            rowheight=22,
            font=("", 11),
        )
        style.map(
            "Chapter.Treeview",
            background=[("selected", "#1f6aa5")],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Chapter.Treeview.Heading",
            background="#333333",
            foreground="#e8e8e8",
            relief="flat",
        )
    
    def _clear_chapter_tree(self):
        """Drop Treeview rows so resize stays fast in other modes."""
        tree = getattr(self, "chapter_tree", None)
        if tree is None:
            return
        children = tree.get_children()
        if children:
            tree.delete(*children)
        self._update_selected_count()
    
    def _populate_chapter_tree(self, *, select_all: bool = True):
        """Fill the chapter Treeview from self.chapters."""
        tree = self.chapter_tree
        tree.delete(*tree.get_children())
        for idx, chapter in enumerate(self.chapters):
            title = chapter.title
            if len(title) > 60:
                title = title[:57] + "..."
            tree.insert("", "end", iid=str(idx), text=f"{idx + 1}. {title}")
        if select_all and self.chapters:
            tree.selection_set(tree.get_children())
        self._update_selected_count()
    
    def _selected_chapter_indices(self) -> List[int]:
        """Indices of selected chapters (sorted)."""
        out = []
        for iid in self.chapter_tree.selection():
            try:
                out.append(int(iid))
            except ValueError:
                continue
        out.sort()
        return out
    
    def _tree_select_all_event(self, event=None):
        self._select_all()
        return "break"
    
    def _update_chapter_list(self):
        """Update UI with fetched chapters."""
        if not self.novel_info:
            return
        
        self.title_label.configure(text=self.novel_info.title)
        self.author_label.configure(text=self.novel_info.author)
        self.chapters_label.configure(text=str(len(self.chapters)))
        
        if self.novel_info.cover_url:
            thread = threading.Thread(
                target=self._load_cover,
                args=(self.novel_info.cover_url, self._fetch_generation)
            )
            thread.daemon = True
            thread.start()
        
        thread = threading.Thread(
            target=self._translate_title,
            args=(self.novel_info.title, self._fetch_generation)
        )
        thread.daemon = True
        thread.start()
        
        self._list_generation += 1
        self.download_btn.configure(state="disabled")
        self._update_status(f"Loading {len(self.chapters)} chapters...")
        self._populate_chapter_tree(select_all=True)
        self.download_btn.configure(state="normal")
        self._update_status(f"Found {len(self.chapters)} chapters. Ready to download.")
    
    def _update_selected_count(self):
        """Update the selected count label."""
        try:
            count = len(self.chapter_tree.selection())
        except Exception:
            count = 0
        self.selected_label.configure(text=f"Selected: {count}")
    
    def _select_all(self):
        children = self.chapter_tree.get_children()
        if children:
            self.chapter_tree.selection_set(children)
        self._update_selected_count()
    
    def _select_none(self):
        children = self.chapter_tree.get_children()
        if children:
            self.chapter_tree.selection_remove(children)
        self._update_selected_count()
    
    def _invert_selection(self):
        children = self.chapter_tree.get_children()
        if not children:
            return
        selected = set(self.chapter_tree.selection())
        to_select = [c for c in children if c not in selected]
        self.chapter_tree.selection_set(to_select)
        self._update_selected_count()
    
    def _select_range(self):
        """Select only the chapters in the From-To range (1-based, inclusive)."""
        children = self.chapter_tree.get_children()
        if not children:
            return
        n = len(children)
        try:
            start = int(self.range_from_entry.get())
        except ValueError:
            start = 1
        try:
            end = int(self.range_to_entry.get())
        except ValueError:
            end = n
        start = max(1, min(start, n))
        end = max(start, min(end, n))
        self.chapter_tree.selection_set(children[start - 1:end])
        self._update_selected_count()
    
    # ------------------------------------------------------------------
    # Progress / status coalescing (keeps UI smooth under load)
    # ------------------------------------------------------------------
    
    def _ui_progress(
        self,
        fraction: Optional[float] = None,
        status: Optional[str] = None,
        *,
        force: bool = False,
    ):
        """Thread-safe: queue progress/status; coalesce to ~10 Hz unless force."""
        self.after(0, lambda: self._coalesce_progress(fraction, status, force))
    
    def _coalesce_progress(
        self,
        fraction: Optional[float],
        status: Optional[str],
        force: bool,
    ):
        if fraction is not None:
            self._pending_progress = fraction
        if status is not None:
            self._pending_status = status
        if force:
            self._flush_progress()
            return
        if self._progress_flush_scheduled:
            return
        elapsed_ms = (time.monotonic() - self._last_progress_flush) * 1000.0
        delay = max(0, int(self._PROGRESS_MIN_INTERVAL_MS - elapsed_ms))
        self._progress_flush_scheduled = True
        self.after(delay, self._flush_progress)
    
    def _flush_progress(self):
        self._progress_flush_scheduled = False
        self._last_progress_flush = time.monotonic()
        if self._pending_progress is not None:
            try:
                self.progress_bar.set(self._pending_progress)
            except Exception:
                pass
            self._pending_progress = None
        if self._pending_status is not None:
            try:
                self.status_label.configure(text=self._pending_status)
            except Exception:
                pass
            self._pending_status = None
    
    # ------------------------------------------------------------------
    # Output folder
    # ------------------------------------------------------------------
    
    def _choose_output_dir(self):
        """Let the user pick a custom output folder."""
        initial = self.output_dir or str(self._get_downloads_folder())
        chosen = filedialog.askdirectory(initialdir=initial, title="Choose output folder")
        if chosen:
            self.output_dir = chosen
            self._update_output_dir_label()
            self._save_settings()
    
    def _reset_output_dir(self):
        """Reset to ~/.huaepub/books."""
        self.output_dir = ''
        self._update_output_dir_label()
        self._save_settings()
    
    def _update_output_dir_label(self):
        if self.output_dir:
            self.output_dir_label.configure(text=self.output_dir, text_color="white")
        else:
            self.output_dir_label.configure(
                text=f"{get_default_books_dir()} (default)", text_color="gray"
            )
    
    def _on_download(self):
        """Handle download button click."""
        if not self.chapters or not self.novel_info:
            return
        
        indices = self._selected_chapter_indices()
        selected_chapters = [self.chapters[i] for i in indices if 0 <= i < len(self.chapters)]
        
        if not selected_chapters:
            messagebox.showwarning("Warning", "Please select at least one chapter")
            return
        
        # Use translated title if available, otherwise original
        title_for_filename = self.translated_title if self.translated_title else self.novel_info.title
        
        downloads_dir = self._get_downloads_folder()
        # Overwrite same novel file when re-downloading / updating
        preferred = ""
        if self.novel_info and self.novel_info.source_url:
            entry = self.library_store.get_library_entry(self.novel_info.source_url)
            if entry:
                preferred = entry.epub_filename or entry.output_path or ""
        output_path = self._epub_path(
            downloads_dir,
            title_for_filename,
            preferred_name=Path(preferred).name if preferred else "",
            preferred_path=preferred,
        )
        
        print(f"Auto-saving to: {output_path}")
        
        # Persist current options before starting
        self._save_settings()
        
        # Local resume snapshot (chapter HTML lives in cache.db; not synced to Drive)
        self._set_active_job({
            "kind": "single",
            "status": "running",
            "source_url": self.novel_info.source_url or "",
            "title": self.novel_info.title or "",
            "translated_title": self.translated_title or "",
            "info": novel_info_to_job(self.novel_info),
            "chapters": chapters_to_job(selected_chapters),
            "output_path": output_path,
            "options": self._download_options_snapshot(),
        })
        
        # Start download
        self.is_downloading = True
        self.cancel_requested = False
        self.is_paused = False
        self.download_btn.configure(state="disabled")
        self._set_download_controls_active(True)
        self.fetch_btn.configure(state="disabled")
        
        thread = threading.Thread(
            target=self._download_thread,
            args=(selected_chapters, output_path)
        )
        thread.daemon = True
        thread.start()
    
    def _get_downloads_folder(self) -> Path:
        """Get the output folder: user-chosen folder if set, else ~/.huaepub/books."""
        custom = (self.output_dir or '').strip()
        if custom:
            path = Path(custom)
            if path.exists():
                return path
            try:
                path.mkdir(parents=True, exist_ok=True)
                return path
            except Exception:
                print(f"Warning: chosen output folder unavailable: {custom}")
        return get_default_books_dir()
    
    def _epub_path(
        self,
        folder: Path,
        title: str,
        *,
        preferred_name: str = "",
        preferred_path: str = "",
    ) -> str:
        """
        Canonical EPUB path for a novel. Rebuilds/updates overwrite the same file
        (no ' (1)' copies) so local books and Drive stay in sync by filename.
        """
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)

        name = (preferred_name or "").strip()
        if not name and preferred_path:
            try:
                name = Path(preferred_path).name
            except Exception:
                name = ""
        if name and name.lower().endswith(".epub"):
            # Strip accidental ' (N)' suffixes from older unique-path runs
            stem = Path(name).stem
            stem = re.sub(r" \(\d+\)$", "", stem)
            name = f"{stem}.epub"
        else:
            name = f"{safe_filename(title)}.epub"

        return str(folder / name)

    def _unique_epub_path(self, folder: Path, title: str) -> str:
        """Back-compat alias: same as _epub_path (overwrites)."""
        return self._epub_path(folder, title)
    
    def _record_successful_download(
        self,
        info: NovelInfo,
        chapters: List[Chapter],
        translated_title: Optional[str],
        output_path: str,
    ):
        """Update history + library after a successful EPUB build."""
        if not info:
            return
        display_title = translated_title or info.title
        last_ch = chapters[-1] if chapters else None
        try:
            epub_name = Path(output_path).name if output_path else ''
            self.library_store.add_history(
                source_url=info.source_url,
                title=info.title,
                translated_title=display_title,
                author=info.author,
                chapter_count=len(chapters),
                output_path=output_path,
            )
            self.library_store.upsert_library(
                source_url=info.source_url,
                title=info.title,
                translated_title=display_title,
                author=info.author,
                cover_url=info.cover_url or '',
                chapter_count=len(chapters),
                last_chapter_url=last_ch.url if last_ch else '',
                last_chapter_title=last_ch.title if last_ch else '',
                output_path=output_path,
                epub_filename=epub_name,
            )
        except Exception as e:
            print(f"Warning: failed to update library/history: {e}")
        if self.library_mode:
            self.after(0, self._refresh_library_ui)
        # Optional Drive sync (library push + EPUB upload)
        self._schedule_drive_push_after_download(
            info.source_url if info else '',
            output_path,
        )
    
    def _download_chapters_with_cache(
        self,
        parser,
        chapters: List[Chapter],
        book_key: str,
        set_status,
        set_progress,
    ) -> List[str]:
        """
        Download chapter contents sequentially with:
        - persistent chapter cache (resume support - cached chapters are
          instant and skip the rate-limit delay)
        - ETA in the status text
        - an end-of-run retry pass for failed chapters
        
        set_status(text) / set_progress(fraction 0..1) are called from this
        worker thread and must be thread-safe.
        
        Returns titles of chapters that still failed after the retry pass.
        Raises _DownloadCancelled if the user cancels.
        """
        total = len(chapters)
        delay = parser.request_delay
        use_cache = bool(self.use_cache_var.get())
        failed: List[Chapter] = []
        start_time = time.monotonic()
        paused_for = 0.0  # exclude pause time from ETA
        
        for idx, chapter in enumerate(chapters):
            paused_for += self._wait_while_paused(set_status)
            if self.cancel_requested:
                raise _DownloadCancelled()
            
            set_progress((idx + 1) / total)
            
            eta_text = ""
            if idx >= 3:
                elapsed = max(0.001, (time.monotonic() - start_time) - paused_for)
                avg = elapsed / idx
                eta_text = f"  (ETA {format_eta(avg * (total - idx))})"
            
            # Cached chapters are free - no fetch, no delay
            cached = self.cache.get_chapter(chapter.url) if use_cache else None
            if cached:
                chapter.content = cached
                set_status(f"Chapter [{idx+1}/{total}] from cache{eta_text}")
                self._persist_active_job()
                continue
            
            set_status(f"Downloading [{idx+1}/{total}]: {chapter.title[:40]}{eta_text}")
            try:
                chapter.content = parser.get_chapter_content(chapter)
                if use_cache:
                    self.cache.put_chapter(book_key, chapter.url, chapter.title, chapter.content)
            except Exception as e:
                # A single failed chapter should not abort the whole download;
                # it gets another chance in the retry pass below
                print(f"  Chapter [{idx+1}/{total}] failed: {chapter.title}: {e}")
                failed.append(chapter)
            
            self._persist_active_job()
            if idx < total - 1:
                paused_for += self._interruptible_delay(delay, set_status)
        
        # End-of-run retry pass: transient failures usually succeed here
        still_failed: List[str] = []
        if failed:
            set_status(f"Retrying {len(failed)} failed chapter(s)...")
            print(f"Retrying {len(failed)} failed chapter(s)...")
            for chapter in failed:
                paused_for += self._wait_while_paused(set_status)
                if self.cancel_requested:
                    raise _DownloadCancelled()
                paused_for += self._interruptible_delay(delay, set_status)
                try:
                    chapter.content = parser.get_chapter_content(chapter)
                    if use_cache:
                        self.cache.put_chapter(book_key, chapter.url, chapter.title, chapter.content)
                    print(f"  Retry succeeded: {chapter.title}")
                except Exception as e:
                    print(f"  Retry failed: {chapter.title}: {e}")
                    chapter.content = "<p>[Chapter could not be downloaded from the source site.]</p>"
                    still_failed.append(chapter.title)
        
        return still_failed
    
    def _download_thread(self, chapters: List[Chapter], output_path: str):
        """Download and build EPUB in background thread."""
        try:
            book_key = self.novel_info.source_url if self.novel_info else ''
            
            # Phase 1: Download chapter content (first half of the progress bar)
            try:
                failed_chapters = self._download_chapters_with_cache(
                    self.parser, chapters, book_key,
                    set_status=lambda s: self._ui_progress(status=s),
                    set_progress=lambda f: self._ui_progress(fraction=f / 2),
                )
            except _DownloadCancelled:
                self._ui_progress(status="Cancelled", force=True)
                return
            
            # Phase 2: Build EPUB
            self._ui_progress(status="Building EPUB...", force=True)
            
            # Create cleaner and translator
            cleaner = ContentCleaner() if self.clean_var.get() else None
            translator = None
            
            if self.translate_var.get():
                translator = self._make_translator(self._get_workers())

            # Build EPUB
            if translator:
                builder = TranslatedEPUBBuilder(cleaner=cleaner, translator=translator, image_cache=self.cache)
                
                def progress_cb(current, total_steps, status):
                    if self.cancel_requested:
                        translator.cancel()
                        return
                    progress = 0.5 + (current / total_steps) * 0.5
                    self._ui_progress(progress, status)
                
                builder.build_with_translation(
                    self.novel_info,
                    chapters,
                    output_path,
                    progress_cb
                )
            else:
                builder = EPUBBuilder(cleaner=cleaner, image_cache=self.cache)
                
                def progress_cb(current, total_steps, status):
                    progress = 0.5 + (current / total_steps) * 0.5
                    self._ui_progress(progress, status)
                
                builder.build(
                    self.novel_info,
                    chapters,
                    output_path,
                    progress_cb
                )
            
            # Done
            self._record_successful_download(
                self.novel_info, chapters, self.translated_title, output_path
            )
            self._clear_active_job()
            
            success_msg = f"EPUB saved to:\n{output_path}"
            if failed_chapters:
                shown = "\n".join(f"  • {t[:50]}" for t in failed_chapters[:10])
                if len(failed_chapters) > 10:
                    shown += f"\n  ... and {len(failed_chapters) - 10} more"
                success_msg += (
                    f"\n\nWarning: {len(failed_chapters)} chapter(s) could not be "
                    f"downloaded and contain placeholder text:\n{shown}"
                )
            
            title_note = self.translated_title or (self.novel_info.title if self.novel_info else "Novel")
            notify("Download complete", f"{title_note}\nSaved to {Path(output_path).name}")
            
            self._ui_progress(1.0, f"Done! Saved to: {output_path}", force=True)
            self.after(0, lambda m=success_msg: messagebox.showinfo("Success", m))
            
        except Exception as e:
            # Keep the local job so the user can resume after fixing the issue
            try:
                self._persist_active_job(force=True)
                job = self._active_job
                if job:
                    self.after(0, lambda j=job: self._show_resume_banner(j))
            except Exception:
                pass
            error_msg = f"Download failed: {str(e)}"
            self.after(0, lambda msg=error_msg: self._show_error(msg))
        finally:
            self.is_downloading = False
            self.is_paused = False
            self.after(0, lambda: self.download_btn.configure(state="normal"))
            self.after(0, lambda: self._set_download_controls_active(False))
            self.after(0, lambda: self.fetch_btn.configure(state="normal"))
    
    def _set_download_controls_active(self, active: bool):
        """Enable/disable Pause + Cancel for an in-flight download."""
        try:
            if active:
                self.pause_btn.configure(
                    state="normal", text="Pause",
                    fg_color="gray40", hover_color="gray30",
                )
                self.cancel_btn.configure(state="normal")
            else:
                self.is_paused = False
                self.pause_btn.configure(
                    state="disabled", text="Pause",
                    fg_color="gray40", hover_color="gray30",
                )
                self.cancel_btn.configure(state="disabled")
        except Exception:
            pass
    
    def _on_pause_toggle(self):
        """Pause or resume the current download (chapters already fetched stay cached)."""
        if not self.is_downloading:
            return
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.pause_btn.configure(text="Resume", fg_color="#2B7A3E", hover_color="#236332")
            self._update_status("Paused — click Resume to continue (safe to close the app)")
            self._persist_active_job(force=True)
        else:
            self.pause_btn.configure(text="Pause", fg_color="gray40", hover_color="gray30")
            self._update_status("Resuming…")
            self._persist_active_job(force=True)
    
    def _wait_while_paused(self, set_status=None) -> float:
        """
        Block the worker while paused. Returns seconds spent paused.
        Raises _DownloadCancelled if the user cancels while paused.
        """
        if not self.is_paused:
            return 0.0
        if set_status:
            set_status("Paused — click Resume to continue")
        else:
            self.after(0, lambda: self._update_status("Paused — click Resume to continue"))
        t0 = time.monotonic()
        while self.is_paused:
            if self.cancel_requested:
                raise _DownloadCancelled()
            time.sleep(0.2)
        return time.monotonic() - t0
    
    def _interruptible_delay(self, seconds: float, set_status=None) -> float:
        """Sleep for request_delay, but wake for pause/cancel. Returns pause time."""
        paused_total = 0.0
        end = time.monotonic() + max(0.0, seconds)
        while True:
            paused_total += self._wait_while_paused(set_status)
            if self.cancel_requested:
                raise _DownloadCancelled()
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.2, remaining))
        return paused_total
    
    def _on_cancel(self):
        """Handle cancel button click."""
        self.cancel_requested = True
        self.is_paused = False  # unblock pause wait so cancel can proceed
        self._clear_active_job()
        try:
            self.pause_btn.configure(text="Pause", fg_color="gray40", hover_color="gray30")
        except Exception:
            pass
        self._update_status("Cancelling...")
    
    # ------------------------------------------------------------------
    # Local download job (resume after close / reboot — not Drive-synced)
    # ------------------------------------------------------------------
    
    def _download_options_snapshot(self) -> dict:
        return {
            "translate": bool(self.translate_var.get()),
            "clean": bool(self.clean_var.get()),
            "workers": self._get_workers(),
            "use_cache": bool(self.use_cache_var.get()),
            "translation_backend": (
                "libretranslate"
                if self.backend_menu.get() == "LibreTranslate"
                else "google"
            ),
            "output_dir": self.output_dir or "",
        }
    
    def _apply_download_options(self, options: Optional[dict]):
        if not options:
            return
        try:
            if "translate" in options:
                self.translate_var.set(bool(options["translate"]))
            if "clean" in options:
                self.clean_var.set(bool(options["clean"]))
            if "use_cache" in options:
                self.use_cache_var.set(bool(options.get("use_cache", True)))
            if "workers" in options:
                self.workers_entry.delete(0, "end")
                self.workers_entry.insert(0, str(int(options["workers"])))
            if "translation_backend" in options:
                self.backend_menu.set(
                    "LibreTranslate"
                    if options["translation_backend"] == "libretranslate"
                    else "Google"
                )
            if "output_dir" in options and options["output_dir"] is not None:
                self.output_dir = options["output_dir"] or ""
                self._update_output_dir_label()
        except Exception as e:
            print(f"Warning: could not restore download options: {e}")
    
    def _persist_active_job(self, force: bool = False):
        """Write _active_job to disk (throttled unless force)."""
        if not self._active_job:
            return
        self._job_save_counter += 1
        if not force and self._job_save_counter % 10 != 0:
            return
        self._active_job["status"] = "paused" if self.is_paused else "running"
        save_job(self._active_job, self.data_dir)
    
    def _set_active_job(self, job: dict):
        self._active_job = job
        self._job_save_counter = 0
        self._hide_resume_banner()
        save_job(job, self.data_dir)
    
    def _clear_active_job(self):
        self._active_job = None
        self._job_save_counter = 0
        clear_job(self.data_dir)
        self._hide_resume_banner()
    
    def _hide_resume_banner(self):
        try:
            self.resume_frame.grid_remove()
        except Exception:
            pass
    
    def _show_resume_banner(self, job: dict):
        urls = job_chapter_urls(job)
        cached = self.cache.count_cached_urls(urls) if urls else 0
        total = len(urls)
        title = job_display_title(job)
        if total:
            detail = f"{cached}/{total} chapters cached"
        else:
            detail = "cached chapters will be reused"
        self.resume_label.configure(
            text=f"Incomplete download: {title}\n{detail} — resume anytime (saved locally, not on Drive)."
        )
        self.resume_frame.grid(row=0, column=0, columnspan=3, padx=8, pady=(8, 0), sticky="ew")
    
    def _check_resume_job(self):
        """On startup, surface a previously unfinished download."""
        if self.is_downloading:
            return
        job = load_job(self.data_dir)
        if not job:
            return
        self._active_job = job
        self._show_resume_banner(job)
        self._update_status(f"Incomplete download ready to resume: {job_display_title(job)}")
    
    def _on_discard_job(self):
        if self.is_downloading:
            return
        if not messagebox.askyesno(
            "Discard incomplete download",
            "Remove the saved resume point?\n\n"
            "Cached chapter text stays on this PC and can still speed up a new download.",
        ):
            return
        self._clear_active_job()
        self._update_status("Resume point discarded")
    
    def _on_resume_job(self):
        if self.is_downloading:
            return
        job = self._active_job or load_job(self.data_dir)
        if not job:
            self._hide_resume_banner()
            return
        kind = job.get("kind")
        try:
            if kind == "single":
                self._resume_single_job(job)
            elif kind == "multi":
                self._resume_multi_job(job)
            elif kind == "library_update":
                self._resume_library_update_job(job)
            elif kind == "library_update_all":
                self._resume_library_update_all_job(job)
            else:
                messagebox.showerror("Resume", f"Unknown job type: {kind}")
                self._clear_active_job()
        except Exception as e:
            import traceback
            traceback.print_exc()
            messagebox.showerror("Resume failed", str(e))
    
    def _resume_single_job(self, job: dict):
        self.mode_switch.set("Single")
        self._on_mode_change("Single")
        self._apply_download_options(job.get("options"))
        
        info = novel_info_from_job(job.get("info"))
        chapters = chapters_from_job(job.get("chapters") or [])
        source_url = (job.get("source_url") or (info.source_url if info else "")).strip()
        if not chapters or not source_url:
            raise Exception("Saved download is missing chapter list or URL")
        
        parser = get_parser_for_url(source_url)
        if not parser:
            raise Exception(f"Unsupported site:\n{source_url}")
        
        if not info:
            info = NovelInfo(title=job.get("title") or "Untitled", source_url=source_url)
        elif not info.source_url:
            info.source_url = source_url
        
        self.parser = parser
        self.novel_info = info
        self.chapters = chapters
        self.translated_title = job.get("translated_title") or None
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, source_url)
        self.title_label.configure(text=info.title)
        self.author_label.configure(text=info.author or "-")
        self.chapters_label.configure(text=str(len(chapters)))
        self.eng_title_label.configure(text=self.translated_title or "-")
        self._populate_chapter_tree(select_all=True)
        
        output_path = job.get("output_path") or self._epub_path(
            self._get_downloads_folder(),
            self.translated_title or info.title,
        )
        self._set_active_job(job)
        self.is_downloading = True
        self.cancel_requested = False
        self.is_paused = False
        self.download_btn.configure(state="disabled")
        self._set_download_controls_active(True)
        self.fetch_btn.configure(state="disabled")
        self._update_status("Resuming download…")
        
        thread = threading.Thread(
            target=self._download_thread,
            args=(chapters, output_path),
            daemon=True,
        )
        thread.start()
    
    def _resume_multi_job(self, job: dict):
        self.mode_switch.set("Multi")
        self._on_mode_change("Multi")
        self._apply_download_options(job.get("options"))
        
        novels = []
        for item in job.get("novels") or []:
            if item.get("done"):
                continue
            source_url = (item.get("source_url") or item.get("url") or "").strip()
            chapters = chapters_from_job(item.get("chapters") or [])
            info = novel_info_from_job(item.get("info"))
            if not source_url or not chapters:
                continue
            parser = get_parser_for_url(source_url)
            if not parser:
                continue
            if not info:
                info = NovelInfo(
                    title=item.get("title") or "Untitled",
                    source_url=source_url,
                )
            novels.append({
                "url": source_url,
                "parser": parser,
                "info": info,
                "chapters": chapters,
                "status": "fetched",
                "translated_title": item.get("translated_title") or "",
            })
        
        if not novels:
            self._clear_active_job()
            raise Exception("No unfinished novels left in the saved multi-download")
        
        # Rebuild result rows for the remaining queue
        for w in list(self.multi_result_labels):
            try:
                w["frame"].destroy()
            except Exception:
                pass
        self.multi_result_labels = []
        self.multi_novels = novels
        for idx, novel in enumerate(novels):
            self._multi_create_result_row(idx, novel["url"])
            title = novel.get("translated_title") or novel["info"].title
            self.multi_result_labels[idx]["title"].configure(text=title)
            self.multi_result_labels[idx]["chapters"].configure(
                text=f"{len(novel['chapters'])} ch."
            )
            self.multi_result_labels[idx]["status"].configure(
                text="Queued", text_color="gray"
            )
        
        self._set_active_job(job)
        self.is_downloading = True
        self.cancel_requested = False
        self.is_paused = False
        self.multi_download_btn.configure(state="disabled")
        self.multi_fetch_btn.configure(state="disabled")
        self.multi_clear_btn.configure(state="disabled")
        self._set_download_controls_active(True)
        self.fetch_btn.configure(state="disabled")
        self.mode_switch.configure(state="disabled")
        self._update_status("Resuming multi-download…")
        
        thread = threading.Thread(
            target=self._multi_download_thread,
            args=(novels,),
            daemon=True,
        )
        thread.start()
    
    def _resume_library_update_job(self, job: dict):
        self.mode_switch.set("Library")
        self._on_mode_change("Library")
        self._apply_download_options(job.get("options"))
        
        source_url = (job.get("source_url") or "").strip()
        entry = self.library_store.get_library_entry(source_url) if source_url else None
        parser = get_parser_for_url(source_url) if source_url else None
        chapters = chapters_from_job(job.get("chapters") or [])
        info = novel_info_from_job(job.get("info"))
        if not parser or not chapters or not info:
            raise Exception("Saved library update is incomplete — try Update again from the library")
        
        output_path = job.get("output_path") or ""
        translated_title = job.get("translated_title") or (
            entry.translated_title if entry else None
        ) or info.title
        
        self._set_active_job(job)
        self.is_downloading = True
        self.cancel_requested = False
        self.is_paused = False
        self._set_download_controls_active(True)
        self.mode_switch.configure(state="disabled")
        self.library_refresh_btn.configure(state="disabled")
        self.progress_bar.set(0)
        self._update_status(f"Resuming library update: {translated_title[:40]}…")
        
        thread = threading.Thread(
            target=self._library_update_resume_thread,
            args=(entry, parser, info, chapters, output_path, translated_title),
            daemon=True,
        )
        thread.start()
    
    def _library_update_resume_thread(
        self, entry, parser, info, chapters, output_path, translated_title
    ):
        """Continue a saved library update using stored chapter list + cache."""
        try:
            book_key = info.source_url or (entry.source_url if entry else "")
            if not output_path:
                output_path = self._epub_path(
                    self._get_downloads_folder(),
                    translated_title,
                    preferred_name=(entry.epub_filename if entry else "") or "",
                    preferred_path=(entry.output_path if entry else "") or "",
                )
            
            failed_titles = self._download_chapters_with_cache(
                parser, chapters, book_key,
                set_status=lambda s: self._ui_progress(status=s),
                set_progress=lambda f: self._ui_progress(fraction=f / 2),
            )
            
            cleaner = ContentCleaner() if self.clean_var.get() else None
            translator = (
                self._make_translator(self._get_workers())
                if self.translate_var.get() else None
            )
            if translator:
                builder = TranslatedEPUBBuilder(
                    cleaner=cleaner, translator=translator, image_cache=self.cache
                )
                
                def progress_cb(current, total_steps, status):
                    if self.cancel_requested:
                        translator.cancel()
                        return
                    self._ui_progress(0.5 + (current / total_steps) * 0.5, status)
                
                builder.build_with_translation(info, chapters, output_path, progress_cb)
            else:
                builder = EPUBBuilder(cleaner=cleaner, image_cache=self.cache)
                
                def progress_cb(current, total_steps, status):
                    self._ui_progress(0.5 + (current / total_steps) * 0.5, status)
                
                builder.build(info, chapters, output_path, progress_cb)
            
            self._record_successful_download(info, chapters, translated_title, output_path)
            self._clear_active_job()
            display = translated_title or info.title
            msg = f"Updated!\n{display}\n→ {output_path}"
            if failed_titles:
                msg += f"\n\n{len(failed_titles)} chapter(s) failed (placeholders)."
            notify("Library update complete", f"{display}")
            self._ui_progress(1.0, f"Updated → {output_path}", force=True)
            self.after(0, lambda m=msg: messagebox.showinfo("Library Updated", m))
        except _DownloadCancelled:
            self._ui_progress(status="Cancelled", force=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.after(0, lambda msg=str(e): self._show_error(f"Library update failed: {msg}"))
        finally:
            self.is_downloading = False
            self.is_paused = False
            self.after(0, lambda: self._set_download_controls_active(False))
            self.after(0, lambda: self.mode_switch.configure(state="normal"))
            self.after(0, lambda: self.library_refresh_btn.configure(state="normal"))
            self.after(0, lambda: self.library_check_btn.configure(state="normal"))
            self.after(0, self._refresh_library_ui)
            self.after(0, self._update_library_update_all_btn)
    
    def _resume_library_update_all_job(self, job: dict):
        self.mode_switch.set("Library")
        self._on_mode_change("Library")
        self._apply_download_options(job.get("options"))
        
        pending_urls = [
            e.get("source_url") for e in (job.get("entries") or [])
            if e.get("source_url") and not e.get("done")
        ]
        entries = []
        for url in pending_urls:
            entry = self.library_store.get_library_entry(url)
            if entry:
                entries.append(entry)
        if not entries:
            self._clear_active_job()
            raise Exception("No unfinished library novels left to update")
        
        self._set_active_job(job)
        self.is_downloading = True
        self.cancel_requested = False
        self.is_paused = False
        self._set_download_controls_active(True)
        self.mode_switch.configure(state="disabled")
        self.library_refresh_btn.configure(state="disabled")
        self.library_check_btn.configure(state="disabled")
        self.library_update_all_btn.configure(state="disabled")
        self.progress_bar.set(0)
        self._update_status(f"Resuming Update All ({len(entries)} remaining)…")
        
        thread = threading.Thread(
            target=self._library_update_all_thread,
            args=(entries,),
            daemon=True,
        )
        thread.start()
    
    # ------------------------------------------------------------------
    # Multi-download mode
    # ------------------------------------------------------------------
    
    def _on_mode_change(self, value: str):
        """Toggle between Single, Multi, and Library modes."""
        if self.is_downloading:
            # Revert to current mode
            if self.library_mode:
                self.mode_switch.set("Library")
            elif self.multi_mode:
                self.mode_switch.set("Multi")
            else:
                self.mode_switch.set("Single")
            return
        
        self.multi_mode = (value == "Multi")
        self.library_mode = (value == "Library")
        
        # Hide all mode panels first
        self.single_url_frame.grid_remove()
        self.info_frame.grid_remove()
        self.list_frame.grid_remove()
        self.multi_frame.grid_remove()
        self.library_frame.grid_remove()
        self.download_btn.pack_forget()
        
        if self.library_mode:
            self._clear_chapter_tree()
            self.library_frame.grid(row=2, column=0, rowspan=2, padx=10, pady=5, sticky="nsew")
            self._refresh_library_ui()
            self._update_drive_status_label()
            if self.drive_enabled_var.get():
                self._schedule_drive_sync(silent=True)
            self._schedule_library_check(reason="library_tab")
        elif self.multi_mode:
            self._clear_chapter_tree()
            self.multi_frame.grid(row=2, column=0, rowspan=2, padx=10, pady=5, sticky="nsew")
        else:
            self.single_url_frame.grid()
            self.info_frame.grid()
            self.list_frame.grid()
            self.download_btn.pack(side="left", padx=5)
            # Restore chapter rows if we already fetched (cleared when leaving Single)
            if self.chapters and not self.chapter_tree.get_children():
                self._populate_chapter_tree(select_all=True)
                self.download_btn.configure(state="normal")
    
    def _multi_clear_urls(self):
        """Clear the multi-mode URL block."""
        self.multi_url_text.delete("1.0", "end")
    
    def _multi_get_urls(self) -> List[str]:
        """Parse unique URLs from the multi-mode text block."""
        return extract_urls(self.multi_url_text.get("1.0", "end"))
    
    def _multi_append_urls(self, urls: List[str]) -> List[str]:
        """Append URLs that aren't already in the block. Returns newly added URLs."""
        existing = set(self._multi_get_urls())
        added = []
        for url in urls:
            if url not in existing:
                added.append(url)
                existing.add(url)
        if not added:
            return []
        current = self.multi_url_text.get("1.0", "end").strip()
        block = ("\n" if current else "") + "\n".join(added)
        self.multi_url_text.insert("end", block)
        return added
    
    def _on_multi_fetch(self):
        """Fetch info for all URLs in multi mode."""
        urls = self._multi_get_urls()
        if not urls:
            messagebox.showerror("Error", "Please paste at least one novel URL.")
            return
        
        # Validate all URLs have parsers
        parsers_list = []
        for url in urls:
            parser = get_parser_for_url(url)
            if not parser:
                messagebox.showerror("Error", f"Unsupported site:\n{url}")
                return
            parsers_list.append((url, parser))
        
        # Clear old results
        self.multi_novels.clear()
        for widget in self.multi_results_frame.winfo_children():
            widget.destroy()
        self.multi_result_labels.clear()
        
        # Create result rows
        for idx, (url, parser) in enumerate(parsers_list):
            self.multi_novels.append({
                'url': url, 'parser': parser,
                'info': None, 'chapters': [],
                'status': 'pending', 'translated_title': None
            })
            self._multi_create_result_row(idx, url)
        
        # Disable UI during fetch
        self.multi_fetch_btn.configure(state="disabled", text="Fetching...")
        self.multi_download_btn.configure(state="disabled")
        self.multi_clear_btn.configure(state="disabled")
        self.mode_switch.configure(state="disabled")
        self.progress_bar.set(0)
        self._update_status(f"Fetching {len(parsers_list)} novel(s)...")
        
        thread = threading.Thread(target=self._multi_fetch_thread)
        thread.daemon = True
        thread.start()
    
    def _multi_create_result_row(self, idx: int, url: str):
        """Create a result row in the multi results panel."""
        row_frame = ctk.CTkFrame(self.multi_results_frame)
        row_frame.pack(fill="x", padx=5, pady=3)
        row_frame.grid_columnconfigure(1, weight=1)
        
        num_label = ctk.CTkLabel(row_frame, text=f"{idx + 1}.", width=25, font=("", 12))
        num_label.grid(row=0, column=0, padx=(8, 4), pady=8, sticky="w")
        
        title_label = ctk.CTkLabel(
            row_frame, text=url[:60] + ("..." if len(url) > 60 else ""),
            font=("", 12), anchor="w"
        )
        title_label.grid(row=0, column=1, padx=4, pady=8, sticky="w")
        
        chapters_label = ctk.CTkLabel(row_frame, text="", width=80, font=("", 11), text_color="gray")
        chapters_label.grid(row=0, column=2, padx=4, pady=8)
        
        status_label = ctk.CTkLabel(row_frame, text="Pending", width=90, font=("", 11), text_color="gray")
        status_label.grid(row=0, column=3, padx=(4, 8), pady=8)
        
        self.multi_result_labels.append({
            'frame': row_frame, 'title': title_label,
            'chapters': chapters_label, 'status': status_label
        })
    
    def _multi_fetch_thread(self):
        """Fetch all novels sequentially in background."""
        total = len(self.multi_novels)
        
        for idx, novel in enumerate(self.multi_novels):
            self.after(0, lambda i=idx: self.multi_result_labels[i]['status'].configure(
                text="Fetching...", text_color="orange"
            ))
            self.after(0, lambda i=idx, t=total: self.progress_bar.set((i) / t))
            self.after(0, lambda i=idx, t=total: self._update_status(
                f"Fetching novel {i + 1}/{t}..."
            ))
            
            try:
                parser = novel['parser']
                url = novel['url']
                
                if hasattr(parser, 'fetch_all_parallel'):
                    info, chapters = parser.fetch_all_parallel(url)
                else:
                    info = parser.get_novel_info(url)
                    chapters = parser.get_chapter_list(url)
                
                novel['info'] = info
                novel['chapters'] = chapters
                novel['status'] = 'fetched'
                try:
                    self.cache.put_chapter_list(url, chapters)
                except Exception:
                    pass
                
                # Translate title
                try:
                    translator = self._make_translator(1)
                    translated = translator.translate_text(info.title)
                    novel['translated_title'] = translated if translated and translated != info.title else info.title
                except Exception:
                    novel['translated_title'] = info.title
                
                display_title = novel['translated_title']
                if len(display_title) > 45:
                    display_title = display_title[:42] + "..."
                
                self.after(0, lambda i=idx, t=display_title: self.multi_result_labels[i]['title'].configure(text=t))
                self.after(0, lambda i=idx, c=len(chapters): self.multi_result_labels[i]['chapters'].configure(
                    text=f"{c} ch."
                ))
                self.after(0, lambda i=idx: self.multi_result_labels[i]['status'].configure(
                    text="Ready", text_color="#2B7A3E"
                ))
                
            except Exception as e:
                novel['status'] = 'error'
                err = str(e)[:30]
                self.after(0, lambda i=idx, msg=err: self.multi_result_labels[i]['status'].configure(
                    text=f"Error", text_color="red"
                ))
                self.after(0, lambda i=idx, msg=str(e): self.multi_result_labels[i]['title'].configure(
                    text=f"Error: {msg[:50]}"
                ))
        
        # Re-enable UI
        self.after(0, lambda: self.multi_fetch_btn.configure(state="normal", text="Fetch All"))
        self.after(0, lambda: self.multi_clear_btn.configure(state="normal"))
        self.after(0, lambda: self.mode_switch.configure(state="normal"))
        self.after(0, lambda: self.progress_bar.set(1.0))
        
        # Enable download if at least one novel was fetched successfully
        fetched = [n for n in self.multi_novels if n['status'] == 'fetched']
        if fetched:
            self.after(0, lambda: self.multi_download_btn.configure(state="normal"))
            self.after(0, lambda c=len(fetched), t=total: self._update_status(
                f"Fetched {c}/{t} novels. Ready to download."
            ))
        else:
            self.after(0, lambda: self._update_status("No novels fetched successfully."))
    
    def _on_multi_download(self):
        """Start downloading all fetched novels sequentially."""
        fetched = [n for n in self.multi_novels if n['status'] == 'fetched']
        if not fetched:
            return
        
        # Persist current options before starting
        self._save_settings()
        
        self._set_active_job({
            "kind": "multi",
            "status": "running",
            "options": self._download_options_snapshot(),
            "novels": [
                {
                    "source_url": (n.get("url") or (n["info"].source_url if n.get("info") else "")) or "",
                    "title": n["info"].title if n.get("info") else "",
                    "translated_title": n.get("translated_title") or "",
                    "info": novel_info_to_job(n.get("info")),
                    "chapters": chapters_to_job(n.get("chapters") or []),
                    "done": False,
                }
                for n in fetched
            ],
        })
        
        self.is_downloading = True
        self.cancel_requested = False
        self.is_paused = False
        self.multi_download_btn.configure(state="disabled")
        self.multi_fetch_btn.configure(state="disabled")
        self.multi_clear_btn.configure(state="disabled")
        self._set_download_controls_active(True)
        self.fetch_btn.configure(state="disabled")
        self.mode_switch.configure(state="disabled")
        
        thread = threading.Thread(target=self._multi_download_thread, args=(fetched,))
        thread.daemon = True
        thread.start()
    
    def _mark_multi_novel_done(self, source_url: str):
        """Flip done=True on the matching novel in the active multi job."""
        if not self._active_job or self._active_job.get("kind") != "multi":
            return
        for novel in self._active_job.get("novels") or []:
            if novel.get("source_url") == source_url or novel.get("url") == source_url:
                novel["done"] = True
                break
        self._persist_active_job(force=True)
    
    def _multi_download_thread(self, novels: list):
        """Download all novels sequentially in background."""
        total_novels = len(novels)
        results = []  # (title, path, success, error, failed_chapter_count)
        downloads_dir = self._get_downloads_folder()
        cancelled = False
        
        for novel_idx, novel in enumerate(novels):
            try:
                self._wait_while_paused()
            except _DownloadCancelled:
                results.append((novel['translated_title'] or "Unknown", "", False, "Cancelled", 0))
                cancelled = True
                break
            if self.cancel_requested:
                results.append((novel['translated_title'] or "Unknown", "", False, "Cancelled", 0))
                cancelled = True
                break
            
            info = novel['info']
            chapters = novel['chapters']
            parser = novel['parser']
            title_for_filename = novel['translated_title'] if novel['translated_title'] else info.title
            
            # Find the index in the full multi_novels list for UI updates
            try:
                full_idx = self.multi_novels.index(novel)
            except ValueError:
                full_idx = novel_idx
            
            self.after(0, lambda i=full_idx: self.multi_result_labels[i]['status'].configure(
                text="Downloading", text_color="orange"
            ) if i < len(self.multi_result_labels) else None)
            self.after(0, lambda ni=novel_idx, tn=total_novels: self._update_status(
                f"Novel {ni + 1}/{tn}: Downloading chapters..."
            ))
            
            try:
                # Generate output path (overwrite same novel file)
                preferred = ""
                if info and info.source_url:
                    lib_entry = self.library_store.get_library_entry(info.source_url)
                    if lib_entry:
                        preferred = lib_entry.epub_filename or lib_entry.output_path or ""
                output_path = self._epub_path(
                    downloads_dir,
                    title_for_filename,
                    preferred_name=Path(preferred).name if preferred else "",
                    preferred_path=preferred,
                )
                
                # Phase 1: Download chapters (with cache + retry pass)
                book_key = info.source_url if info else novel['url']
                
                def set_status(s, _ni=novel_idx, _tn=total_novels):
                    self._ui_progress(status=f"Novel {_ni + 1}/{_tn} — {s}")
                
                def set_progress(f, _ni=novel_idx, _tn=total_novels):
                    overall = (_ni + f / 2) / _tn
                    self._ui_progress(fraction=overall)
                
                try:
                    failed_titles = self._download_chapters_with_cache(
                        parser, chapters, book_key, set_status, set_progress
                    )
                except _DownloadCancelled:
                    cancelled = True
                    raise Exception("Cancelled by user")
                failed_ch_count = len(failed_titles)
                
                # Phase 2: Build EPUB
                self._ui_progress(
                    status=f"Novel {novel_idx + 1}/{total_novels}: Building EPUB...",
                    force=True,
                )
                
                cleaner = ContentCleaner() if self.clean_var.get() else None
                translator = None
                
                if self.translate_var.get():
                    translator = self._make_translator(self._get_workers())

                if translator:
                    builder = TranslatedEPUBBuilder(cleaner=cleaner, translator=translator, image_cache=self.cache)
                    
                    def progress_cb(current, total_steps, status, _ni=novel_idx, _tn=total_novels):
                        if self.cancel_requested:
                            translator.cancel()
                            return
                        overall = (_ni + 0.5 + (current / total_steps) * 0.5) / _tn
                        self._ui_progress(overall, f"Novel {_ni + 1}/{_tn}: {status}")
                    
                    builder.build_with_translation(info, chapters, output_path, progress_cb)
                else:
                    builder = EPUBBuilder(cleaner=cleaner, image_cache=self.cache)
                    
                    def progress_cb(current, total_steps, status, _ni=novel_idx, _tn=total_novels):
                        overall = (_ni + 0.5 + (current / total_steps) * 0.5) / _tn
                        self._ui_progress(overall, f"Novel {_ni + 1}/{_tn}: {status}")
                    
                    builder.build(info, chapters, output_path, progress_cb)
                
                results.append((title_for_filename, output_path, True, None, failed_ch_count))
                self._record_successful_download(
                    info, chapters, novel.get('translated_title'), output_path
                )
                self._mark_multi_novel_done(book_key)
                status_text = "Done" if not failed_ch_count else f"Done ({failed_ch_count} ch. failed)"
                self.after(0, lambda i=full_idx, s=status_text: self.multi_result_labels[i]['status'].configure(
                    text=s, text_color="#2B7A3E"
                ) if i < len(self.multi_result_labels) else None)
                
            except Exception as e:
                if "Cancelled" in str(e):
                    cancelled = True
                    results.append((title_for_filename, "", False, "Cancelled", 0))
                    break
                results.append((title_for_filename, "", False, str(e), 0))
                self.after(0, lambda i=full_idx: self.multi_result_labels[i]['status'].configure(
                    text="Failed", text_color="red"
                ) if i < len(self.multi_result_labels) else None)
        
        # All done - show summary
        self.after(0, lambda: self.progress_bar.set(1.0))
        
        success = [r for r in results if r[2]]
        failed = [r for r in results if not r[2]]
        
        if self.cancel_requested:
            pass  # Cancel already discarded the resume file
        elif self._active_job and self._active_job.get("kind") == "multi":
            pending = [n for n in self._active_job.get("novels") or [] if not n.get("done")]
            if pending:
                self._persist_active_job(force=True)
                job = self._active_job
                self.after(0, lambda j=job: self._show_resume_banner(j))
            else:
                self._clear_active_job()
        else:
            self._clear_active_job()
        
        summary = f"Completed: {len(success)}/{len(results)} novels\n\n"
        if success:
            summary += "Saved to:\n"
            for title, path, _, _, failed_ch in success:
                line = Path(path).name
                if failed_ch:
                    line += f"  ({failed_ch} chapter(s) failed - placeholder text)"
                summary += f"  • {line}\n"
        if failed:
            summary += "\nFailed:\n"
            for title, _, _, err, _ in failed:
                short_title = title[:30] + "..." if len(title) > 30 else title
                summary += f"  • {short_title}: {err[:40]}\n"
        
        summary += f"\nLocation: {downloads_dir}"
        
        notify(
            "Multi-download complete",
            f"{len(success)}/{len(results)} novels saved"
            + (f", {len(failed)} failed" if failed else ""),
        )
        
        self.after(0, lambda s=summary: self._update_status(
            f"Done! {len(success)}/{len(results)} novels downloaded."
        ))
        self.after(0, lambda s=summary: messagebox.showinfo("Multi-Download Complete", s))
        
        # Re-enable UI
        self.is_downloading = False
        self.is_paused = False
        self.after(0, lambda: self.multi_download_btn.configure(state="normal"))
        self.after(0, lambda: self.multi_fetch_btn.configure(state="normal"))
        self.after(0, lambda: self.multi_clear_btn.configure(state="normal"))
        self.after(0, lambda: self._set_download_controls_active(False))
        self.after(0, lambda: self.fetch_btn.configure(state="normal"))
        self.after(0, lambda: self.mode_switch.configure(state="normal"))
    
    # ------------------------------------------------------------------
    # Menubar
    # ------------------------------------------------------------------
    
    def _create_menubar(self):
        """Dark CTk menubar (native Win32 menus can't match the app theme)."""
        self._menu_popup = None
        self._menu_buttons = {}
        
        # Match CTk window surface (native Win32 menus can't be recolored)
        bar = ctk.CTkFrame(self, height=32, corner_radius=0, fg_color=("gray92", "gray14"))
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        self._menubar = bar
        
        for name in ("File", "Library", "Help"):
            btn = ctk.CTkButton(
                bar,
                text=name,
                width=64,
                height=28,
                corner_radius=4,
                fg_color="transparent",
                text_color=("gray10", "gray90"),
                hover_color=("gray80", "gray25"),
                font=("", 13),
                anchor="center",
            )
            btn.configure(command=lambda n=name, b=btn: self._menu_toggle(n, b))
            btn.pack(side="left", padx=(6 if name == "File" else 2, 2), pady=2)
            self._menu_buttons[name] = btn
        
        self.bind("<Escape>", lambda _e: self._menu_close(), add="+")
        self.bind_all("<Button-1>", self._menu_on_global_click, add="+")
    
    def _menu_items_for(self, name: str):
        """Build (label, command) rows; None = separator."""
        if name == "File":
            items = [
                ("Open books folder", self._menu_open_books_folder),
                ("Open data folder", self._menu_open_data_folder),
                ("Open log file", self._menu_open_log_file),
                None,
            ]
            history = self.library_store.get_history()[:12]
            if history:
                items.append(("Recent", None))
                for entry in history:
                    title = entry.translated_title or entry.title or entry.source_url
                    if len(title) > 42:
                        title = title[:39] + "..."
                    url = entry.source_url
                    items.append((f"  {title}", lambda u=url: self._load_url_from_history(u)))
                items.append(None)
            items.append(("Quit", self._on_close))
            return items
        if name == "Library":
            return [
                ("Check for updates", self._menu_library_check),
                ("Update all", self._on_library_update_all),
                None,
                ("Sync Now", self._menu_drive_sync_now),
                ("Open Drive folder", self._on_drive_open_folder),
                None,
                ("Reset library…", self._on_reset_library),
            ]
        return [
            ("Google Drive setup…", self._menu_drive_setup),
            ("Check for app updates", self._on_check_updates),
            None,
            ("About", self._menu_about),
        ]
    
    def _menu_toggle(self, name: str, button: ctk.CTkButton):
        if self._menu_popup is not None and getattr(self, "_menu_popup_name", None) == name:
            self._menu_close()
            return
        self._menu_open(name, button)
    
    def _menu_open(self, name: str, button: ctk.CTkButton):
        self._menu_close()
        items = self._menu_items_for(name)
        
        popup = ctk.CTkToplevel(self)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        self._menu_popup = popup
        self._menu_popup_name = name
        
        frame = ctk.CTkFrame(
            popup,
            fg_color=("gray92", "gray20"),
            corner_radius=6,
            border_width=1,
            border_color=("gray70", "gray35"),
        )
        frame.pack(fill="both", expand=True)
        
        for item in items:
            if item is None:
                ctk.CTkFrame(frame, height=1, fg_color=("gray70", "gray40")).pack(
                    fill="x", padx=8, pady=4
                )
                continue
            label, cmd = item
            disabled = cmd is None
            row = ctk.CTkButton(
                frame,
                text=label,
                anchor="w",
                height=30,
                corner_radius=4,
                fg_color="transparent",
                text_color=("gray50", "gray55") if disabled else ("gray10", "gray90"),
                hover_color=("gray80", "gray30"),
                font=("", 13),
                state="disabled" if disabled else "normal",
                command=(lambda c=cmd: self._menu_run(c)) if cmd else None,
            )
            row.pack(fill="x", padx=4, pady=1)
        
        self.update_idletasks()
        frame.update_idletasks()
        x = button.winfo_rootx()
        y = button.winfo_rooty() + button.winfo_height() + 2
        # Size to content
        popup.update_idletasks()
        w = max(200, frame.winfo_reqwidth() + 4)
        h = frame.winfo_reqheight() + 4
        popup.geometry(f"{w}x{h}+{x}+{y}")
        
        popup.bind("<Escape>", lambda _e: self._menu_close())
    
    def _menu_run(self, cmd):
        self._menu_close()
        if cmd:
            cmd()
    
    def _menu_close(self):
        popup = self._menu_popup
        self._menu_popup = None
        self._menu_popup_name = None
        if popup is not None:
            try:
                popup.destroy()
            except Exception:
                pass
    
    def _menu_widget_under(self, widget, ancestor) -> bool:
        try:
            w = str(widget)
            a = str(ancestor)
            return w == a or w.startswith(a + ".")
        except Exception:
            return False
    
    def _menu_on_global_click(self, event):
        """Close dropdown when clicking outside it / the menu buttons."""
        if self._menu_popup is None:
            return
        widget = event.widget
        if self._menu_widget_under(widget, self._menu_popup):
            return
        for btn in self._menu_buttons.values():
            if self._menu_widget_under(widget, btn):
                return
        self.after_idle(self._menu_close)
    
    def _reveal_path(self, path: Path, *, create_dir: bool = False):
        """Open a file or folder in the OS file manager / default app."""
        path = Path(path)
        try:
            if create_dir and not path.exists():
                path.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                messagebox.showinfo("Open", f"Not found yet:\n{path}")
                return
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess
                subprocess.run(["open", str(path)], check=False)
            else:
                import subprocess
                subprocess.run(["xdg-open", str(path)], check=False)
        except Exception as e:
            messagebox.showerror("Open", str(e))
    
    def _menu_open_books_folder(self):
        self._reveal_path(self._get_downloads_folder(), create_dir=True)
    
    def _menu_open_data_folder(self):
        self._reveal_path(self.data_dir, create_dir=True)
    
    def _menu_open_log_file(self):
        log_path = self.data_dir / "logs" / LOG_FILE_NAME
        if not log_path.exists():
            messagebox.showinfo("Log file", f"No log yet.\nExpected at:\n{log_path}")
            return
        self._reveal_path(log_path)
    
    def _load_url_from_history(self, url: str):
        """Load a history URL into Single mode."""
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, url)
        if self.multi_mode or self.library_mode:
            self.mode_switch.set("Single")
            self._on_mode_change("Single")
    
    def _menu_library_check(self):
        if not self.library_store.get_library():
            messagebox.showinfo("Library", "No tracked novels yet. Download something first.")
            return
        if not self.library_mode:
            self.mode_switch.set("Library")
            self._on_mode_change("Library")
        self._schedule_library_check(reason="manual", force=True)
    
    def _menu_drive_sync_now(self):
        if not self.drive_enabled_var.get():
            messagebox.showinfo(
                "Google Drive",
                "Enable Google Drive sync in the Library tab first.",
            )
            return
        self._on_drive_sync_now()
    
    def _menu_drive_setup(self):
        messagebox.showinfo("Google Drive setup", oauth_setup_instructions())
    
    def _menu_about(self):
        messagebox.showinfo(
            "About",
            f"{APP_TITLE}\n"
            f"Version {get_current_version()}\n\n"
            f"Data folder:\n{self.data_dir}\n\n"
            f"Books folder:\n{self._get_downloads_folder()}",
        )
    
    # ------------------------------------------------------------------
    # Recent history
    # ------------------------------------------------------------------
    
    def _show_recent_menu(self):
        """Popup listing recent downloads; click one to load its URL."""
        history = self.library_store.get_history()
        if not history:
            messagebox.showinfo("Recent", "No download history yet.")
            return
        
        popup = ctk.CTkToplevel(self)
        popup.title("Recent downloads")
        popup.geometry("560x420")
        popup.transient(self)
        popup.grab_set()
        
        popup.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - 560) // 2
        y = self.winfo_y() + (self.winfo_height() - 420) // 2
        popup.geometry(f"+{x}+{y}")
        
        ctk.CTkLabel(popup, text="Click a novel to load its URL", font=("", 13, "bold")).pack(
            padx=12, pady=(12, 6), anchor="w"
        )
        
        scroll = ctk.CTkScrollableFrame(popup)
        scroll.pack(fill="both", expand=True, padx=10, pady=5)
        
        def pick(url: str):
            popup.destroy()
            self._load_url_from_history(url)
        
        for entry in history:
            title = entry.translated_title or entry.title or entry.source_url
            if len(title) > 55:
                title = title[:52] + "..."
            meta = entry.author or ""
            if entry.chapter_count:
                meta = f"{meta} · {entry.chapter_count} ch." if meta else f"{entry.chapter_count} ch."
            
            row = ctk.CTkFrame(scroll)
            row.pack(fill="x", pady=3, padx=2)
            
            text_col = ctk.CTkFrame(row, fg_color="transparent")
            text_col.pack(side="left", fill="x", expand=True, padx=8, pady=6)
            ctk.CTkLabel(text_col, text=title, font=("", 12, "bold"), anchor="w").pack(fill="x")
            if meta:
                ctk.CTkLabel(text_col, text=meta, font=("", 11), text_color="gray", anchor="w").pack(fill="x")
            
            ctk.CTkButton(
                row, text="Load", width=70, height=28,
                command=lambda u=entry.source_url: pick(u)
            ).pack(side="right", padx=8, pady=6)
    
    # ------------------------------------------------------------------
    # Library mode
    # ------------------------------------------------------------------
    
    def _refresh_library_ui(self):
        """Rebuild the library shelf from the store (grid or list)."""
        self._library_row_widgets.clear()
        self._library_cover_images.clear()
        
        entries = self._filtered_library_entries()
        if self._library_view == "list":
            self._render_library_tree(entries)
        else:
            self._render_library_grid(entries)
        self._update_library_update_all_btn()
        self._update_library_download_btn()
    
    def _filtered_library_entries(self) -> list:
        entries = list(self.library_store.get_library())
        if self._library_filter == "updates":
            entries = [
                e for e in entries
                if (self._library_check_status.get(e.source_url) or {}).get("state") == "update"
                and int((self._library_check_status.get(e.source_url) or {}).get("new_count") or 0) > 0
            ]
        return entries
    
    def _on_library_view_change(self, value: str):
        self._library_view = "list" if value == "List" else "grid"
        self._apply_library_view_visibility()
        self._refresh_library_ui()
        self._save_settings()
    
    def _on_library_filter_change(self, value: str):
        self._library_filter = "updates" if value == "Updates" else "all"
        self._refresh_library_ui()
        self._save_settings()
    
    def _apply_library_view_visibility(self):
        if self._library_view == "list":
            self.library_list_frame.grid_remove()
            self.library_tree_frame.grid(row=0, column=0, sticky="nsew")
        else:
            self.library_tree_frame.grid_remove()
            self.library_list_frame.grid(row=0, column=0, sticky="nsew")
            self.after_idle(self._sync_library_grid_scrollregion)
    
    def _toggle_drive_panel(self):
        self._drive_panel_expanded = not self._drive_panel_expanded
        self._apply_drive_panel_visibility()
        self._save_settings()
    
    def _apply_drive_panel_visibility(self):
        if self._drive_panel_expanded:
            self.drive_details.pack(fill="x", after=self.drive_summary_row)
            self.drive_expand_btn.configure(text="▾ Drive")
        else:
            self.drive_details.pack_forget()
            self.drive_expand_btn.configure(text="▸ Drive")
    
    def _setup_library_tree_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Library.Treeview",
            background="#2b2b2b",
            foreground="#e8e8e8",
            fieldbackground="#2b2b2b",
            borderwidth=0,
            rowheight=26,
            font=("", 11),
        )
        style.map(
            "Library.Treeview",
            background=[("selected", "#1f6aa5")],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Library.Treeview.Heading",
            background="#333333",
            foreground="#e8e8e8",
            relief="flat",
        )
    
    def _library_status_text(self, source_url: str) -> Tuple[str, str]:
        info = self._library_check_status.get(source_url) or {}
        state = info.get("state", "")
        if state == "checking":
            return "Checking…", "orange"
        if state == "update":
            n = int(info.get("new_count") or 0)
            total = int(info.get("total") or 0)
            text = f"{n} new" + (f" · {total} on site" if total else "")
            return text, "#2B7A3E"
        if state == "current":
            total = int(info.get("total") or 0)
            return ("Up to date" + (f" · {total}" if total else ""), "gray")
        if state == "error":
            err = (info.get("error") or "error")[:50]
            return f"Failed: {err}", "red"
        return "", "gray"
    
    def _library_grid_viewport_width(self) -> int:
        """
        Width available for cover columns.

        Prefer library_body / library_frame (they shrink with the window). Avoid
        trusting the CTkScrollableFrame canvas first — it often keeps the old
        wide width after a shrink, which froze columns at e.g. 7.
        """
        for getter in (
            lambda: self.library_body.winfo_width(),
            lambda: self.library_frame.winfo_width(),
            lambda: self.library_list_frame._parent_frame.winfo_width(),
            lambda: self.library_list_frame._parent_canvas.winfo_width(),
        ):
            try:
                w = int(getter())
                if w > 40:
                    # Leave room for the CTk vertical scrollbar (~16–20px)
                    return max(80, w - 20)
            except Exception:
                pass
        return 200
    
    def _library_grid_column_count(self, viewport_width: Optional[int] = None) -> int:
        width = viewport_width if viewport_width is not None else self._library_grid_viewport_width()
        # ~132px tile + horizontal padding
        return max(1, int(width) // 150)
    
    def _on_library_window_configure(self, event=None):
        """Root window resize — only act on the toplevel itself."""
        if event is not None and getattr(event, "widget", None) is not self:
            return
        self._schedule_library_grid_reflow()
    
    def _on_library_shelf_configure(self, event=None):
        """library_frame / library_body resized."""
        if event is not None:
            widget = getattr(event, "widget", None)
            if widget not in (self.library_body, self.library_frame):
                return
        self._schedule_library_grid_reflow()
    
    def _schedule_library_grid_reflow(self):
        """Debounced column reflow when the shelf width changes."""
        if not self.library_mode or self._library_view != "grid":
            return
        width = self._library_grid_viewport_width()
        cols = self._library_grid_column_count(width)
        if (
            abs(width - self._library_grid_last_width) < 8
            and cols == self._library_grid_last_cols
            and self._library_grid_last_cols
        ):
            return
        if self._library_reflow_after is not None:
            try:
                self.after_cancel(self._library_reflow_after)
            except Exception:
                pass
        self._library_reflow_after = self.after(80, self._reflow_library_grid)
    
    def _sync_library_grid_scrollregion(self, restore_y: Optional[float] = None):
        """
        Force the cover-grid canvas to scroll through all tiles.

        CTkScrollableFrame often keeps scrollregion == viewport when children are
        gridded, so the scrollbar thumb stays full-size and nothing moves.
        """
        if self._library_view != "grid":
            return
        frame = self.library_list_frame
        try:
            canvas = frame._parent_canvas
            win_id = frame._create_window_id
        except Exception:
            return
        try:
            if restore_y is None:
                try:
                    restore_y = canvas.yview()[0]
                except Exception:
                    restore_y = 0.0
            frame.update_idletasks()
            canvas_w = self._library_grid_viewport_width()
            # Prefer laid-out content height; fall back to estimating from tile rows
            content_h = max(int(frame.winfo_reqheight()), 1)
            tiles = [w for w in self._library_row_widgets if w.get("kind") == "tile"]
            if tiles:
                cols = self._library_grid_column_count(canvas_w)
                rows = (len(tiles) + cols - 1) // cols
                # tile 220 + pady 12
                estimated = rows * 232 + 24
                content_h = max(content_h, estimated)
            # Pin content window to viewport width so columns can shrink on resize
            canvas.itemconfigure(win_id, width=canvas_w, height=content_h)
            frame.update_idletasks()
            bbox = canvas.bbox("all")
            if bbox:
                x1, y1, x2, y2 = bbox
                # Do not let scrollregion wider than the viewport (avoids horizontal clipping)
                canvas.configure(scrollregion=(x1, y1, canvas_w, max(y2, content_h)))
            else:
                canvas.configure(scrollregion=(0, 0, canvas_w, content_h))
            canvas.yview_moveto(max(0.0, min(float(restore_y), 1.0)))
        except Exception as e:
            print(f"Warning: library scrollregion sync failed: {e}")
    
    def _bind_library_scroll_helpers(self):
        """Reliable mouse-wheel scrolling over cover tiles (Windows/macOS/Linux)."""
        frame = self.library_list_frame
        try:
            canvas = frame._parent_canvas
        except Exception:
            return
        
        def _pointer_over_library() -> bool:
            try:
                x, y = self.winfo_pointerxy()
                widget = self.winfo_containing(x, y)
                while widget is not None:
                    if widget is frame or widget is canvas:
                        return True
                    # CTkScrollableFrame geometry is on its parent frame
                    try:
                        if widget is frame._parent_frame:
                            return True
                    except Exception:
                        pass
                    widget = getattr(widget, "master", None)
            except Exception:
                return False
            return False
        
        def _on_wheel(event):
            if self._library_view != "grid" or not self.library_mode:
                return
            if not _pointer_over_library():
                return
            try:
                # Refresh region in case tiles were added/resized since last sync
                top, bottom = canvas.yview()
                if top == 0.0 and bottom == 1.0:
                    self._sync_library_grid_scrollregion(restore_y=top)
                    top, bottom = canvas.yview()
                    if top == 0.0 and bottom == 1.0:
                        return
            except Exception:
                return
            try:
                if getattr(event, "delta", 0):
                    # Windows / macOS
                    steps = -int(event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
                    if sys.platform == "darwin":
                        steps = -int(event.delta)
                    canvas.yview_scroll(steps, "units")
                elif getattr(event, "num", None) in (4, 5):
                    canvas.yview_scroll(-1 if event.num == 4 else 1, "units")
            except Exception:
                pass
            return "break"
        
        # bind_all so wheel works when hovering CTk child widgets
        if not self._library_wheel_bound:
            self.bind_all("<MouseWheel>", _on_wheel, add="+")
            self.bind_all("<Button-4>", _on_wheel, add="+")
            self.bind_all("<Button-5>", _on_wheel, add="+")
            self._library_wheel_bound = True
    
    def _reflow_library_grid(self):
        self._library_reflow_after = None
        if self._library_view != "grid" or not self.library_mode:
            return
        tiles = [w for w in self._library_row_widgets if w.get("kind") == "tile"]
        if not tiles:
            return
        
        # Body width tracks the window; force the scroll canvas to the same width
        # so tiles wrap instead of staying in a wide off-screen row.
        try:
            body_w = max(int(self.library_body.winfo_width()), 80)
            canvas = self.library_list_frame._parent_canvas
            canvas.configure(width=max(60, body_w - 24))
            canvas.itemconfigure(
                self.library_list_frame._create_window_id,
                width=max(60, body_w - 24),
            )
        except Exception:
            pass
        
        width = self._library_grid_viewport_width()
        cols = self._library_grid_column_count(width)
        self._library_grid_last_width = width
        self._library_grid_last_cols = cols
        
        # Preserve scroll position across regrid (geometry changes reset it)
        y0 = 0.0
        try:
            y0 = self.library_list_frame._parent_canvas.yview()[0]
        except Exception:
            pass
        
        for i, row in enumerate(tiles):
            widget = row.get("frame")
            if widget is None:
                continue
            try:
                widget.grid_forget()
            except Exception:
                pass
            try:
                widget.grid(row=i // cols, column=i % cols, padx=6, pady=6, sticky="n")
            except Exception:
                pass
        
        self._sync_library_grid_scrollregion(restore_y=y0)
    
    def _render_library_grid(self, entries: list):
        for child in self.library_list_frame.winfo_children():
            child.destroy()
        self._library_row_widgets.clear()
        self._library_cover_images.clear()
        self._library_grid_last_width = 0  # force next reflow to place tiles
        self._library_grid_last_cols = 0
        
        if not entries:
            msg = (
                "No novels with updates. Run Check updates, or switch filter to All."
                if self._library_filter == "updates"
                else "No tracked novels yet.\nDownload something in Single or Multi mode and it will appear here."
            )
            ctk.CTkLabel(
                self.library_list_frame,
                text=msg,
                text_color="gray",
                justify="left",
            ).pack(padx=12, pady=20, anchor="w")
            return
        
        for entry in entries:
            self._create_library_tile(entry)
        self.after(50, self._reflow_library_grid)
        # Second pass after layout settles — ensures scrollbar thumb shrinks for overflow
        self.after(200, self._sync_library_grid_scrollregion)
    
    def _create_library_tile(self, entry):
        tile = ctk.CTkFrame(self.library_list_frame, width=132, height=220)
        tile.grid_propagate(False)
        
        cover_lbl = ctk.CTkLabel(tile, text="No cover", width=110, height=150)
        cover_lbl.pack(padx=8, pady=(8, 4))
        
        title = entry.translated_title or entry.title or entry.source_url
        if len(title) > 28:
            title = title[:25] + "…"
        ctk.CTkLabel(
            tile, text=title, font=("", 11, "bold"), wraplength=116, justify="center"
        ).pack(padx=4)
        
        status_label = ctk.CTkLabel(tile, text="", font=("", 10), text_color="gray")
        status_label.pack(padx=4, pady=(2, 6))
        
        def select(_e=None, u=entry.source_url):
            self._selected_library_url = u
            self._update_library_download_btn()
        
        def menu(event, e=entry):
            self._selected_library_url = e.source_url
            self._show_library_entry_menu(e, event.x_root, event.y_root)
        
        for w in (tile, cover_lbl, status_label):
            w.bind("<Button-1>", select)
            w.bind("<Double-Button-1>", lambda _e, ent=entry: self._on_library_update(ent))
            w.bind("<Button-3>", menu)
        
        self._library_row_widgets.append({
            "kind": "tile",
            "url": entry.source_url,
            "frame": tile,
            "status_label": status_label,
            "cover_label": cover_lbl,
        })
        self._apply_library_row_status(entry.source_url)
        self._load_library_cover_async(entry, cover_lbl)
    
    def _render_library_tree(self, entries: list):
        tree = self.library_tree
        tree.delete(*tree.get_children())
        self._library_row_widgets.clear()
        
        if not entries:
            return
        
        for entry in entries:
            title = entry.translated_title or entry.title or entry.source_url
            when = ""
            if entry.last_downloaded_at:
                try:
                    when = time.strftime("%Y-%m-%d", time.localtime(entry.last_downloaded_at))
                except Exception:
                    when = ""
            status, _ = self._library_status_text(entry.source_url)
            iid = entry.source_url
            tree.insert(
                "",
                "end",
                iid=iid,
                text=title[:80],
                values=(entry.chapter_count or "", status, when),
            )
            self._library_row_widgets.append({
                "kind": "tree",
                "url": entry.source_url,
            })
    
    def _on_library_tree_select(self, _event=None):
        sel = self.library_tree.selection()
        self._selected_library_url = sel[0] if sel else None
        self._update_library_download_btn()
    
    def _on_library_tree_activate(self, _event=None):
        entry = self._selected_library_entry()
        if entry:
            self._on_library_update(entry)
    
    def _on_library_tree_menu(self, event):
        row = self.library_tree.identify_row(event.y)
        if row:
            self.library_tree.selection_set(row)
            self._selected_library_url = row
            entry = self.library_store.get_library_entry(row)
            if entry:
                self._show_library_entry_menu(entry, event.x_root, event.y_root)
    
    def _selected_library_entry(self):
        url = self._selected_library_url
        if not url:
            return None
        return self.library_store.get_library_entry(url)
    
    def _show_library_entry_menu(self, entry, x: int, y: int):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Update", command=lambda e=entry: self._on_library_update(e))
        menu.add_command(label="Open URL", command=lambda u=entry.source_url: self._library_open_url(u))
        local_missing = not (entry.output_path and Path(entry.output_path).is_file())
        remote_id = entry.drive_file_id or self._remote_books.get(entry.epub_filename or "")
        if local_missing and remote_id and self.drive_enabled_var.get():
            menu.add_command(
                label="Download EPUB",
                command=lambda e=entry, fid=remote_id: self._on_drive_download_epub(e, fid),
            )
        menu.add_separator()
        menu.add_command(
            label="Remove",
            command=lambda u=entry.source_url: self._on_library_remove(u),
        )
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()
    
    def _on_library_selected_update(self):
        entry = self._selected_library_entry()
        if entry:
            self._on_library_update(entry)
    
    def _on_library_selected_open(self):
        entry = self._selected_library_entry()
        if entry:
            self._library_open_url(entry.source_url)
    
    def _on_library_selected_remove(self):
        entry = self._selected_library_entry()
        if entry:
            self._on_library_remove(entry.source_url)
    
    def _on_library_selected_download_epub(self):
        entry = self._selected_library_entry()
        if not entry:
            return
        remote_id = entry.drive_file_id or self._remote_books.get(entry.epub_filename or "")
        if remote_id:
            self._on_drive_download_epub(entry, remote_id)
    
    def _update_library_download_btn(self):
        btn = getattr(self, "library_download_epub_btn", None)
        if btn is None:
            return
        entry = self._selected_library_entry()
        show = False
        if entry and self.drive_enabled_var.get():
            local_missing = not (entry.output_path and Path(entry.output_path).is_file())
            remote_id = entry.drive_file_id or self._remote_books.get(entry.epub_filename or "")
            show = bool(local_missing and remote_id)
        try:
            if show:
                btn.configure(state="normal")
            else:
                btn.configure(state="disabled")
        except Exception:
            pass
    
    def _load_library_cover_async(self, entry, cover_label):
        url = (entry.cover_url or "").strip()
        source = entry.source_url
        
        def worker():
            data = self.cache.get_cover(cover_url=url, source_url=source)
            if not data and url:
                try:
                    from core.security import validate_fetch_url
                    validate_fetch_url(url, allow_http=True)
                    resp = http_session.get(url, timeout=15)
                    resp.raise_for_status()
                    data = resp.content
                    if data:
                        ctype = ""
                        try:
                            ctype = resp.headers.get("content-type", "") or ""
                        except Exception:
                            pass
                        self.cache.put_cover(
                            data, cover_url=url, source_url=source, content_type=ctype
                        )
                except Exception as e:
                    print(f"Library cover fetch failed: {e}")
                    data = None
            if not data:
                return
            try:
                image = Image.open(BytesIO(data))
                image.thumbnail((110, 150), Image.Resampling.LANCZOS)
                ctk_image = ctk.CTkImage(light_image=image, dark_image=image, size=image.size)
                self.after(0, lambda: self._set_library_cover(cover_label, ctk_image, source))
            except Exception as e:
                print(f"Library cover decode failed: {e}")
        
        threading.Thread(target=worker, daemon=True).start()
    
    def _set_library_cover(self, cover_label, ctk_image, source_url: str):
        # Ignore if tile was destroyed / rebuilt
        still = any(
            w.get("url") == source_url and w.get("cover_label") is cover_label
            for w in self._library_row_widgets
        )
        if not still:
            return
        self._library_cover_images.append(ctk_image)
        try:
            cover_label.configure(image=ctk_image, text="")
        except Exception:
            pass
    
    def _library_novels_with_updates(self) -> list:
        urls = []
        for url, info in self._library_check_status.items():
            if info.get('state') == 'update' and int(info.get('new_count') or 0) > 0:
                urls.append(url)
        return urls
    
    def _update_library_update_all_btn(self):
        n = len(self._library_novels_with_updates())
        if n:
            self.library_update_all_btn.configure(
                state="normal", text=f"Update All ({n})"
            )
        else:
            self.library_update_all_btn.configure(state="disabled", text="Update All")
    
    def _apply_library_row_status(self, source_url: str):
        text, color = self._library_status_text(source_url)
        for row in self._library_row_widgets:
            if row.get("url") != source_url:
                continue
            if row.get("kind") == "tile":
                try:
                    row["status_label"].configure(text=text, text_color=color)
                except Exception:
                    pass
            elif row.get("kind") == "tree":
                try:
                    if self.library_tree.exists(source_url):
                        vals = list(self.library_tree.item(source_url, "values"))
                        if len(vals) >= 2:
                            vals[1] = text
                            self.library_tree.item(source_url, values=vals)
                except Exception:
                    pass
            break
    
    def _schedule_library_check(self, reason: str = "", force: bool = False):
        """Background-check all library novels for new chapters (TOC only)."""
        if self._library_checking and not force:
            return
        if self.is_downloading:
            return
        entries = self.library_store.get_library()
        if not entries:
            return
        if self._library_checking:
            return
        
        self._library_checking = True
        try:
            self.library_check_btn.configure(state="disabled", text="Checking…")
            self.library_check_status_label.configure(text="Checking library…")
        except Exception:
            pass
        
        for entry in entries:
            self._library_check_status[entry.source_url] = {
                'state': 'checking', 'new_count': 0, 'total': 0, 'error': ''
            }
            self._apply_library_row_status(entry.source_url)
        
        thread = threading.Thread(
            target=self._library_check_thread,
            args=(list(entries), reason),
            daemon=True,
        )
        thread.start()
    
    def _library_check_thread(self, entries: list, reason: str):
        with_updates = 0
        total = len(entries)
        try:
            for idx, entry in enumerate(entries):
                if self.cancel_requested and reason == "manual":
                    break
                title = entry.translated_title or entry.title or entry.source_url
                self.after(0, lambda i=idx, t=total, n=title: self._update_status(
                    f"Checking library [{i + 1}/{t}]: {n[:40]}"
                ))
                self.after(0, lambda i=idx, t=total: self.library_check_status_label.configure(
                    text=f"Checking {i + 1}/{t}…"
                ))
                
                delay = 1.0
                try:
                    parser = get_parser_for_url(entry.source_url)
                    if not parser:
                        raise Exception("Unsupported site")
                    delay = float(getattr(parser, 'request_delay', 1.0) or 1.0)
                    chapters = parser.get_chapter_list(entry.source_url)
                    if not chapters:
                        raise Exception("No chapters found")
                    # Local-only TOC snapshot for faster future checks / offline hints
                    try:
                        self.cache.put_chapter_list(entry.source_url, chapters)
                    except Exception:
                        pass
                    new_only, _ = new_chapters_since(
                        chapters, entry.last_chapter_url, entry.chapter_count
                    )
                    if new_only:
                        with_updates += 1
                        self._library_check_status[entry.source_url] = {
                            'state': 'update',
                            'new_count': len(new_only),
                            'total': len(chapters),
                            'error': '',
                        }
                    else:
                        self._library_check_status[entry.source_url] = {
                            'state': 'current',
                            'new_count': 0,
                            'total': len(chapters),
                            'error': '',
                        }
                except Exception as e:
                    self._library_check_status[entry.source_url] = {
                        'state': 'error',
                        'new_count': 0,
                        'total': 0,
                        'error': str(e),
                    }
                
                self.after(0, lambda u=entry.source_url: self._apply_library_row_status(u))
                if idx < total - 1:
                    time.sleep(min(max(delay, 0.5), 3.0))
        finally:
            self.after(0, lambda w=with_updates, t=total, r=reason: self._on_library_check_done(w, t, r))
    
    def _on_library_check_done(self, with_updates: int, total: int, reason: str):
        self._library_checking = False
        try:
            self.library_check_btn.configure(state="normal", text="Check updates")
        except Exception:
            pass
        self._update_library_update_all_btn()
        
        if with_updates:
            summary = f"{with_updates}/{total} novel(s) have new chapters"
            self.library_check_status_label.configure(text=summary, text_color="#2B7A3E")
            self._update_status(summary)
            notify("Library updates available", summary)
        else:
            summary = f"All {total} novel(s) up to date"
            self.library_check_status_label.configure(text=summary, text_color="gray")
            self._update_status(summary)
        # Refresh filtered shelf (e.g. Updates-only) and tree status cells
        if self.library_mode:
            self._refresh_library_ui()
    
    def _on_library_update_all(self):
        """Update every novel that the last check marked as having new chapters."""
        if self.is_downloading or self._library_checking:
            return
        urls = self._library_novels_with_updates()
        if not urls:
            messagebox.showinfo("Update All", "No novels with new chapters. Run Check updates first.")
            return
        
        entries = []
        for url in urls:
            entry = self.library_store.get_library_entry(url)
            if entry:
                entries.append(entry)
        if not entries:
            return
        
        if not messagebox.askyesno(
            "Update All",
            f"Update {len(entries)} novel(s) with new chapters?\n\n"
            "Each one rebuilds a full EPUB (cached chapters are reused)."
        ):
            return
        
        self._save_settings()
        self._set_active_job({
            "kind": "library_update_all",
            "status": "running",
            "options": self._download_options_snapshot(),
            "entries": [
                {
                    "source_url": e.source_url,
                    "title": e.title or "",
                    "translated_title": e.translated_title or "",
                    "done": False,
                }
                for e in entries
            ],
        })
        self.is_downloading = True
        self.cancel_requested = False
        self.is_paused = False
        self._set_download_controls_active(True)
        self.mode_switch.configure(state="disabled")
        self.library_refresh_btn.configure(state="disabled")
        self.library_check_btn.configure(state="disabled")
        self.library_update_all_btn.configure(state="disabled")
        self.progress_bar.set(0)
        
        thread = threading.Thread(
            target=self._library_update_all_thread,
            args=(entries,),
            daemon=True,
        )
        thread.start()
    
    def _mark_library_update_all_entry_done(self, source_url: str):
        if not self._active_job or self._active_job.get("kind") != "library_update_all":
            return
        for item in self._active_job.get("entries") or []:
            if item.get("source_url") == source_url:
                item["done"] = True
                break
        self._persist_active_job(force=True)
    
    def _library_update_all_thread(self, entries: list):
        results = []  # (title, ok, detail)
        total = len(entries)
        try:
            for idx, entry in enumerate(entries):
                try:
                    self._wait_while_paused()
                except _DownloadCancelled:
                    results.append((
                        entry.translated_title or entry.title,
                        False,
                        "Cancelled",
                    ))
                    break
                if self.cancel_requested:
                    results.append((
                        entry.translated_title or entry.title,
                        False,
                        "Cancelled",
                    ))
                    break
                
                display = entry.translated_title or entry.title or "Novel"
                self.after(0, lambda i=idx, t=total, n=display: self._update_status(
                    f"Update All [{i + 1}/{t}]: {n[:40]}"
                ))
                
                try:
                    parser = get_parser_for_url(entry.source_url)
                    if not parser:
                        raise Exception("Unsupported site")
                    
                    if hasattr(parser, 'fetch_all_parallel'):
                        info, chapters = parser.fetch_all_parallel(entry.source_url)
                    else:
                        info = parser.get_novel_info(entry.source_url)
                        chapters = parser.get_chapter_list(entry.source_url)
                    if not chapters:
                        raise Exception("No chapters found")
                    
                    # Refresh resume snapshot with this book's chapter list
                    if self._active_job and self._active_job.get("kind") == "library_update_all":
                        for item in self._active_job.get("entries") or []:
                            if item.get("source_url") == entry.source_url:
                                item["chapters"] = chapters_to_job(chapters)
                                item["info"] = novel_info_to_job(info)
                                break
                        self._persist_active_job(force=True)
                    
                    new_only, _ = new_chapters_since(
                        chapters, entry.last_chapter_url, entry.chapter_count
                    )
                    if not new_only:
                        self._library_check_status[entry.source_url] = {
                            'state': 'current', 'new_count': 0,
                            'total': len(chapters), 'error': '',
                        }
                        results.append((display, True, "Already up to date"))
                        self._mark_library_update_all_entry_done(entry.source_url)
                        continue
                    
                    translated_title = entry.translated_title or info.title
                    if self.translate_var.get() and not entry.translated_title:
                        try:
                            translated_title = (
                                self._make_translator(1).translate_text(info.title)
                                or info.title
                            )
                        except Exception:
                            translated_title = info.title
                    
                    output_path = self._epub_path(
                        self._get_downloads_folder(),
                        translated_title,
                        preferred_name=entry.epub_filename or "",
                        preferred_path=entry.output_path or "",
                    )
                    book_key = info.source_url or entry.source_url
                    
                    def set_status(s, _i=idx, _t=total):
                        self._ui_progress(status=f"Update All [{_i + 1}/{_t}] — {s}")
                    
                    def set_progress(f, _i=idx, _t=total):
                        overall = (_i + f / 2) / _t
                        self._ui_progress(fraction=overall)
                    
                    failed_titles = self._download_chapters_with_cache(
                        parser, chapters, book_key, set_status, set_progress
                    )
                    
                    cleaner = ContentCleaner() if self.clean_var.get() else None
                    translator = (
                        self._make_translator(self._get_workers())
                        if self.translate_var.get() else None
                    )
                    
                    if translator:
                        builder = TranslatedEPUBBuilder(
                            cleaner=cleaner, translator=translator, image_cache=self.cache
                        )
                        
                        def progress_cb(current, total_steps, status, _i=idx, _t=total):
                            if self.cancel_requested:
                                translator.cancel()
                                return
                            overall = (_i + 0.5 + (current / total_steps) * 0.5) / _t
                            self._ui_progress(overall, f"Update All [{_i + 1}/{_t}]: {status}")
                        
                        builder.build_with_translation(
                            info, chapters, output_path, progress_cb
                        )
                    else:
                        builder = EPUBBuilder(cleaner=cleaner, image_cache=self.cache)
                        
                        def progress_cb(current, total_steps, status, _i=idx, _t=total):
                            overall = (_i + 0.5 + (current / total_steps) * 0.5) / _t
                            self._ui_progress(overall, f"Update All [{_i + 1}/{_t}]: {status}")
                        
                        builder.build(info, chapters, output_path, progress_cb)
                    
                    self._record_successful_download(
                        info, chapters, translated_title, output_path
                    )
                    self._library_check_status[entry.source_url] = {
                        'state': 'current',
                        'new_count': 0,
                        'total': len(chapters),
                        'error': '',
                    }
                    self._mark_library_update_all_entry_done(entry.source_url)
                    detail = f"+{len(new_only)} → {Path(output_path).name}"
                    if failed_titles:
                        detail += f" ({len(failed_titles)} ch. failed)"
                    results.append((display, True, detail))
                except _DownloadCancelled:
                    results.append((display, False, "Cancelled"))
                    break
                except Exception as e:
                    results.append((display, False, str(e)))
                    self._library_check_status[entry.source_url] = {
                        'state': 'error',
                        'new_count': 0,
                        'total': 0,
                        'error': str(e),
                    }
        finally:
            self.after(0, lambda r=results: self._on_library_update_all_done(r))
    
    def _on_library_update_all_done(self, results: list):
        self.is_downloading = False
        self.is_paused = False
        # Clear job only when every entry finished (Cancel already cleared it)
        if self._active_job and self._active_job.get("kind") == "library_update_all":
            pending = [e for e in self._active_job.get("entries") or [] if not e.get("done")]
            if pending and not self.cancel_requested:
                self._persist_active_job(force=True)
                self._show_resume_banner(self._active_job)
            else:
                self._clear_active_job()
        self._set_download_controls_active(False)
        self.mode_switch.configure(state="normal")
        self.library_refresh_btn.configure(state="normal")
        self.library_check_btn.configure(state="normal")
        self.progress_bar.set(1.0)
        self._refresh_library_ui()
        self._update_library_update_all_btn()
        
        ok = [r for r in results if r[1]]
        failed = [r for r in results if not r[1]]
        summary = f"Update All: {len(ok)}/{len(results)} succeeded"
        if failed:
            summary += f", {len(failed)} failed"
        self._update_status(summary)
        notify("Update All complete", summary)
        
        lines = [summary, ""]
        for title, success, detail in results:
            short = title[:40] + ("…" if len(title) > 40 else "")
            mark = "✓" if success else "✗"
            lines.append(f"{mark} {short}: {detail[:60]}")
        messagebox.showinfo("Update All complete", "\n".join(lines))
    
    def _library_open_url(self, url: str):
        self.mode_switch.set("Single")
        self._on_mode_change("Single")
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, url)
    
    def _on_library_remove(self, source_url: str):
        if not messagebox.askyesno("Remove", "Remove this novel from your library?\n(Cached chapters are kept.)"):
            return
        self.library_store.remove_library(source_url)
        self._refresh_library_ui()
        if self.drive_enabled_var.get() and self.drive_library_var.get():
            self._schedule_drive_sync(silent=True)
    
    def _on_reset_library(self):
        """Clear tracked library locally and push empty library.json to Drive if connected."""
        if self.is_downloading or self._library_checking or self._drive_syncing:
            messagebox.showinfo("Reset library", "Wait for the current download/sync to finish.")
            return
        
        n = len(self.library_store.get_library())
        h = len(self.library_store.get_history())
        if n == 0 and h == 0:
            messagebox.showinfo("Reset library", "Library and history are already empty.")
            return
        
        if not messagebox.askyesno(
            "Reset library",
            "Clear all tracked novels from your local library?\n\n"
            "If Google Drive sync is connected, the remote library.json "
            "will also be cleared.\n\n"
            "EPUB files and chapter cache are NOT deleted.",
            icon="warning",
        ):
            return
        
        clear_history = False
        if h:
            clear_history = messagebox.askyesno(
                "Also clear history?",
                f"Also clear Recent download history ({h} item(s))?",
            )
        
        self.library_store.clear(clear_library=True, clear_history=clear_history)
        self._library_check_status.clear()
        self._remote_books.clear()
        self._refresh_library_ui()
        self.library_check_status_label.configure(text="Library reset", text_color="gray")
        
        drive_msg = ""
        if self.drive_enabled_var.get():
            def worker():
                try:
                    if not self.drive_sync.is_connected():
                        if not self.drive_sync.try_restore_session():
                            self.after(0, lambda: self._update_status(
                                "Local library cleared (Drive not connected)"
                            ))
                            return
                    # Always push empty library so the next sync does not pull old entries back
                    self.drive_sync.push_library(self.library_store.get_data())
                    where = self.drive_sync.location_description()
                    self.after(0, lambda: self._record_drive_sync_success(
                        f"Library reset locally and on Drive ({where})"
                    ))
                    self.after(0, lambda: self._update_status(
                        f"Library reset (local + Drive → {where})"
                    ))
                    self.after(0, lambda: notify(
                        "Library reset",
                        f"Cleared locally and on Drive ({where})",
                    ))
                except Exception as e:
                    err = str(e)
                    print(f"Drive library reset failed: {err}")
                    self.after(0, lambda msg=err: messagebox.showwarning(
                        "Drive reset incomplete",
                        f"Local library was cleared, but Drive update failed:\n{msg}\n\n"
                        "Connect and Sync Now (or Reset again) when online.",
                    ))
                    self.after(0, lambda: self._update_status(
                        "Local library cleared (Drive update failed)"
                    ))
            
            self._update_status("Clearing Drive library…")
            threading.Thread(target=worker, daemon=True).start()
            drive_msg = "\nUpdating Google Drive…"
        else:
            self._update_status("Library reset (local only)")
        
        messagebox.showinfo(
            "Library reset",
            f"Cleared {n} tracked novel(s)"
            + (" and history." if clear_history else ".")
            + drive_msg
            + "\n\nEPUB files on disk were kept.",
        )
    
    def _on_library_update(self, entry):
        """Check for new chapters and rebuild the full EPUB (cache for old ones)."""
        if self.is_downloading:
            return
        
        parser = get_parser_for_url(entry.source_url)
        if not parser:
            messagebox.showerror("Error", f"Unsupported site:\n{entry.source_url}")
            return
        
        self._save_settings()
        self.is_downloading = True
        self.cancel_requested = False
        self.is_paused = False
        self._set_download_controls_active(True)
        self.mode_switch.configure(state="disabled")
        self.library_refresh_btn.configure(state="disabled")
        self.progress_bar.set(0)
        title = entry.translated_title or entry.title or "novel"
        self._update_status(f"Checking for new chapters: {title[:40]}...")
        
        thread = threading.Thread(
            target=self._library_update_thread,
            args=(entry, parser),
            daemon=True,
        )
        thread.start()
    
    def _library_update_thread(self, entry, parser):
        """Background: fetch TOC, find new chapters, rebuild full EPUB."""
        display = entry.translated_title or entry.title or "Novel"
        try:
            url = entry.source_url
            if hasattr(parser, 'fetch_all_parallel'):
                info, chapters = parser.fetch_all_parallel(url)
            else:
                info = parser.get_novel_info(url)
                chapters = parser.get_chapter_list(url)
            
            if not chapters:
                raise Exception("No chapters found on source site")
            
            new_only, _start = new_chapters_since(
                chapters, entry.last_chapter_url, entry.chapter_count
            )
            
            if not new_only:
                notify("Up to date", f"{display} — no new chapters")
                self.after(0, lambda: self._update_status(f"Up to date: {display}"))
                self.after(0, lambda: messagebox.showinfo(
                    "Up to date",
                    f"No new chapters for:\n{display}\n\n({len(chapters)} chapters on site)"
                ))
                return
            
            self.after(0, lambda n=len(new_only), t=len(chapters): self._update_status(
                f"{n} new chapter(s) — rebuilding full EPUB ({t} total, cache for old)..."
            ))
            
            # Prefer stored English title; fall back to a fresh translation of the title
            translated_title = entry.translated_title or info.title
            if self.translate_var.get() and not entry.translated_title:
                try:
                    translated_title = self._make_translator(1).translate_text(info.title) or info.title
                except Exception:
                    translated_title = info.title
            
            output_path = self._epub_path(
                self._get_downloads_folder(),
                translated_title,
                preferred_name=entry.epub_filename or "",
                preferred_path=entry.output_path or "",
            )
            book_key = info.source_url or url
            
            self._set_active_job({
                "kind": "library_update",
                "status": "running",
                "source_url": url,
                "title": info.title or entry.title or "",
                "translated_title": translated_title or "",
                "info": novel_info_to_job(info),
                "chapters": chapters_to_job(chapters),
                "output_path": output_path,
                "options": self._download_options_snapshot(),
            })
            
            def set_status(s):
                self._ui_progress(status=s)
            
            def set_progress(f):
                self._ui_progress(fraction=f / 2)
            
            try:
                failed_titles = self._download_chapters_with_cache(
                    parser, chapters, book_key, set_status, set_progress
                )
            except _DownloadCancelled:
                self._ui_progress(status="Cancelled", force=True)
                return
            
            self._ui_progress(status="Building EPUB...", force=True)
            
            cleaner = ContentCleaner() if self.clean_var.get() else None
            translator = self._make_translator(self._get_workers()) if self.translate_var.get() else None
            
            if translator:
                builder = TranslatedEPUBBuilder(cleaner=cleaner, translator=translator, image_cache=self.cache)
                
                def progress_cb(current, total_steps, status):
                    if self.cancel_requested:
                        translator.cancel()
                        return
                    progress = 0.5 + (current / total_steps) * 0.5
                    self._ui_progress(progress, status)
                
                builder.build_with_translation(info, chapters, output_path, progress_cb)
            else:
                builder = EPUBBuilder(cleaner=cleaner, image_cache=self.cache)
                
                def progress_cb(current, total_steps, status):
                    progress = 0.5 + (current / total_steps) * 0.5
                    self._ui_progress(progress, status)
                
                builder.build(info, chapters, output_path, progress_cb)
            
            self._record_successful_download(info, chapters, translated_title, output_path)
            self._clear_active_job()
            self._library_check_status[entry.source_url] = {
                'state': 'current',
                'new_count': 0,
                'total': len(chapters),
                'error': '',
            }
            
            new_count = len(new_only)
            msg = (
                f"Updated {display}\n"
                f"{new_count} new chapter(s) · {len(chapters)} total\n\n"
                f"Saved to:\n{output_path}"
            )
            if failed_titles:
                msg += f"\n\nWarning: {len(failed_titles)} chapter(s) failed."
            
            notify("Library update complete", f"{display}: +{new_count} chapters")
            self._ui_progress(1.0, f"Updated! +{new_count} → {output_path}", force=True)
            self.after(0, lambda m=msg: messagebox.showinfo("Library Updated", m))
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self._persist_active_job(force=True)
            except Exception:
                pass
            self.after(0, lambda msg=str(e): self._show_error(f"Library update failed: {msg}"))
        finally:
            self.is_downloading = False
            self.is_paused = False
            self.after(0, lambda: self._set_download_controls_active(False))
            self.after(0, lambda: self.mode_switch.configure(state="normal"))
            self.after(0, lambda: self.library_refresh_btn.configure(state="normal"))
            self.after(0, lambda: self.library_check_btn.configure(state="normal"))
            self.after(0, self._refresh_library_ui)
            self.after(0, self._update_library_update_all_btn)
    
    # ------------------------------------------------------------------
    # Google Drive sync
    # ------------------------------------------------------------------
    
    def _update_drive_sync_controls(self):
        """Enable/disable Drive controls based on master checkbox."""
        on = bool(self.drive_enabled_var.get())
        state = "normal" if on else "disabled"
        for w in (
            self.drive_connect_btn,
            self.drive_sync_now_btn,
            self.drive_open_folder_btn,
            self.drive_change_folder_btn,
        ):
            try:
                w.configure(state=state)
            except Exception:
                pass
        self._update_drive_status_label()
        self._update_drive_folder_help()
    
    def _update_drive_folder_help(self):
        name = self.settings.get('drive_folder_name') or DRIVE_FOLDER_NAME
        self.drive_folder_help.configure(
            text=f"Drive folder: My Drive → {name} (library.json + books/). Use Change folder to pick another."
        )
    
    def _update_drive_last_sync_label(self):
        ts = float(self.settings.get('drive_last_synced_at') or 0)
        summary = (self.settings.get('drive_last_sync_summary') or "").strip()
        if not ts:
            self.drive_last_sync_label.configure(text="Last synced: never")
            return
        try:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        except Exception:
            when = "?"
        label = f"Last synced: {when}"
        if summary:
            label = f"{summary} · {label}"
        self.drive_last_sync_label.configure(text=label)
    
    def _record_drive_sync_success(self, summary: str):
        self.settings['drive_last_synced_at'] = time.time()
        self.settings['drive_last_sync_summary'] = summary
        from core.settings import update_settings
        update_settings(
            drive_last_synced_at=self.settings['drive_last_synced_at'],
            drive_last_sync_summary=summary,
        )
        self._update_drive_last_sync_label()
    
    def _update_drive_status_label(self):
        if not self.drive_enabled_var.get():
            self.drive_status_label.configure(text="Drive sync off", text_color="gray")
            self.drive_connect_btn.configure(text="Connect")
            return
        if self.drive_sync.is_connected():
            email = self.drive_sync.connected_email() or "connected"
            where = self.drive_sync.location_description()
            self.drive_status_label.configure(
                text=f"Connected as {email} → {where}",
                text_color="#2B7A3E",
            )
            self.drive_connect_btn.configure(text="Disconnect")
        else:
            self.drive_status_label.configure(text="Not connected", text_color="orange")
            self.drive_connect_btn.configure(text="Connect")
    
    def _on_drive_enabled_toggle(self):
        self._save_settings()
        self._update_drive_sync_controls()
        if self.drive_enabled_var.get():
            if not self.drive_library_var.get() and not self.drive_epubs_var.get():
                messagebox.showwarning(
                    "Nothing to sync",
                    "Enable Sync library and/or Sync EPUBs, or turn Drive sync off."
                )
            self._schedule_drive_sync(silent=True)
        else:
            self._update_status("Drive sync disabled (local library unchanged)")
    
    def _on_drive_option_change(self):
        self._save_settings()
        if (
            self.drive_enabled_var.get()
            and not self.drive_library_var.get()
            and not self.drive_epubs_var.get()
        ):
            messagebox.showwarning(
                "Nothing to sync",
                "Both sync targets are off. Enable at least one, or disable Drive sync."
            )
    
    def _on_drive_change_folder(self):
        """Dialog to choose a custom My Drive folder name or paste a folder URL."""
        if not self.drive_enabled_var.get():
            return
        
        popup = ctk.CTkToplevel(self)
        popup.title("Google Drive folder")
        popup.geometry("520x280")
        popup.transient(self)
        popup.grab_set()
        popup.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - 520) // 2
        y = self.winfo_y() + (self.winfo_height() - 280) // 2
        popup.geometry(f"+{x}+{y}")
        
        ctk.CTkLabel(
            popup,
            text="Choose where library.json and books/ are stored on Google Drive.",
            font=("", 12),
            wraplength=480,
            justify="left",
        ).pack(padx=16, pady=(16, 8), anchor="w")
        
        ctk.CTkLabel(popup, text="Folder name in My Drive:", font=("", 12)).pack(
            padx=16, pady=(8, 2), anchor="w"
        )
        name_entry = ctk.CTkEntry(popup, width=460)
        name_entry.insert(0, self.settings.get('drive_folder_name') or DRIVE_FOLDER_NAME)
        name_entry.pack(padx=16, pady=(0, 8), anchor="w")
        
        ctk.CTkLabel(
            popup,
            text="Or paste an existing Drive folder link / id (optional):",
            font=("", 12),
        ).pack(padx=16, pady=(4, 2), anchor="w")
        url_entry = ctk.CTkEntry(
            popup, width=460, placeholder_text="https://drive.google.com/drive/folders/..."
        )
        existing_id = (self.settings.get('drive_folder_id') or '').strip()
        if existing_id:
            url_entry.insert(0, f"https://drive.google.com/drive/folders/{existing_id}")
        url_entry.pack(padx=16, pady=(0, 8), anchor="w")
        
        ctk.CTkLabel(
            popup,
            text="Tip: leave the link empty to create/reuse the folder name under My Drive.",
            font=("", 11),
            text_color="gray",
            wraplength=480,
            justify="left",
        ).pack(padx=16, pady=(0, 10), anchor="w")
        
        btn_row = ctk.CTkFrame(popup, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(0, 16))
        
        def on_default():
            name_entry.delete(0, "end")
            name_entry.insert(0, DRIVE_FOLDER_NAME)
            url_entry.delete(0, "end")
        
        def on_save():
            folder_name = name_entry.get().strip() or DRIVE_FOLDER_NAME
            folder_url = url_entry.get().strip()
            popup.destroy()
            
            def worker():
                try:
                    if not self.drive_sync.is_connected():
                        if not self.drive_sync.try_restore_session():
                            raise DriveSyncError("Connect to Google Drive first.")
                    self.drive_sync.set_custom_folder(folder_name, folder_url)
                    # Reload settings that drive_sync wrote
                    self.settings = load_settings()
                    self.after(0, self._update_drive_folder_help)
                    self.after(0, self._update_drive_status_label)
                    self.after(0, lambda: self._update_status(
                        f"Drive folder set to My Drive / {self.drive_sync.configured_folder_name()}"
                    ))
                    self.after(0, lambda: self._schedule_drive_sync(silent=False))
                except Exception as e:
                    err = str(e)
                    print(f"Change Drive folder failed: {err}")
                    self.after(0, lambda msg=err: messagebox.showerror(
                        "Change folder failed", msg
                    ))
            
            threading.Thread(target=worker, daemon=True).start()
        
        ctk.CTkButton(
            btn_row, text="Use default name", width=130,
            command=on_default, fg_color="gray40", hover_color="gray30",
        ).pack(side="left")
        ctk.CTkButton(btn_row, text="Cancel", width=90, command=popup.destroy,
                      fg_color="gray40", hover_color="gray30").pack(side="right", padx=4)
        ctk.CTkButton(btn_row, text="Save", width=90, command=on_save,
                      fg_color="#2B7A3E", hover_color="#236332").pack(side="right", padx=4)
    
    def _on_drive_open_folder(self):
        import webbrowser
        link = self.drive_sync.folder_web_link()
        if not link:
            if not self.drive_sync.is_connected():
                messagebox.showinfo("Open folder", "Connect to Google Drive first.")
                return
            
            def worker():
                try:
                    self.drive_sync.ensure_folder_layout()
                    url = self.drive_sync.folder_web_link()
                    if url:
                        webbrowser.open(url)
                    else:
                        self.after(0, lambda: messagebox.showinfo(
                            "Open folder",
                            "Could not resolve the Drive folder yet. Try Sync Now first."
                        ))
                except Exception as e:
                    err = str(e)
                    print(f"Open folder failed: {err}")
                    self.after(0, lambda msg=err: messagebox.showerror("Open folder", msg))
            
            threading.Thread(target=worker, daemon=True).start()
            return
        webbrowser.open(link)
    
    def _on_drive_connect(self):
        if not self.drive_enabled_var.get():
            return
        if self.drive_sync.is_connected():
            self.drive_sync.logout()
            self._update_drive_status_label()
            self._update_status("Disconnected from Google Drive")
            return
        
        if not self.drive_sync.client_configured():
            messagebox.showinfo("Google OAuth setup", oauth_setup_instructions())
            return
        
        self._save_settings()
        self.drive_connect_btn.configure(state="disabled", text="Signing in...")
        self._update_status("Opening browser for Google sign-in...")
        
        def worker():
            try:
                email = self.drive_sync.login()
                self.after(0, lambda em=email: self._on_drive_login_done(True, em, None))
            except Exception as e:
                err = str(e)
                print(f"Drive connect failed: {err}")
                self.after(0, lambda msg=err: self._on_drive_login_done(False, "", msg))
        
        threading.Thread(target=worker, daemon=True).start()
    
    def _on_drive_login_done(self, ok: bool, email: str, error: Optional[str]):
        self.drive_connect_btn.configure(state="normal")
        self._update_drive_status_label()
        if ok:
            where = self.drive_sync.location_description()
            self._update_status(f"Connected ({email or 'ok'}) → {where}")
            self._schedule_drive_sync(silent=False)
        else:
            self.drive_connect_btn.configure(text="Connect")
            messagebox.showerror("Google Drive", error or "Sign-in failed")
            self._update_status("Google sign-in failed")
    
    def _on_drive_sync_now(self):
        if not self.drive_enabled_var.get():
            return
        if not self.drive_library_var.get() and not self.drive_epubs_var.get():
            messagebox.showwarning(
                "Nothing to sync",
                "Enable Sync library and/or Sync EPUBs first."
            )
            return
        self._schedule_drive_sync(silent=False)
    
    def _drive_startup_sync(self):
        """Restore token and sync quietly after app launch."""
        if not self.drive_enabled_var.get():
            return
        
        def worker():
            try:
                if self.drive_sync.try_restore_session():
                    self.after(0, self._update_drive_status_label)
                    self.after(0, lambda: self._schedule_drive_sync(silent=True))
                else:
                    self.after(0, self._update_drive_status_label)
            except Exception:
                self.after(0, self._update_drive_status_label)
        
        threading.Thread(target=worker, daemon=True).start()
    
    def _schedule_drive_sync(self, silent: bool = True):
        if not self.drive_enabled_var.get() or self._drive_syncing:
            return
        if not self.drive_library_var.get() and not self.drive_epubs_var.get():
            return
        
        self._drive_syncing = True
        if not silent:
            self._update_status("Syncing with Google Drive...")
            self.drive_sync_now_btn.configure(state="disabled", text="Syncing...")
        
        def worker():
            msg = ""
            err = None
            try:
                if not self.drive_sync.is_connected():
                    if not self.drive_sync.try_restore_session():
                        raise DriveSyncError("Not connected. Click Connect first.")
                
                self.drive_sync.reset_layout_cache()
                self.drive_sync.ensure_folder_layout()
                where = self.drive_sync.location_description()
                novel_count = 0
                uploaded = 0
                
                if self.drive_library_var.get():
                    merged = self.drive_sync.sync_library_with_store(self.library_store)
                    novel_count = len(merged.library)
                
                if self.drive_epubs_var.get():
                    self._remote_books = self.drive_sync.list_remote_books()
                    # Upload local EPUBs missing from *this* Drive location
                    for entry in self.library_store.get_library():
                        local = entry.output_path
                        if not local or not Path(local).is_file():
                            continue
                        name = entry.epub_filename or Path(local).name
                        if name in self._remote_books:
                            # Refresh stored id for current location
                            if entry.drive_file_id != self._remote_books[name]:
                                self.library_store.update_drive_file(
                                    entry.source_url,
                                    drive_file_id=self._remote_books[name],
                                    epub_filename=name,
                                )
                            continue
                        file_id = self.drive_sync.upload_epub(local, name)
                        self.library_store.update_drive_file(
                            entry.source_url,
                            drive_file_id=file_id,
                            epub_filename=name,
                        )
                        self._remote_books[name] = file_id
                        uploaded += 1
                    if self.drive_library_var.get() and uploaded:
                        self.drive_sync.push_library(self.library_store.get_data())
                
                parts = []
                if self.drive_library_var.get():
                    parts.append(f"{novel_count} novel(s) in library.json")
                if self.drive_epubs_var.get():
                    parts.append(
                        f"{uploaded} EPUB uploaded"
                        if uploaded
                        else f"{len(self._remote_books)} EPUB(s) on Drive"
                    )
                msg = f"Synced to {where}: " + ", ".join(parts)
            except Exception as e:
                import traceback
                traceback.print_exc()
                err = str(e)
            
            self.after(0, lambda m=msg, er=err, si=silent: self._on_drive_sync_done(m, er, si))
        
        threading.Thread(target=worker, daemon=True).start()
    
    def _on_drive_sync_done(self, msg: str, err: Optional[str], silent: bool):
        self._drive_syncing = False
        self.drive_sync_now_btn.configure(state="normal", text="Sync Now")
        self._update_drive_status_label()
        self._refresh_library_ui()
        if err:
            print(f"Drive sync failed: {err}")
            self._update_status(f"Drive sync failed: {err[:120]}")
            if not silent:
                messagebox.showerror("Google Drive sync", err)
        else:
            self._record_drive_sync_success(msg)
            self._update_status(msg)
            if not silent:
                notify("Drive sync", msg)
    
    def _schedule_drive_push_after_download(self, source_url: str, output_path: str):
        if not self.drive_enabled_var.get():
            return
        if not self.drive_library_var.get() and not self.drive_epubs_var.get():
            return
        
        def worker():
            try:
                if not self.drive_sync.is_connected():
                    if not self.drive_sync.try_restore_session():
                        return
                
                epub_name = Path(output_path).name if output_path else ""
                if self.drive_epubs_var.get() and output_path and Path(output_path).is_file():
                    file_id = self.drive_sync.upload_epub(output_path, epub_name)
                    if source_url:
                        self.library_store.update_drive_file(
                            source_url,
                            drive_file_id=file_id,
                            epub_filename=epub_name,
                            output_path=output_path,
                        )
                        self._remote_books[epub_name] = file_id
                
                if self.drive_library_var.get():
                    self.drive_sync.push_library(self.library_store.get_data())
                
                summary = f"Saved + synced to {self.drive_sync.location_description()}"
                if epub_name:
                    summary += f": {epub_name}"
                self.after(0, lambda: self._record_drive_sync_success(summary))
                self.after(0, lambda: self._update_status(summary))
            except Exception as e:
                err = str(e)
                print(f"Drive push after download failed: {err}")
                self.after(0, lambda msg=err: self._update_status(
                    f"Saved locally (Drive sync failed: {msg[:60]})"
                ))
        
        threading.Thread(target=worker, daemon=True).start()
    
    def _on_drive_download_epub(self, entry, file_id: str):
        if self.is_downloading:
            return
        title = entry.translated_title or entry.title or "novel"
        dest = Path(self._epub_path(
            self._get_downloads_folder(),
            title,
            preferred_name=entry.epub_filename or "",
            preferred_path=entry.output_path or "",
        ))
        
        self._update_status(f"Downloading from Drive: {dest.name}...")
        
        def worker():
            try:
                path = self.drive_sync.download_epub(file_id, str(dest))
                self.library_store.update_drive_file(
                    entry.source_url,
                    drive_file_id=file_id,
                    epub_filename=dest.name,
                    output_path=path,
                )
                self.after(0, lambda p=path: self._update_status(f"Downloaded from Drive: {p}"))
                self.after(0, lambda n=dest.name: notify("Download complete", n))
                self.after(0, self._refresh_library_ui)
                self.after(0, lambda p=path: messagebox.showinfo(
                    "Downloaded", f"EPUB saved to:\n{p}"
                ))
            except Exception as e:
                err = str(e)
                print(f"Drive download failed: {err}")
                self.after(0, lambda msg=err: messagebox.showerror(
                    "Drive download failed", msg
                ))
        
        threading.Thread(target=worker, daemon=True).start()
    
    # ------------------------------------------------------------------
    # Clipboard watcher
    # ------------------------------------------------------------------
    
    def _refresh_clipboard_label(self):
        if not hasattr(self, 'clipboard_cb'):
            return
        if self.clipboard_var.get():
            self.clipboard_cb.configure(text="Watch clipboard for URLs (active)")
        else:
            self.clipboard_cb.configure(text="Watch clipboard for URLs")

    def _on_clipboard_toggle(self):
        self._save_settings()
        self._refresh_clipboard_label()
        if self.clipboard_var.get():
            self._update_status("Clipboard watcher on — copy a novel URL to queue it")
            # Ignore whatever is currently on the clipboard so we only catch fresh copies
            try:
                self._clipboard_last = self.clipboard_get()
            except Exception:
                self._clipboard_last = ""
    
    def _poll_clipboard(self):
        """Periodic clipboard check; only processes URL-like clipboard text."""
        try:
            if self.clipboard_var.get() and not self.is_downloading:
                # Skip when minimized / unmapped — avoids needless OS clipboard churn
                try:
                    visible = self.winfo_viewable() and self.state() != "iconic"
                except Exception:
                    visible = True
                if visible:
                    try:
                        text = self.clipboard_get()
                    except Exception:
                        text = None
                    
                    if text and text != self._clipboard_last:
                        self._clipboard_last = text
                        # Ignore non-URL clipboard noise (passwords, snippets, etc.)
                        if looks_like_url(text):
                            from core.security import is_fetch_url_safe
                            urls = [u for u in extract_urls(text) if is_fetch_url_safe(u)]
                            fresh = [u for u in urls if u not in self._clipboard_seen_urls]
                            if fresh:
                                for u in fresh:
                                    self._clipboard_seen_urls.add(u)
                                if len(self._clipboard_seen_urls) > 200:
                                    self._clipboard_seen_urls = set(list(self._clipboard_seen_urls)[-100:])
                                self._handle_clipboard_urls(fresh)
        except Exception:
            pass
        finally:
            self.after(3000, self._poll_clipboard)
    
    def _handle_clipboard_urls(self, urls: List[str]):
        """Add clipboard URLs to the multi block (and single field if empty)."""
        if not urls:
            return
        
        added = self._multi_append_urls(urls)
        if not added:
            return
        
        # If single URL box is empty, fill with the first new URL for convenience
        if not self.url_entry.get().strip():
            self.url_entry.insert(0, added[0])
        
        label = added[0] if len(added) == 1 else f"{len(added)} URLs"
        self._update_status(f"Queued from clipboard: {label}")
        
        # Show the Multi queue so the user sees what was added
        if not self.multi_mode and not self.is_downloading:
            if self.library_mode or not self.chapters:
                self.mode_switch.set("Multi")
                self._on_mode_change("Multi")
    
    def _load_cover(self, url: str, generation: int):
        """Load cover image from URL in background (local cache first)."""
        try:
            from core.security import UnsafeURLError, validate_fetch_url
            validate_fetch_url(url, allow_http=True)
            
            data = self.cache.get_cover(cover_url=url)
            if not data:
                print(f"Loading cover from: {url}")
                response = http_session.get(url, timeout=15)
                response.raise_for_status()
                data = response.content
                if data:
                    ctype = ""
                    try:
                        ctype = response.headers.get("content-type", "") or ""
                    except Exception:
                        pass
                    self.cache.put_cover(data, cover_url=url, content_type=ctype)
            
            # Load image with PIL
            image = Image.open(BytesIO(data))
            
            # Resize to fit (100x140 max, keep aspect ratio)
            image.thumbnail((100, 140), Image.Resampling.LANCZOS)
            
            # Convert to CTkImage
            ctk_image = ctk.CTkImage(light_image=image, dark_image=image, size=image.size)
            
            if generation != self._fetch_generation:
                return  # A newer fetch started; don't overwrite its cover
            
            # Update UI in main thread
            self.after(0, lambda: self._set_cover_image(ctk_image))
            print("Cover loaded successfully")
            
        except Exception as e:
            print(f"Failed to load cover: {e}")
    
    def _set_cover_image(self, image):
        """Set the cover image in the UI."""
        self.cover_image = image  # Keep reference
        self.cover_label.configure(image=image, text="")
    
    def _translate_title(self, title: str, generation: int):
        """Translate the title to English in background."""
        try:
            print(f"Translating title: {title}")
            translator = self._make_translator(1)
            translated = translator.translate_text(title)
            
            if generation != self._fetch_generation:
                return  # A newer fetch started; don't overwrite its title
            
            if translated and translated != title:
                self.translated_title = translated
                self.after(0, lambda t=translated: self.eng_title_label.configure(text=t, text_color="white"))
                print(f"Translated title: {translated}")
            else:
                self.translated_title = title
                self.after(0, lambda: self.eng_title_label.configure(text="(same as original)", text_color="gray"))
                
        except Exception as e:
            print(f"Failed to translate title: {e}")
            if generation != self._fetch_generation:
                return
            self.translated_title = title
            self.after(0, lambda: self.eng_title_label.configure(text="(translation failed)", text_color="gray"))
    
    def _update_status(self, text: str):
        """Update status label."""
        self.status_label.configure(text=text)
    
    def _show_error(self, message: str):
        """Show error message."""
        self.status_label.configure(text="Error")
        messagebox.showerror("Error", message)
    
    def _on_auto_update_toggle(self):
        """Handle auto-update checkbox toggle."""
        set_auto_check_updates(self.auto_update_var.get())
    
    def _auto_check_updates(self):
        """Auto-check for updates on startup (silent unless update available)."""
        def callback(has_update, latest_version, message):
            if has_update:
                self.after(0, lambda: self._show_update_available(latest_version, message))
        
        check_for_updates_async(callback)
    
    def _on_check_updates(self):
        """Handle manual check for updates button click."""
        self.update_btn.configure(state="disabled", text="Checking...")
        
        def callback(has_update, latest_version, message):
            self.after(0, lambda: self.update_btn.configure(state="normal", text="Check for Updates"))
            if has_update:
                self.after(0, lambda: self._show_update_available(latest_version, message))
            else:
                self.after(0, lambda: messagebox.showinfo("Up to Date", message))
        
        check_for_updates_async(callback)
    
    def _show_update_available(self, latest_version: str, message: str):
        """Show update available dialog and offer to download."""
        result = messagebox.askyesno(
            "Update Available",
            f"{message}\n\nWould you like to download and install the update?",
            icon="info"
        )
        
        if result:
            self._download_update()
    
    def _download_update(self):
        """Download and install the update."""
        # Import here to check if frozen
        from core.updater import is_frozen
        
        # Create progress dialog
        progress_window = ctk.CTkToplevel(self)
        progress_window.title("Updating...")
        progress_window.geometry("400x150")
        progress_window.transient(self)
        progress_window.grab_set()
        
        # Center the window
        progress_window.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - 400) // 2
        y = self.winfo_y() + (self.winfo_height() - 150) // 2
        progress_window.geometry(f"+{x}+{y}")
        
        # Progress UI
        ctk.CTkLabel(progress_window, text="Downloading update...", font=("", 14)).pack(pady=(20, 10))
        
        progress_bar = ctk.CTkProgressBar(progress_window, width=350)
        progress_bar.pack(pady=10)
        progress_bar.set(0)
        
        status_label = ctk.CTkLabel(progress_window, text="Connecting...", font=("", 11))
        status_label.pack(pady=5)
        
        def progress_callback(current, total, status):
            self.after(0, lambda: progress_bar.set(current / total))
            self.after(0, lambda s=status: status_label.configure(text=s))
        
        def completion_callback(success, message):
            self.after(0, progress_window.destroy)
            if success:
                self.after(0, lambda: self._handle_update_complete(message))
            else:
                self.after(0, lambda: messagebox.showerror("Update Failed", message))
        
        download_update_async(progress_callback, completion_callback)
    
    def _handle_update_complete(self, message: str):
        """Handle successful update completion."""
        from core.updater import is_frozen

        messagebox.showinfo("Update Complete", message)

        # Frozen Windows updates swap the exe in-place and schedule a hidden
        # helper to relaunch after this process exits (avoids console flash and
        # PyInstaller pythonXX.dll load races from starting too early).
        if is_frozen():
            self.after(400, self._on_close)


def main():
    log_path = setup_logging(get_data_dir())
    print(f"Logging to: {log_path}")
    app = HuaEPUBApp()
    app.mainloop()


if __name__ == "__main__":
    main()
