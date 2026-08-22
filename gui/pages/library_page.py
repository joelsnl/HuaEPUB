# Author: joelsnl and Anthropic Claude
from __future__ import annotations

import time
from typing import Dict, List, Optional

from PySide6.QtCore import QSize, Qt, Signal, Slot
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)


class _LibraryTile(QFrame):
    """Cover + title + status line for the Library grid."""

    def __init__(self, title: str, status: str, kind: str, pixmap: Optional[QPixmap], parent=None):
        super().__init__(parent)
        self.setObjectName("libraryTile")
        self.setFixedSize(132, 220)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 4)
        lay.setSpacing(2)

        self.cover = QLabel()
        self.cover.setFixedSize(110, 150)
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setStyleSheet("background:#3a3a3a;border-radius:4px;")
        if pixmap and not pixmap.isNull():
            self.cover.setPixmap(pixmap)
        else:
            self.cover.setText("No Cover")
            self.cover.setStyleSheet("background:#3a3a3a;border-radius:4px;color:#888;")
        lay.addWidget(self.cover, 0, Qt.AlignHCenter)

        self.title_lbl = QLabel(title)
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.title_lbl.setFixedHeight(34)
        self.title_lbl.setStyleSheet("font-size:11px;")
        lay.addWidget(self.title_lbl)

        self.status_lbl = QLabel(status or " ")
        self.status_lbl.setAlignment(Qt.AlignHCenter)
        self.status_lbl.setFixedHeight(18)
        lay.addWidget(self.status_lbl)
        self.set_status(status, kind)

    def set_status(self, status: str, kind: str):
        self.status_lbl.setText(status or " ")
        colors = {
            "update": "#f0b429",
            "current": "#6bbf6b",
            "checking": "#aaaaaa",
            "error": "#e74c3c",
        }
        color = colors.get(kind, "#888888")
        weight = "bold" if kind == "update" else "normal"
        self.status_lbl.setStyleSheet(f"color:{color};font-size:11px;font-weight:{weight};")


