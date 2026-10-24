# Author: joelsnl and Anthropic Claude
"""Stats tab: a "wrapped"-style summary of the Library and reading activity.

Recomputed each time the tab is opened (``refresh()``), from whatever is already
in ``library.json``, ``reading.json``, ``notes.json`` and the chapter/translation
cache - see ``core/reading_stats.py`` for what each number means and which ones
are estimates.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from core.reading_stats import compute_reading_stats

_COLUMNS = 3


def _word(n: int, singular: str, plural_form: str = "") -> str:
    return singular if n == 1 else (plural_form or f"{singular}s")


class StatsPage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        body = QWidget()
        scroll.setWidget(body)
        self._layout = QVBoxLayout(body)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.empty_label = QLabel(
            "Nothing to show yet. Add a book and start reading, and this page fills in."
        )
        self.empty_label.setObjectName("mutedLabel")
        self.empty_label.setWordWrap(True)
        self._layout.addWidget(self.empty_label)

        self.hero = QFrame()
        self.hero.setObjectName("statHero")
        hero_layout = QVBoxLayout(self.hero)
        self.hero_num = QLabel("0")
        self.hero_num.setObjectName("statHeroNum")
        self.hero_label = QLabel("chapters read, all time")
        self.hero_label.setObjectName("statLabel")
        self.hero_sub = QLabel("")
        self.hero_sub.setObjectName("statSub")
        hero_layout.addWidget(self.hero_num)
        hero_layout.addWidget(self.hero_label)
        hero_layout.addWidget(self.hero_sub)
        self._layout.addWidget(self.hero)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self._layout.addWidget(self.grid_host)

        self.foot_label = QLabel("")
        self.foot_label.setObjectName("hintLabel")
        self.foot_label.setWordWrap(True)
        self._layout.addWidget(self.foot_label)

        self.hero.setVisible(False)
        self.grid_host.setVisible(False)

    def _clear_grid(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _tile(self, num: str, label: str, sub: str = "") -> QFrame:
        frame = QFrame()
        frame.setObjectName("statTile")
        layout = QVBoxLayout(frame)
        num_label = QLabel(num)
        num_label.setObjectName("statNum")
        num_label.setWordWrap(True)
        layout.addWidget(num_label)
        text_label = QLabel(label)
        text_label.setObjectName("statLabel")
        text_label.setWordWrap(True)
        layout.addWidget(text_label)
        if sub:
            sub_label = QLabel(sub)
            sub_label.setObjectName("statSub")
            sub_label.setWordWrap(True)
            layout.addWidget(sub_label)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        return frame

    def refresh(self) -> None:
        stats = compute_reading_stats(self.session)
        books = stats["books"]
        chapters = stats["chapters"]
        text = stats["text"]
        sites = stats["sites"]

        has_anything = books["tracked"] > 0 or books["shelved"] > 0
        self.empty_label.setVisible(not has_anything)
        self.hero.setVisible(has_anything)
        self.grid_host.setVisible(has_anything)
        self.foot_label.setVisible(has_anything)
        if not has_anything:
            return

        hero_num = chapters["read"] if chapters["read"] > 0 else chapters["in_library"]
        self.hero_num.setText(f"{hero_num:,}")
        self.hero_label.setText(
            "chapters read, all time" if chapters["read"] > 0 else "chapters in your library"
        )
        streak = stats["reading_streak_days"]
        if streak >= 2:
            self.hero_sub.setText(f"Reading streak: {streak} days running")
        elif streak == 1:
            self.hero_sub.setText("You read today - keep it going.")
        else:
            self.hero_sub.setText("")

        self._clear_grid()
        tiles = [self._tile(
            f"{books['tracked']:,}", f"{_word(books['tracked'], 'book')} in your library",
            f"{books['finished']:,} finished · {books['reading']:,} in progress",
        ), self._tile(
            f"{chapters['cached']:,}", "chapters downloaded", "cached on this PC right now",
        )]
        if text["characters_cached"] > 0:
            tiles.append(self._tile(
                f"{text['characters_cached']:,}", "characters downloaded",
                "estimated from what is cached now",
            ))
        if text["translated_words"] > 0:
            tiles.append(self._tile(
                f"{text['translated_words']:,}", "words translated",
                "estimated from the translation cache",
            ))
        if books["authors"] > 0:
            tiles.append(self._tile(
                f"{books['authors']:,}", f"{_word(books['authors'], 'author')} in your library",
            ))
        if sites["count"] > 0:
            sub = f"Most from {sites['top']} ({sites['top_chapters']:,} chapters)" if sites["top"] else ""
            tiles.append(self._tile(
                f"{sites['count']:,}", f"{_word(sites['count'], 'site')} you've used", sub,
            ))
        longest = stats["longest_book"]
        if longest:
            tiles.append(self._tile(
                f"{longest['chapters']:,}", "chapters in your longest book", longest["title"],
            ))
        recent = stats["most_recent_book"]
        if recent:
            from datetime import datetime

            at = datetime.fromtimestamp(recent["at"]).strftime("%b %d, %Y") if recent["at"] else "—"
            tiles.append(self._tile(at, "most recently downloaded", recent["title"]))
        if stats["notes"] > 0:
            tiles.append(self._tile(f"{stats['notes']:,}", f"{_word(stats['notes'], 'note')} saved"))
        if stats["bookmarks"] > 0:
            tiles.append(self._tile(
                f"{stats['bookmarks']:,}", f"{_word(stats['bookmarks'], 'bookmark')} saved",
            ))
        if stats["glossary_terms"] > 0:
            name_word = "name" if stats["glossary_terms"] == 1 else "names"
            tiles.append(self._tile(
                f"{stats['glossary_terms']:,}", f"{name_word} & terms your translator has learned",
            ))
        if books["untouched"] > 0:
            tiles.append(self._tile(
                f"{books['untouched']:,}", f"{_word(books['untouched'], 'book')} waiting for you to start",
            ))
        if books["shelved"] > 0:
            tiles.append(self._tile(f"{books['shelved']:,}", f"{_word(books['shelved'], 'book')} shelved"))

        for i, tile in enumerate(tiles):
            self.grid.addWidget(tile, i // _COLUMNS, i % _COLUMNS)

        self.foot_label.setText(
            "Characters and translated words are estimates from what is cached right now, not a "
            "lifetime total - the cache can drop old chapters to stay under its size limit."
        )
