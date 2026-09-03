# Author: joelsnl and Anthropic Claude
"""
QMessageBox helpers.

Fusion + QSS draws mnemonic underlines on &Yes / &No, but those are Alt+Y /
Alt+N. On Yes/No dialogs we bind Y and N so they work without Alt. No other
letter shortcuts.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractButton, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QInputDialog, QLabel, QListWidget, QMessageBox, QPushButton, QVBoxLayout,
)

_YES_LABELS = frozenset({"yes"})
_NO_LABELS = frozenset({"no", "not now"})


def _letter_key(letter: str) -> Qt.Key | None:
    ch = (letter or "").strip().upper()[:1]
    if not ch.isalpha():
        return None
    return getattr(Qt.Key, f"Key_{ch}", None)


def _yes_no_letter(label: str) -> str | None:
    key = (label or "").replace("&", "").strip().casefold()
    if key in _YES_LABELS:
        return "Y"
    if key in _NO_LABELS:
        return "N"
    return None


def bind_letter_shortcuts(box: QMessageBox) -> QMessageBox:
    """Bind Y/N on Yes/No buttons only. Safe to call twice."""
    claimed: set[str] = set()
    bound_buttons: set[int] = set()

    def add(btn: QAbstractButton | None, letter: str) -> None:
        if btn is None:
            return
        key = _letter_key(letter)
        if key is None:
            return
        mark = letter.upper()[:1]
        if mark in claimed:
            return
        claimed.add(mark)
        bound_buttons.add(id(btn))
        shortcut = QShortcut(QKeySequence(key), box)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.activated.connect(btn.click)

    add(box.button(QMessageBox.StandardButton.Yes), "Y")
    add(box.button(QMessageBox.StandardButton.No), "N")
    for btn in box.buttons():
        if id(btn) in bound_buttons:
            continue
        letter = _yes_no_letter(btn.text() or "")
        if letter:
            add(btn, letter)
    return box


def exec_box(box: QMessageBox) -> int:
    """Bind Y/N if this is a Yes/No box, then run the modal loop."""
    bind_letter_shortcuts(box)
    return box.exec()


def _standard_box(
    parent,
    icon: QMessageBox.Icon,
    title: str,
    text: str,
    buttons: QMessageBox.StandardButton,
    default: QMessageBox.StandardButton | None = None,
) -> QMessageBox:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(buttons)
    if default is not None:
        box.setDefaultButton(default)
    bind_letter_shortcuts(box)
    return box


def ask_yes_no(
    parent,
    title: str,
    text: str,
    *,
    default_yes: bool = True,
) -> bool:
    """Yes/No question. Y and N both work (not only Alt+Y / Alt+N)."""
    default = (
        QMessageBox.StandardButton.Yes
        if default_yes
        else QMessageBox.StandardButton.No
    )
    box = _standard_box(
        parent,
        QMessageBox.Icon.Question,
        title,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        default,
    )
    return box.exec() == QMessageBox.StandardButton.Yes


def ask_yes_not_now_dont_ask(parent, title: str, text: str) -> str:
    """
    Yes / Not now / Don't ask. Returns ``yes``, ``later``, or ``never``.
    Y and N work without Alt (Don't ask has no letter shortcut).
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    yes = box.addButton("Yes", QMessageBox.ButtonRole.YesRole)
    not_now = box.addButton("Not now", QMessageBox.ButtonRole.NoRole)
    dont = box.addButton("Don't ask", QMessageBox.ButtonRole.ActionRole)
    box.setDefaultButton(yes)
    box.setEscapeButton(not_now)
    exec_box(box)
    clicked = box.clickedButton()
    if clicked is yes:
        return "yes"
    if clicked is dont:
        return "never"
    if clicked is not_now:
        return "later"
    return "later"


def ask_accept_glossary_proposals(parent, proposals: list) -> bool:
    """Accept all / Discard. Returns True if the user accepted."""
    rows = [p for p in (proposals or []) if isinstance(p, dict) or p]
    if not rows:
        return False
    lines = []
    for item in rows[:40]:
        if isinstance(item, dict):
            src = str(item.get("source") or "")
            tgt = str(item.get("target") or "")
            kind = str(item.get("kind") or "term")
            evidence = str(item.get("evidence") or "").strip()
            title = str(item.get("novel_title") or "").strip()
        else:
            src = getattr(item, "source", "")
            tgt = getattr(item, "target", "")
            kind = getattr(item, "kind", "term")
            evidence = (getattr(item, "evidence", "") or "").strip()
            title = (getattr(item, "novel_title", "") or "").strip()
        prefix = f"{title}: " if title else ""
        extra = f"\n    {evidence}" if evidence else ""
        lines.append(f"• {prefix}{src} → {tgt} ({kind}){extra}")
    more = "" if len(rows) <= 40 else f"\n…and {len(rows) - 40} more"
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("Accept glossary terms?")
    box.setText(
        "Accept these terms into the per-novel glossaries?\n\n"
        + "\n".join(lines)
        + more
    )
    accept = box.addButton("Accept all", QMessageBox.ButtonRole.YesRole)
    discard = box.addButton("Discard", QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(accept)
    box.setEscapeButton(discard)
    exec_box(box)
    return box.clickedButton() is accept


def show_info(parent, title: str, text: str) -> None:
    _standard_box(
        parent,
        QMessageBox.Icon.Information,
        title,
        text,
        QMessageBox.StandardButton.Ok,
        QMessageBox.StandardButton.Ok,
    ).exec()


def show_info_with_preview(
    parent,
    title: str,
    text: str,
    *,
    preview_label: str = "Preview",
) -> bool:
    """Ok + Preview. Returns True if Preview was clicked."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    preview = box.addButton(preview_label, QMessageBox.ButtonRole.ActionRole)
    box.setDefaultButton(QMessageBox.StandardButton.Ok)
    exec_box(box)
    return box.clickedButton() is preview


def pick_item(parent, title: str, text: str, items: list[str]) -> int | None:
    """Pick one label. Returns the index, or None if cancelled / empty."""
    labels = [str(item) for item in items if str(item).strip()]
    if not labels:
        return None
    if len(labels) == 1:
        return 0
    chosen, ok = QInputDialog.getItem(parent, title, text, labels, 0, False)
    if not ok:
        return None
    try:
        return labels.index(chosen)
    except ValueError:
        return 0


def show_warning(parent, title: str, text: str) -> None:
    _standard_box(
        parent,
        QMessageBox.Icon.Warning,
        title,
        text,
        QMessageBox.StandardButton.Ok,
        QMessageBox.StandardButton.Ok,
    ).exec()


def show_error(parent, title: str, text: str) -> None:
    _standard_box(
        parent,
        QMessageBox.Icon.Critical,
        title,
        text,
        QMessageBox.StandardButton.Ok,
        QMessageBox.StandardButton.Ok,
    ).exec()


def show_rich_info(parent, title: str, html: str) -> None:
    """Information box with clickable HTML links."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Information)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(html)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    for lbl in box.findChildren(QLabel):
        lbl.setOpenExternalLinks(True)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    exec_box(box)


def pick_recent_download(parent, history) -> str | None:
    """Recent-downloads picker. Returns a source URL, or None."""
    if not history:
        show_info(parent, "Recent", "No download history yet.")
        return None
    dlg = QDialog(parent)
    dlg.setWindowTitle("Recent downloads")
    dlg.resize(520, 400)
    lay = QVBoxLayout(dlg)
    lst = QListWidget()
    for h in history:
        title = h.translated_title or h.title or h.source_url
        lst.addItem(f"{title}\n{h.source_url}")
    lay.addWidget(lst)
    buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Cancel)
    lay.addWidget(buttons)
    buttons.rejected.connect(dlg.reject)
    chosen: list[str] = []

    def accept():
        row = lst.currentRow()
        if row < 0:
            return
        chosen.append(history[row].source_url)
        dlg.accept()

    buttons.accepted.connect(accept)
    lst.itemDoubleClicked.connect(lambda _: accept())
    dlg.exec()
    return chosen[0] if chosen else None


def show_cache_dialog(parent, cache, settings: dict, on_status) -> None:
    """Help → Cache… size cap and clear buttons."""
    from core.settings import set_setting

    dlg = QDialog(parent)
    dlg.setWindowTitle("Cache")
    dlg.setMinimumWidth(460)
    layout = QVBoxLayout(dlg)

    def size_text() -> str:
        n = cache.file_size_bytes()
        if n < 1024 * 1024:
            shown = f"{n / 1024:.0f} KB"
        elif n < 1024 * 1024 * 1024:
            shown = f"{n / (1024 * 1024):.1f} MB"
        else:
            shown = f"{n / (1024 * 1024 * 1024):.2f} GB"
        return f"Current size: {shown}"

    size_lbl = QLabel(size_text())
    layout.addWidget(size_lbl)
    explain = QLabel(
        "Chapter HTML, translations (including polished spans), covers, and "
        "tables of contents live in ~/.huaepub/cache.db. This is not Drive-synced. "
        "When the file grows past the limit, the oldest cached chapters are "
        "deleted first (least recently stored). Translations are kept unless "
        "the cache is still over the limit.\n\n"
        "Nothing is cleared on a timer — only when over the cap, or when you "
        "clear it here. llama.cpp models live separately in ~/.huaepub/polish/."
    )
    explain.setWordWrap(True)
    layout.addWidget(explain)

    cap_row = QHBoxLayout()
    cap_row.addWidget(QLabel("Maximum size:"))
    combo = QComboBox()
    choices = [
        (512, "512 MB"),
        (1024, "1 GB"),
        (2048, "2 GB"),
        (4096, "4 GB"),
        (0, "Unlimited"),
    ]
    for mb, label in choices:
        combo.addItem(label, mb)
    current = int(settings.get("cache_max_mb", 2048) or 0)
    idx = next((i for i, (mb, _) in enumerate(choices) if mb == current), 2)
    combo.setCurrentIndex(idx)

    def on_cap_changed(_index: int):
        mb = int(combo.currentData())
        settings["cache_max_mb"] = mb
        set_setting("cache_max_mb", mb)
        removed = cache.maybe_evict()
        size_lbl.setText(size_text())
        if removed:
            on_status(f"Cache trimmed ({removed} oldest entries removed)")

    combo.currentIndexChanged.connect(on_cap_changed)
    cap_row.addWidget(combo)
    cap_row.addStretch(1)
    layout.addLayout(cap_row)

    btn_row = QHBoxLayout()
    clear_ch = QPushButton("Clear chapter cache")
    clear_ch.setToolTip("Delete chapter HTML, covers, and TOCs. Keep translations.")
    clear_all = QPushButton("Clear all cache")
    clear_all.setToolTip("Delete chapters, covers, TOCs, and translations.")

    def on_clear_chapters():
        if not ask_yes_no(
            dlg, "Clear chapter cache",
            "Delete cached chapter HTML, covers, and tables of contents?\n\n"
            "Translations stay. The next download will re-fetch chapter text.",
        ):
            return
        cache.clear_chapter_data()
        size_lbl.setText(size_text())
        on_status("Chapter cache cleared")

    def on_clear_all():
        if not ask_yes_no(
            dlg, "Clear all cache",
            "Delete the entire cache, including translations?\n\n"
            "The next download and translate will redo all network work.",
        ):
            return
        cache.clear_all()
        size_lbl.setText(size_text())
        on_status("All cache cleared")

    clear_ch.clicked.connect(on_clear_chapters)
    clear_all.clicked.connect(on_clear_all)
    btn_row.addWidget(clear_ch)
    btn_row.addWidget(clear_all)
    btn_row.addStretch(1)
    layout.addLayout(btn_row)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)
    dlg.exec()