class LibraryPage(QWidget):
    check_requested = Signal()
    update_all_requested = Signal()
    update_selected = Signal(object)  # LibraryEntry
    open_selected = Signal(str)
    remove_selected = Signal(str)
    download_epub_selected = Signal(object)
    refresh_requested = Signal()
    drive_connect = Signal()
    drive_sync = Signal()
    drive_disconnect = Signal()
    drive_change_folder = Signal()
    drive_open_folder = Signal()
    view_changed = Signal(str)
    filter_changed = Signal(str)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.check_status: Dict[str, dict] = {}
        self._entries: List = []
        self._selected_url: Optional[str] = None
        self._view = session.settings.get("library_view", "grid") or "grid"
        self._filter = session.settings.get("library_filter", "all") or "all"

        root = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(QLabel(
            "Your library — covers & TOC stay on this device; Drive syncs library.json + EPUBs."
        ))
        header.addStretch(1)
        self.filter_all = QPushButton("All")
        self.filter_updates = QPushButton("Updates")
        self.view_grid = QPushButton("Grid")
        self.view_list = QPushButton("List")
        for b in (self.filter_all, self.filter_updates, self.view_grid, self.view_list):
            b.setObjectName("secondaryBtn")
            b.setCheckable(True)
        self.filter_all.clicked.connect(lambda: self._set_filter("all"))
        self.filter_updates.clicked.connect(lambda: self._set_filter("updates"))
        self.view_grid.clicked.connect(lambda: self._set_view("grid"))
        self.view_list.clicked.connect(lambda: self._set_view("list"))
        self.check_btn = QPushButton("Check updates")
        self.update_all_btn = QPushButton("Update All")
        self.update_all_btn.setObjectName("successBtn")
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setObjectName("secondaryBtn")
        self.check_btn.clicked.connect(self.check_requested.emit)
        self.update_all_btn.clicked.connect(self.update_all_requested.emit)
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)
        for w in (
            self.filter_all, self.filter_updates, self.view_grid, self.view_list,
            self.check_btn, self.update_all_btn, self.refresh_btn,
        ):
            header.addWidget(w)
        root.addLayout(header)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color:#aaa;")
        root.addWidget(self.status_label)

        # Drive panel
        self.drive_box = QGroupBox("Google Drive")
        drive_lay = QVBoxLayout(self.drive_box)
        self.drive_enabled = QCheckBox("Sync with Google Drive")
        self.drive_enabled.setChecked(bool(session.settings.get("drive_sync_enabled", False)))
        drive_lay.addWidget(self.drive_enabled)
        opts = QHBoxLayout()
        self.drive_library = QCheckBox("Sync library")
        self.drive_library.setChecked(bool(session.settings.get("drive_sync_library", True)))
        self.drive_epubs = QCheckBox("Sync EPUBs")
        self.drive_epubs.setChecked(bool(session.settings.get("drive_sync_epubs", True)))
        opts.addWidget(self.drive_library)
        opts.addWidget(self.drive_epubs)
        opts.addStretch(1)
        drive_lay.addLayout(opts)
        btns = QHBoxLayout()
        self.drive_connect_btn = QPushButton("Connect")
        self.drive_sync_btn = QPushButton("Sync Now")
        self.drive_folder_btn = QPushButton("Change folder")
        self.drive_open_btn = QPushButton("Open folder")
        self.drive_disconnect_btn = QPushButton("Disconnect")
        for b in (
            self.drive_connect_btn, self.drive_sync_btn, self.drive_folder_btn,
            self.drive_open_btn, self.drive_disconnect_btn,
        ):
            b.setObjectName("secondaryBtn")
            btns.addWidget(b)
        btns.addStretch(1)
        drive_lay.addLayout(btns)
        self.drive_status = QLabel("")
        self.drive_status.setStyleSheet("color:#aaa;")
        self.drive_status.setWordWrap(True)
        drive_lay.addWidget(self.drive_status)
        self.drive_connect_btn.clicked.connect(self.drive_connect.emit)
        self.drive_sync_btn.clicked.connect(self.drive_sync.emit)
        self.drive_folder_btn.clicked.connect(self.drive_change_folder.emit)
        self.drive_open_btn.clicked.connect(self.drive_open_folder.emit)
        self.drive_disconnect_btn.clicked.connect(self.drive_disconnect.emit)
        root.addWidget(self.drive_box)

        self.stack = QStackedWidget()
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setIconSize(QSize(110, 150))
        self.grid.setGridSize(QSize(140, 230))
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setSpacing(6)
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.grid.setUniformItemSizes(True)
        self.grid.itemSelectionChanged.connect(self._on_grid_select)
        self.grid.itemDoubleClicked.connect(self._on_grid_activate)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Title", "Chapters", "Status", "Updated"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_table_select)
        self.table.doubleClicked.connect(self._on_table_activate)

        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.table)
        root.addWidget(self.stack, 1)

        actions = QHBoxLayout()
        self.update_btn = QPushButton("Update")
        self.update_btn.setObjectName("successBtn")
        self.open_btn = QPushButton("Open URL")
        self.open_btn.setObjectName("secondaryBtn")
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setObjectName("secondaryBtn")
        self.dl_epub_btn = QPushButton("Download EPUB")
        self.update_btn.clicked.connect(self._emit_update)
        self.open_btn.clicked.connect(self._emit_open)
        self.remove_btn.clicked.connect(self._emit_remove)
        self.dl_epub_btn.clicked.connect(self._emit_dl_epub)
        for b in (self.update_btn, self.open_btn, self.remove_btn, self.dl_epub_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        root.addLayout(actions)

        self._sync_toggle_styles()
        self._apply_view()
        self._update_drive_enabled()
        self.drive_enabled.toggled.connect(self._update_drive_enabled)

    def _set_filter(self, value: str):
        self._filter = value
        self._sync_toggle_styles()
        self.filter_changed.emit(value)
        self.refresh()

    def _set_view(self, value: str):
        self._view = value
        self._sync_toggle_styles()
        self._apply_view()
        self.view_changed.emit(value)

    def _sync_toggle_styles(self):
        self.filter_all.setChecked(self._filter == "all")
        self.filter_updates.setChecked(self._filter == "updates")
        self.view_grid.setChecked(self._view == "grid")
        self.view_list.setChecked(self._view == "list")

    def _apply_view(self):
        self.stack.setCurrentWidget(self.grid if self._view == "grid" else self.table)

    def _update_drive_enabled(self):
        on = self.drive_enabled.isChecked()
        for w in (
            self.drive_library, self.drive_epubs, self.drive_connect_btn,
            self.drive_sync_btn, self.drive_folder_btn, self.drive_open_btn,
            self.drive_disconnect_btn,
        ):
            w.setEnabled(on)

    def set_drive_busy(self, busy: bool):
        self.drive_sync_btn.setEnabled(not busy and self.drive_enabled.isChecked())
        self.drive_sync_btn.setText("Syncing…" if busy else "Sync Now")

    def show_all(self):
        """Ensure the All filter is active (Updates hides novels until Check runs)."""
        if self._filter != "all":
            self._set_filter("all")
        else:
            self.refresh()

    def filtered_entries(self):
        entries = self.session.library_store.get_library()
        if self._filter == "updates":
            entries = [
                e for e in entries
                if (self.check_status.get(e.source_url) or {}).get("state") == "update"
                and int((self.check_status.get(e.source_url) or {}).get("new_count") or 0) > 0
            ]
        return entries

    def refresh(self):
        selected = self._selected_url
        all_entries = self.session.library_store.get_library()
        self._entries = self.filtered_entries()
        self.grid.clear()
        self.table.setRowCount(0)
        for entry in self._entries:
            title = entry.translated_title or entry.title or entry.source_url
            st, kind = self._status_display(entry.source_url)
            pix = self._cover_pixmap(entry)

            # Icon + text (not setItemWidget): macOS IconMode often paints
            # embedded widgets as blank, which looked like an empty library.
            status_line = st.strip() or "—"
            item = QListWidgetItem(f"{title[:40]}\n{status_line}")
            item.setData(Qt.UserRole, entry.source_url)
            item.setToolTip(f"{title}\n{status_line}")
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            item.setSizeHint(QSize(140, 210))
            if pix and not pix.isNull():
                item.setIcon(QIcon(pix))
            else:
                # Placeholder so tiles keep a consistent cover-sized icon area
                blank = QPixmap(110, 150)
                blank.fill(Qt.GlobalColor.darkGray)
                item.setIcon(QIcon(blank))
            self.grid.addItem(item)

            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(title[:80]))
            self.table.setItem(r, 1, QTableWidgetItem(str(entry.chapter_count or "")))
            status_item = QTableWidgetItem(st.strip())
            if kind == "update":
                status_item.setForeground(Qt.yellow)
            elif kind == "current":
                status_item.setForeground(Qt.green)
            elif kind == "error":
                status_item.setForeground(Qt.red)
            self.table.setItem(r, 2, status_item)
            when = ""
            if entry.last_downloaded_at:
                try:
                    when = time.strftime("%Y-%m-%d", time.localtime(entry.last_downloaded_at))
                except Exception:
                    pass
            self.table.setItem(r, 3, QTableWidgetItem(when))
            self.table.item(r, 0).setData(Qt.UserRole, entry.source_url)

        if selected:
            self._reselect(selected)

        total = len(all_entries)
        shown = len(self._entries)
        if self._filter == "updates":
            if total and not shown:
                self.status_label.setText(
                    f"{total} novel(s) in library — none flagged yet. "
                    "Click All, or run Check updates."
                )
            else:
                self.status_label.setText(
                    f"Updates filter: {shown}/{total} novel(s) with new chapters"
                )
        else:
            self.status_label.setText(f"{total} novel(s) in library")

        self.update_all_btn.setEnabled(any(
            (self.check_status.get(e.source_url) or {}).get("state") == "update"
            for e in all_entries
        ))

    def _reselect(self, url: str):
        for i in range(self.grid.count()):
            it = self.grid.item(i)
            if it and it.data(Qt.UserRole) == url:
                self.grid.setCurrentItem(it)
                break
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.data(Qt.UserRole) == url:
                self.table.selectRow(r)
                break

    def _cover_pixmap(self, entry) -> Optional[QPixmap]:
        data = self.session.cache.get_cover(
            cover_url=entry.cover_url or "", source_url=entry.source_url or ""
        )
        if not data:
            return None
        pix = QPixmap()
        if pix.loadFromData(data):
            return pix.scaled(110, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return None

    def _status_display(self, url: str) -> tuple[str, str]:
        info = self.check_status.get(url) or {}
        state = info.get("state", "")
        if state == "checking":
            return "Checking…", "checking"
        if state == "update":
            n = int(info.get("new_count") or 0)
            return (f"{n} new" if n else "Update"), "update"
        if state == "current":
            return "Up to date", "current"
        if state == "error":
            err = (info.get("error") or "Failed")[:36]
            return f"Failed: {err}", "error"
        return "", ""

    def _status_text(self, url: str) -> str:
        return self._status_display(url)[0]

    @Slot(str, object)
    def apply_entry_status(self, url: str, st: object):
        st = dict(st or {})
        self.check_status[url] = st
        # Update just the matching tile/row when possible (avoids full rebuild flicker)
        text, kind = self._status_display(url)
        entry = self.session.library_store.get_library_entry(url)
        display_title = ""
        if entry:
            display_title = entry.translated_title or entry.title or url
        updated = False
        for i in range(self.grid.count()):
            it = self.grid.item(i)
            if it and it.data(Qt.UserRole) == url:
                title = display_title or ((it.text() or "").split("\n", 1)[0] or url)
                it.setText(f"{title[:40]}\n{text or '—'}")
                it.setToolTip(f"{title}\n{text}" if text else title)
                if st.get("cover_refreshed") and entry:
                    pix = self._cover_pixmap(entry)
                    if pix and not pix.isNull():
                        it.setIcon(QIcon(pix))
                updated = True
                break
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.data(Qt.UserRole) == url:
                if display_title:
                    it.setText(display_title[:80])
                status_item = QTableWidgetItem(text)
                if kind == "update":
                    status_item.setForeground(Qt.yellow)
                elif kind == "current":
                    status_item.setForeground(Qt.green)
                elif kind == "error":
                    status_item.setForeground(Qt.red)
                self.table.setItem(r, 2, status_item)
                updated = True
                break
        if not updated:
            self.refresh()
        else:
            self.update_all_btn.setEnabled(any(
                (self.check_status.get(e.source_url) or {}).get("state") == "update"
                for e in self.session.library_store.get_library()
            ))

    def selected_entry(self):
        url = self._selected_url
        if not url:
            return None
        return self.session.library_store.get_library_entry(url)

    def _on_grid_select(self):
        items = self.grid.selectedItems()
        self._selected_url = items[0].data(Qt.UserRole) if items else None

    def _on_table_select(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self._selected_url = None
            return
        item = self.table.item(rows[0].row(), 0)
        self._selected_url = item.data(Qt.UserRole) if item else None

    def _on_grid_activate(self, item):
        self._selected_url = item.data(Qt.UserRole)
        self._emit_update()

    def _on_table_activate(self, _idx):
        self._emit_update()

    def _emit_update(self):
        e = self.selected_entry()
        if e:
            self.update_selected.emit(e)

    def _emit_open(self):
        if self._selected_url:
            self.open_selected.emit(self._selected_url)

    def _emit_remove(self):
        if self._selected_url:
            self.remove_selected.emit(self._selected_url)

    def _emit_dl_epub(self):
        e = self.selected_entry()
        if e:
            self.download_epub_selected.emit(e)

    def set_check_busy(self, busy: bool):
        self.check_btn.setEnabled(not busy)
        self.check_btn.setText("Checking…" if busy else "Check updates")
