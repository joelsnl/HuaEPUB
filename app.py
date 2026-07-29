#!/usr/bin/env python3
# Author: joelsnl and Anthropic Claude
"""
Novel Downloader & Translator
A standalone GUI application for downloading and translating web novels to EPUB.

Based on WebToEpub extension and fixTranslate.py
"""

import os
import sys
import time
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path
from typing import List, Optional
from io import BytesIO

import customtkinter as ctk
from PIL import Image

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
from core.settings import load_settings, save_settings, get_app_dir
from core.cache import NovelCache
from core.logger import setup_logging

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


class NovelDownloaderApp(ctk.CTk):
    """Main application window."""
    
    def __init__(self):
        super().__init__()
        
        self.title(f"Novel Downloader & Translator v{get_current_version()}")
        self.geometry("900x700")
        self.minsize(800, 600)
        
        # Get app directory for auto-save
        self.app_dir = get_app_dir()
        
        # Persistent settings and caches
        self.settings = load_settings()
        self.output_dir = self.settings.get('output_dir', '') or ''
        self.cache = NovelCache(self.app_dir / 'cache.db')
        
        # State
        self.novel_info: Optional[NovelInfo] = None
        self.chapters: List[Chapter] = []
        self.parser = None
        self.is_downloading = False
        self.cancel_requested = False
        self.cover_image = None  # Store PhotoImage reference
        self.translated_title = None  # Store translated title
        
        # Generation counters to ignore results from stale background work
        self._fetch_generation = 0   # bumped on each new fetch
        self._list_generation = 0    # bumped each time the chapter list rebuilds
        
        # Multi-download mode state
        self.multi_mode = False
        self.multi_url_entries: List[ctk.CTkEntry] = []
        self.multi_novels: List[dict] = []  # [{url, parser, info, chapters, status, translated_title}]
        self.multi_result_labels: List[dict] = []  # UI labels for each novel row
        
        # Create UI
        self._create_ui()
        
        # Cleanup browser on close
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        
        # Auto-check for updates on startup (if enabled)
        if get_auto_check_updates():
            self.after(2000, self._auto_check_updates)  # Check after 2 seconds
    
    def _on_close(self):
        """Handle window close - persist settings and clean up."""
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
        self.settings['workers'] = self._get_workers()
        self.settings['output_dir'] = self.output_dir
        self.settings['translation_backend'] = (
            'libretranslate' if self.backend_menu.get() == 'LibreTranslate' else 'google'
        )
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
        
        # Configure grid
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        
        # === Mode Toggle + URL Input Section ===
        url_frame = ctk.CTkFrame(self)
        url_frame.grid(row=0, column=0, padx=10, pady=(10, 5), sticky="ew")
        url_frame.grid_columnconfigure(1, weight=1)
        
        # Mode toggle
        self.mode_switch = ctk.CTkSegmentedButton(
            url_frame, values=["Single", "Multi"],
            command=self._on_mode_change, width=140
        )
        self.mode_switch.set("Single")
        self.mode_switch.grid(row=0, column=0, padx=(10, 5), pady=10)
        
        # Single-mode URL entry
        self.single_url_frame = ctk.CTkFrame(url_frame, fg_color="transparent")
        self.single_url_frame.grid(row=0, column=1, columnspan=2, padx=0, pady=0, sticky="ew")
        self.single_url_frame.grid_columnconfigure(0, weight=1)
        
        self.url_entry = ctk.CTkEntry(self.single_url_frame, placeholder_text="Enter novel URL (e.g., https://twkan.com/book/12345.html)")
        self.url_entry.grid(row=0, column=0, padx=5, pady=10, sticky="ew")
        
        self.fetch_btn = ctk.CTkButton(self.single_url_frame, text="Fetch Chapters", command=self._on_fetch)
        self.fetch_btn.grid(row=0, column=1, padx=(5, 10), pady=10)
        
        # === Single Mode: Novel Info Section (with cover preview) ===
        self.info_frame = ctk.CTkFrame(self)
        self.info_frame.grid(row=1, column=0, padx=10, pady=5, sticky="ew")
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
        self.list_frame.grid(row=2, column=0, padx=10, pady=5, sticky="nsew")
        self.list_frame.grid_columnconfigure(0, weight=1)
        self.list_frame.grid_rowconfigure(1, weight=1)
        
        # Selection buttons
        btn_frame = ctk.CTkFrame(self.list_frame, fg_color="transparent")
        btn_frame.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        
        ctk.CTkButton(btn_frame, text="Select All", width=90, command=self._select_all).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text="Select None", width=90, command=self._select_none).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text="Invert", width=70, command=self._invert_selection).pack(side="left", padx=4)
        
        # Range selection (e.g. chapters 200-450 without clicking checkboxes)
        ctk.CTkLabel(btn_frame, text="Range:").pack(side="left", padx=(15, 2))
        self.range_from_entry = ctk.CTkEntry(btn_frame, width=55, placeholder_text="from")
        self.range_from_entry.pack(side="left", padx=2)
        ctk.CTkLabel(btn_frame, text="-").pack(side="left")
        self.range_to_entry = ctk.CTkEntry(btn_frame, width=55, placeholder_text="to")
        self.range_to_entry.pack(side="left", padx=2)
        ctk.CTkButton(btn_frame, text="Select Range", width=95, command=self._select_range).pack(side="left", padx=4)
        
        self.selected_label = ctk.CTkLabel(btn_frame, text="Selected: 0")
        self.selected_label.pack(side="right", padx=10)
        
        # Chapter listbox with checkboxes
        self.chapter_frame = ctk.CTkScrollableFrame(self.list_frame)
        self.chapter_frame.grid(row=1, column=0, padx=5, pady=5, sticky="nsew")
        self.chapter_frame.grid_columnconfigure(0, weight=1)
        
        self.chapter_vars: List[ctk.BooleanVar] = []
        self.chapter_checkboxes: List[ctk.CTkCheckBox] = []
        
        # === Multi Mode UI (hidden by default) ===
        self.multi_frame = ctk.CTkFrame(self)
        # Not gridded yet - shown when multi mode is activated
        self.multi_frame.grid_columnconfigure(0, weight=1)
        self.multi_frame.grid_rowconfigure(1, weight=1)
        
        # URL input area with scrollable list
        multi_url_section = ctk.CTkFrame(self.multi_frame)
        multi_url_section.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        multi_url_section.grid_columnconfigure(0, weight=1)
        
        multi_url_header = ctk.CTkFrame(multi_url_section, fg_color="transparent")
        multi_url_header.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        
        ctk.CTkLabel(multi_url_header, text="Novel URLs (max 7):", font=("", 13, "bold")).pack(side="left", padx=5)
        
        self.multi_add_btn = ctk.CTkButton(
            multi_url_header, text="+ Add URL", width=90, height=28,
            command=self._multi_add_url
        )
        self.multi_add_btn.pack(side="right", padx=5)
        
        self.multi_remove_btn = ctk.CTkButton(
            multi_url_header, text="- Remove", width=90, height=28,
            command=self._multi_remove_url,
            fg_color="gray40", hover_color="gray30"
        )
        self.multi_remove_btn.pack(side="right", padx=5)
        
        self.multi_fetch_btn = ctk.CTkButton(
            multi_url_header, text="Fetch All", width=100, height=28,
            command=self._on_multi_fetch, fg_color="#2B7A3E", hover_color="#236332"
        )
        self.multi_fetch_btn.pack(side="right", padx=5)
        
        # URL entries container
        self.multi_url_container = ctk.CTkFrame(multi_url_section, fg_color="transparent")
        self.multi_url_container.grid(row=1, column=0, padx=5, pady=(0, 5), sticky="ew")
        self.multi_url_container.grid_columnconfigure(1, weight=1)
        
        # Start with 2 URL fields
        for i in range(2):
            self._multi_create_url_row(i)
        
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
        
        # === Options Section ===
        options_frame = ctk.CTkFrame(self)
        options_frame.grid(row=3, column=0, padx=10, pady=5, sticky="ew")
        
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
        progress_frame.grid(row=4, column=0, padx=10, pady=5, sticky="ew")
        progress_frame.grid_columnconfigure(0, weight=1)
        
        self.progress_bar = ctk.CTkProgressBar(progress_frame)
        self.progress_bar.grid(row=0, column=0, padx=10, pady=(10, 5), sticky="ew")
        self.progress_bar.set(0)
        
        self.status_label = ctk.CTkLabel(progress_frame, text="Ready")
        self.status_label.grid(row=1, column=0, padx=10, pady=(5, 10))
        
        # === Download Button ===
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=5, column=0, padx=10, pady=10)
        
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
        footer_frame.grid(row=6, column=0, padx=10, pady=(0, 10), sticky="ew")
        
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
            
            # Update UI in main thread
            self.after(0, self._update_chapter_list)
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            error_msg = f"Failed to fetch: {str(e)}"
            self.after(0, lambda msg=error_msg: self._show_error(msg))
        finally:
            self.after(0, lambda: self.fetch_btn.configure(state="normal"))
    
    def _update_chapter_list(self):
        """Update UI with fetched chapters."""
        if not self.novel_info:
            return
        
        # Update info labels
        self.title_label.configure(text=self.novel_info.title)
        self.author_label.configure(text=self.novel_info.author)
        self.chapters_label.configure(text=str(len(self.chapters)))
        
        # Load cover image in background
        if self.novel_info.cover_url:
            thread = threading.Thread(
                target=self._load_cover,
                args=(self.novel_info.cover_url, self._fetch_generation)
            )
            thread.daemon = True
            thread.start()
        
        # Translate title in background
        thread = threading.Thread(
            target=self._translate_title,
            args=(self.novel_info.title, self._fetch_generation)
        )
        thread.daemon = True
        thread.start()
        
        # Clear existing checkboxes
        for cb in self.chapter_checkboxes:
            cb.destroy()
        self.chapter_vars.clear()
        self.chapter_checkboxes.clear()
        
        # Add chapter checkboxes in batches so the UI doesn't freeze
        # on novels with thousands of chapters
        self._list_generation += 1
        self.download_btn.configure(state="disabled")
        self._update_status(f"Loading {len(self.chapters)} chapters...")
        self._add_chapter_rows(0, self._list_generation)
    
    def _add_chapter_rows(self, start: int, generation: int, batch_size: int = 100):
        """Create chapter checkboxes in batches to keep the UI responsive."""
        if generation != self._list_generation:
            return  # A newer fetch replaced this list
        
        end = min(start + batch_size, len(self.chapters))
        for idx in range(start, end):
            chapter = self.chapters[idx]
            var = ctk.BooleanVar(value=True)
            self.chapter_vars.append(var)
            
            cb = ctk.CTkCheckBox(
                self.chapter_frame,
                text=f"{idx + 1}. {chapter.title[:60]}{'...' if len(chapter.title) > 60 else ''}",
                variable=var,
                command=self._update_selected_count
            )
            cb.grid(row=idx, column=0, padx=5, pady=2, sticky="w")
            self.chapter_checkboxes.append(cb)
        
        if end < len(self.chapters):
            self._update_status(f"Loading chapters... {end}/{len(self.chapters)}")
            self.after(1, lambda: self._add_chapter_rows(end, generation, batch_size))
        else:
            self._update_selected_count()
            self.download_btn.configure(state="normal")
            self._update_status(f"Found {len(self.chapters)} chapters. Ready to download.")
    
    def _update_selected_count(self):
        """Update the selected count label."""
        count = sum(1 for var in self.chapter_vars if var.get())
        self.selected_label.configure(text=f"Selected: {count}")
    
    def _select_all(self):
        for var in self.chapter_vars:
            var.set(True)
        self._update_selected_count()
    
    def _select_none(self):
        for var in self.chapter_vars:
            var.set(False)
        self._update_selected_count()
    
    def _invert_selection(self):
        for var in self.chapter_vars:
            var.set(not var.get())
        self._update_selected_count()
    
    def _select_range(self):
        """Select only the chapters in the From-To range (1-based, inclusive)."""
        if not self.chapter_vars:
            return
        n = len(self.chapter_vars)
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
        for i, var in enumerate(self.chapter_vars):
            var.set(start - 1 <= i <= end - 1)
        self._update_selected_count()
    
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
        """Reset to the system Downloads folder."""
        self.output_dir = ''
        self._update_output_dir_label()
        self._save_settings()
    
    def _update_output_dir_label(self):
        if self.output_dir:
            self.output_dir_label.configure(text=self.output_dir, text_color="white")
        else:
            self.output_dir_label.configure(
                text=f"{self._system_downloads_folder()} (default)", text_color="gray"
            )
    
    def _on_download(self):
        """Handle download button click."""
        if not self.chapters or not self.novel_info:
            return
        
        # Get selected chapters
        selected_chapters = [
            self.chapters[i] for i, var in enumerate(self.chapter_vars) if var.get()
        ]
        
        if not selected_chapters:
            messagebox.showwarning("Warning", "Please select at least one chapter")
            return
        
        # Use translated title if available, otherwise original
        title_for_filename = self.translated_title if self.translated_title else self.novel_info.title
        
        # Create shortened filename like WebToEpub: "First...Last.epub"
        clean_title = self._create_short_filename(title_for_filename)
        
        if not clean_title:
            clean_title = "novel"
        
        # Save to central Downloads directory
        downloads_dir = self._get_downloads_folder()
        output_path = str(downloads_dir / f"{clean_title}.epub")
        
        # If file exists, add number
        counter = 1
        base_path = output_path
        while os.path.exists(output_path):
            output_path = base_path.replace(".epub", f" ({counter}).epub")
            counter += 1
        
        print(f"Auto-saving to: {output_path}")
        
        # Persist current options before starting
        self._save_settings()
        
        # Start download
        self.is_downloading = True
        self.cancel_requested = False
        self.download_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.fetch_btn.configure(state="disabled")
        
        thread = threading.Thread(
            target=self._download_thread,
            args=(selected_chapters, output_path)
        )
        thread.daemon = True
        thread.start()
    
    def _system_downloads_folder(self) -> Path:
        """Get the system Downloads folder (or app dir if it doesn't exist)."""
        if sys.platform == "win32":
            downloads = Path(os.environ.get("USERPROFILE", "")) / "Downloads"
        else:
            downloads = Path(os.environ.get("HOME", "")) / "Downloads"
        
        if not downloads.exists():
            downloads = self.app_dir
        
        return downloads
    
    def _get_downloads_folder(self) -> Path:
        """Get the output folder: user-chosen folder if set, else Downloads."""
        custom = (self.output_dir or '').strip()
        if custom:
            path = Path(custom)
            if path.exists():
                return path
            print(f"Warning: chosen output folder no longer exists: {custom}")
        return self._system_downloads_folder()
    
    def _create_short_filename(self, title: str, max_length: int = 40) -> str:
        """
        Create a shortened filename like WebToEpub does.
        Format: "FirstWord...LastWord" if title is too long.
        """
        # Clean the title - keep only safe characters
        clean = "".join(c for c in title if c.isalnum() or c in " ._-").strip()
        
        # Replace multiple spaces with single space
        clean = " ".join(clean.split())
        
        if not clean:
            return "novel"
        
        # If short enough, return as-is
        if len(clean) <= max_length:
            return clean
        
        # Split into words
        words = clean.split()
        
        if len(words) <= 2:
            # Just truncate if only 1-2 words
            return clean[:max_length]
        
        # Take first 2 words and last word, join with "..."
        first_part = " ".join(words[:2])
        last_part = words[-1]
        
        # Format: "First Two...Last"
        shortened = f"{first_part}...{last_part}"
        
        # If still too long, truncate first part
        if len(shortened) > max_length:
            available = max_length - len(last_part) - 3  # 3 for "..."
            first_part = first_part[:available].rstrip()
            shortened = f"{first_part}...{last_part}"
        
        return shortened
    
    @staticmethod
    def _format_eta(seconds: float) -> str:
        """Format a duration like '3m 20s' or '1h 12m'."""
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m {seconds % 60}s"
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    
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
        
        for idx, chapter in enumerate(chapters):
            if self.cancel_requested:
                raise _DownloadCancelled()
            
            set_progress((idx + 1) / total)
            
            eta_text = ""
            if idx >= 3:
                avg = (time.monotonic() - start_time) / idx
                eta_text = f"  (ETA {self._format_eta(avg * (total - idx))})"
            
            # Cached chapters are free - no fetch, no delay
            cached = self.cache.get_chapter(chapter.url) if use_cache else None
            if cached:
                chapter.content = cached
                set_status(f"Chapter [{idx+1}/{total}] from cache{eta_text}")
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
            
            if idx < total - 1:
                time.sleep(delay)
        
        # End-of-run retry pass: transient failures usually succeed here
        still_failed: List[str] = []
        if failed:
            set_status(f"Retrying {len(failed)} failed chapter(s)...")
            print(f"Retrying {len(failed)} failed chapter(s)...")
            for chapter in failed:
                if self.cancel_requested:
                    raise _DownloadCancelled()
                time.sleep(delay)
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
                    set_status=lambda s: self.after(0, lambda t=s: self._update_status(t)),
                    set_progress=lambda f: self.after(0, lambda p=f / 2: self.progress_bar.set(p)),
                )
            except _DownloadCancelled:
                self.after(0, lambda: self._update_status("Cancelled"))
                return
            
            # Phase 2: Build EPUB
            self.after(0, lambda: self._update_status("Building EPUB..."))
            
            # Create cleaner and translator
            cleaner = ContentCleaner() if self.clean_var.get() else None
            translator = None
            
            if self.translate_var.get():
                translator = self._make_translator(self._get_workers())

            # Build EPUB
            if translator:
                builder = TranslatedEPUBBuilder(cleaner=cleaner, translator=translator)
                
                def progress_cb(current, total_steps, status):
                    if self.cancel_requested:
                        translator.cancel()
                        return
                    progress = 0.5 + (current / total_steps) * 0.5
                    self.after(0, lambda p=progress: self.progress_bar.set(p))
                    self.after(0, lambda s=status: self._update_status(s))
                
                builder.build_with_translation(
                    self.novel_info,
                    chapters,
                    output_path,
                    progress_cb
                )
            else:
                builder = EPUBBuilder(cleaner=cleaner)
                
                def progress_cb(current, total_steps, status):
                    progress = 0.5 + (current / total_steps) * 0.5
                    self.after(0, lambda p=progress: self.progress_bar.set(p))
                    self.after(0, lambda s=status: self._update_status(s))
                
                builder.build(
                    self.novel_info,
                    chapters,
                    output_path,
                    progress_cb
                )
            
            # Done
            success_msg = f"EPUB saved to:\n{output_path}"
            if failed_chapters:
                shown = "\n".join(f"  • {t[:50]}" for t in failed_chapters[:10])
                if len(failed_chapters) > 10:
                    shown += f"\n  ... and {len(failed_chapters) - 10} more"
                success_msg += (
                    f"\n\nWarning: {len(failed_chapters)} chapter(s) could not be "
                    f"downloaded and contain placeholder text:\n{shown}"
                )
            
            self.after(0, lambda: self.progress_bar.set(1.0))
            self.after(0, lambda: self._update_status(f"Done! Saved to: {output_path}"))
            self.after(0, lambda m=success_msg: messagebox.showinfo("Success", m))
            
        except Exception as e:
            error_msg = f"Download failed: {str(e)}"
            self.after(0, lambda msg=error_msg: self._show_error(msg))
        finally:
            self.is_downloading = False
            self.after(0, lambda: self.download_btn.configure(state="normal"))
            self.after(0, lambda: self.cancel_btn.configure(state="disabled"))
            self.after(0, lambda: self.fetch_btn.configure(state="normal"))
    
    def _on_cancel(self):
        """Handle cancel button click."""
        self.cancel_requested = True
        self._update_status("Cancelling...")
    
    # ------------------------------------------------------------------
    # Multi-download mode
    # ------------------------------------------------------------------
    
    def _on_mode_change(self, value: str):
        """Toggle between Single and Multi download modes."""
        if self.is_downloading:
            self.mode_switch.set("Multi" if value == "Single" else "Single")
            return
        
        self.multi_mode = (value == "Multi")
        
        if self.multi_mode:
            # Hide single-mode UI
            self.single_url_frame.grid_remove()
            self.info_frame.grid_remove()
            self.list_frame.grid_remove()
            self.download_btn.pack_forget()
            # Show multi-mode UI
            self.multi_frame.grid(row=1, column=0, rowspan=2, padx=10, pady=5, sticky="nsew")
        else:
            # Hide multi-mode UI
            self.multi_frame.grid_remove()
            # Show single-mode UI
            self.single_url_frame.grid()
            self.info_frame.grid()
            self.list_frame.grid()
            self.download_btn.pack(side="left", padx=5)
    
    def _multi_create_url_row(self, index: int):
        """Create a single URL entry row for multi mode."""
        label = ctk.CTkLabel(self.multi_url_container, text=f"{index + 1}.", width=25)
        label.grid(row=index, column=0, padx=(5, 2), pady=3, sticky="w")
        
        entry = ctk.CTkEntry(self.multi_url_container, placeholder_text=f"Novel URL #{index + 1}")
        entry.grid(row=index, column=1, padx=2, pady=3, sticky="ew")
        
        self.multi_url_entries.append(entry)
    
    def _multi_add_url(self):
        """Add a new URL field in multi mode (max 7)."""
        if len(self.multi_url_entries) >= 7:
            messagebox.showinfo("Limit", "Maximum 7 novels in multi-download mode.")
            return
        self._multi_create_url_row(len(self.multi_url_entries))
    
    def _multi_remove_url(self):
        """Remove the last URL field in multi mode (min 2)."""
        if len(self.multi_url_entries) <= 2:
            return
        entry = self.multi_url_entries.pop()
        # Destroy the entry and its label
        row = len(self.multi_url_entries)
        for widget in self.multi_url_container.grid_slaves(row=row):
            widget.destroy()
    
    def _on_multi_fetch(self):
        """Fetch info for all URLs in multi mode."""
        urls = [e.get().strip() for e in self.multi_url_entries if e.get().strip()]
        if not urls:
            messagebox.showerror("Error", "Please enter at least one URL.")
            return
        
        # Validate all URLs have parsers
        parsers = []
        for url in urls:
            parser = get_parser_for_url(url)
            if not parser:
                messagebox.showerror("Error", f"Unsupported site:\n{url}")
                return
            parsers.append((url, parser))
        
        # Clear old results
        self.multi_novels.clear()
        for widget in self.multi_results_frame.winfo_children():
            widget.destroy()
        self.multi_result_labels.clear()
        
        # Create result rows
        for idx, (url, parser) in enumerate(parsers):
            self.multi_novels.append({
                'url': url, 'parser': parser,
                'info': None, 'chapters': [],
                'status': 'pending', 'translated_title': None
            })
            self._multi_create_result_row(idx, url)
        
        # Disable UI during fetch
        self.multi_fetch_btn.configure(state="disabled", text="Fetching...")
        self.multi_download_btn.configure(state="disabled")
        self.multi_add_btn.configure(state="disabled")
        self.multi_remove_btn.configure(state="disabled")
        self.mode_switch.configure(state="disabled")
        self.progress_bar.set(0)
        self._update_status("Fetching novel info...")
        
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
        self.after(0, lambda: self.multi_add_btn.configure(state="normal"))
        self.after(0, lambda: self.multi_remove_btn.configure(state="normal"))
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
        
        self.is_downloading = True
        self.cancel_requested = False
        self.multi_download_btn.configure(state="disabled")
        self.multi_fetch_btn.configure(state="disabled")
        self.multi_add_btn.configure(state="disabled")
        self.multi_remove_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.fetch_btn.configure(state="disabled")
        self.mode_switch.configure(state="disabled")
        
        thread = threading.Thread(target=self._multi_download_thread, args=(fetched,))
        thread.daemon = True
        thread.start()
    
    def _multi_download_thread(self, novels: list):
        """Download all novels sequentially in background."""
        total_novels = len(novels)
        results = []  # (title, path, success, error, failed_chapter_count)
        downloads_dir = self._get_downloads_folder()
        
        for novel_idx, novel in enumerate(novels):
            if self.cancel_requested:
                results.append((novel['translated_title'] or "Unknown", "", False, "Cancelled", 0))
                continue
            
            info = novel['info']
            chapters = novel['chapters']
            parser = novel['parser']
            title_for_filename = novel['translated_title'] if novel['translated_title'] else info.title
            
            # Find the index in the full multi_novels list for UI updates
            full_idx = self.multi_novels.index(novel)
            
            self.after(0, lambda i=full_idx: self.multi_result_labels[i]['status'].configure(
                text="Downloading", text_color="orange"
            ))
            self.after(0, lambda ni=novel_idx, tn=total_novels: self._update_status(
                f"Novel {ni + 1}/{tn}: Downloading chapters..."
            ))
            
            try:
                # Generate output path
                clean_title = self._create_short_filename(title_for_filename)
                if not clean_title:
                    clean_title = "novel"
                output_path = str(downloads_dir / f"{clean_title}.epub")
                counter = 1
                base_path = output_path
                while os.path.exists(output_path):
                    output_path = base_path.replace(".epub", f" ({counter}).epub")
                    counter += 1
                
                # Phase 1: Download chapters (with cache + retry pass)
                book_key = info.source_url if info else novel['url']
                
                def set_status(s, _ni=novel_idx, _tn=total_novels):
                    self.after(0, lambda t=s, ni=_ni, tn=_tn: self._update_status(
                        f"Novel {ni + 1}/{tn} — {t}"
                    ))
                
                def set_progress(f, _ni=novel_idx, _tn=total_novels):
                    overall = (_ni + f / 2) / _tn
                    self.after(0, lambda p=overall: self.progress_bar.set(p))
                
                try:
                    failed_titles = self._download_chapters_with_cache(
                        parser, chapters, book_key, set_status, set_progress
                    )
                except _DownloadCancelled:
                    raise Exception("Cancelled by user")
                failed_ch_count = len(failed_titles)
                
                # Phase 2: Build EPUB
                self.after(0, lambda ni=novel_idx, tn=total_novels: self._update_status(
                    f"Novel {ni + 1}/{tn}: Building EPUB..."
                ))
                
                cleaner = ContentCleaner() if self.clean_var.get() else None
                translator = None
                
                if self.translate_var.get():
                    translator = self._make_translator(self._get_workers())

                if translator:
                    builder = TranslatedEPUBBuilder(cleaner=cleaner, translator=translator)
                    
                    def progress_cb(current, total_steps, status, _ni=novel_idx, _tn=total_novels):
                        if self.cancel_requested:
                            translator.cancel()
                            return
                        overall = (_ni + 0.5 + (current / total_steps) * 0.5) / _tn
                        self.after(0, lambda p=overall: self.progress_bar.set(p))
                        self.after(0, lambda s=status, ni=_ni, tn=_tn: self._update_status(
                            f"Novel {ni + 1}/{tn}: {s}"
                        ))
                    
                    builder.build_with_translation(info, chapters, output_path, progress_cb)
                else:
                    builder = EPUBBuilder(cleaner=cleaner)
                    
                    def progress_cb(current, total_steps, status, _ni=novel_idx, _tn=total_novels):
                        overall = (_ni + 0.5 + (current / total_steps) * 0.5) / _tn
                        self.after(0, lambda p=overall: self.progress_bar.set(p))
                        self.after(0, lambda s=status, ni=_ni, tn=_tn: self._update_status(
                            f"Novel {ni + 1}/{tn}: {s}"
                        ))
                    
                    builder.build(info, chapters, output_path, progress_cb)
                
                results.append((title_for_filename, output_path, True, None, failed_ch_count))
                status_text = "Done" if not failed_ch_count else f"Done ({failed_ch_count} ch. failed)"
                self.after(0, lambda i=full_idx, s=status_text: self.multi_result_labels[i]['status'].configure(
                    text=s, text_color="#2B7A3E"
                ))
                
            except Exception as e:
                results.append((title_for_filename, "", False, str(e), 0))
                self.after(0, lambda i=full_idx: self.multi_result_labels[i]['status'].configure(
                    text="Failed", text_color="red"
                ))
        
        # All done - show summary
        self.after(0, lambda: self.progress_bar.set(1.0))
        
        success = [r for r in results if r[2]]
        failed = [r for r in results if not r[2]]
        
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
        
        self.after(0, lambda s=summary: self._update_status(
            f"Done! {len(success)}/{len(results)} novels downloaded."
        ))
        self.after(0, lambda s=summary: messagebox.showinfo("Multi-Download Complete", s))
        
        # Re-enable UI
        self.is_downloading = False
        self.after(0, lambda: self.multi_download_btn.configure(state="normal"))
        self.after(0, lambda: self.multi_fetch_btn.configure(state="normal"))
        self.after(0, lambda: self.multi_add_btn.configure(state="normal"))
        self.after(0, lambda: self.multi_remove_btn.configure(state="normal"))
        self.after(0, lambda: self.cancel_btn.configure(state="disabled"))
        self.after(0, lambda: self.fetch_btn.configure(state="normal"))
        self.after(0, lambda: self.mode_switch.configure(state="normal"))
    
    def _load_cover(self, url: str, generation: int):
        """Load cover image from URL in background."""
        try:
            print(f"Loading cover from: {url}")
            response = http_session.get(url, timeout=15)
            response.raise_for_status()
            
            # Load image with PIL
            image = Image.open(BytesIO(response.content))
            
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
        
        # If running as compiled executable, close the app so the helper script can replace it
        if is_frozen():
            self.after(500, self._on_close)


def main():
    log_path = setup_logging(get_app_dir())
    print(f"Logging to: {log_path}")
    app = NovelDownloaderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
