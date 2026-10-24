# Author: joelsnl and Anthropic Claude
"""Reading stats: a "Wrapped"-style summary of what is in the Library and what has been read.

Qt-free, shared by the desktop Stats page and the browser's Stats page
(``web/stats_api.py``). Built entirely from local, already-stored data —
``library.json`` (tracked novels), ``reading.json`` (chapter position per book),
``notes.json`` (saved passages) and the chapter/translation cache — so every number
here is something the app already knows, never a guess.

Two counts in here are honest estimates, not exact counts, and are named to say so:
- ``text.characters_cached`` strips HTML tags using a small sample's ratio rather than
  loading every cached chapter into memory (the cache module warns that a full-table
  scan per chapter is too slow on a Raspberry Pi; this runs once per stats view).
- ``text.translated_words`` is estimated the same way from the translation cache.
Both totals only cover what is still in the cache now (eviction can drop old rows),
not a lifetime total.
"""

from __future__ import annotations

import datetime
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from core.notes import count_all_notes
from core.reading import count_all_bookmarks, reading_marks
from core.translation.glossary import count_learned_terms


def _site_of(source_url: str) -> str:
    try:
        host = urlparse(source_url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def compute_reading_stats(session) -> Dict[str, Any]:
    """Build the stats payload from one AppSession (desktop or server mode)."""
    library = session.library_store.get_library()
    history = session.library_store.get_history()
    marks = reading_marks(data_dir=session.data_dir)

    active = [e for e in library if not e.shelved_at]
    shelved = [e for e in library if e.shelved_at]

    reading_count = 0
    finished_count = 0
    untouched_count = 0
    chapters_read = 0
    chapters_in_library = 0
    authors: set = set()
    site_chapters: Dict[str, int] = {}
    longest: Optional[Any] = None
    most_recent: Optional[Any] = None
    reading_days: set = set()

    for e in library:
        count = int(e.chapter_count or 0)
        chapters_in_library += count
        if e.author and e.author.strip():
            authors.add(e.author.strip())
        site = _site_of(e.source_url)
        if site:
            site_chapters[site] = site_chapters.get(site, 0) + count
        if longest is None or count > int(longest.chapter_count or 0):
            longest = e
        if most_recent is None or (e.last_downloaded_at or 0) > (most_recent.last_downloaded_at or 0):
            most_recent = e

        if e.shelved_at:
            continue
        mark = marks.get(e.source_url)
        if not mark:
            untouched_count += 1
            continue
        index, updated_at = mark
        # Capped at the book's known chapter count, never a raw stored index: a book whose
        # count briefly reads 0 (e.g. mid Library check) must not contribute an unbounded number.
        chapters_read += min(index + 1, count) if count else 0
        if updated_at:
            reading_days.add(datetime.date.fromtimestamp(updated_at))
        if count and index + 1 >= count:
            finished_count += 1
        else:
            reading_count += 1

    top_site = max(site_chapters, key=site_chapters.get) if site_chapters else ""

    streak = 0
    day = datetime.date.today()
    while day in reading_days:
        streak += 1
        day -= datetime.timedelta(days=1)

    chapter_totals = session.cache.chapter_text_totals()
    translation_totals = session.cache.translation_text_totals()
    oldest_history_at = min(
        (h.downloaded_at for h in history if h.downloaded_at), default=0.0
    )

    return {
        "generated_at": time.time(),
        "books": {
            "tracked": len(active),
            "shelved": len(shelved),
            "reading": reading_count,
            "finished": finished_count,
            "untouched": untouched_count,
            "authors": len(authors),
        },
        "chapters": {
            "in_library": chapters_in_library,
            "read": chapters_read,
            "cached": chapter_totals["chapters"],
        },
        "text": {
            "characters_cached": chapter_totals["characters"],
            "translated_segments": translation_totals["segments"],
            "translated_words": translation_totals["words"],
            "translated_characters": translation_totals["characters"],
        },
        "sites": {
            "count": len(site_chapters),
            "top": top_site,
            "top_chapters": site_chapters.get(top_site, 0),
        },
        "longest_book": (
            {
                "title": longest.translated_title or longest.title,
                "chapters": int(longest.chapter_count or 0),
            }
            if longest
            else None
        ),
        "most_recent_book": (
            {
                "title": most_recent.translated_title or most_recent.title,
                "at": float(most_recent.last_downloaded_at or 0),
            }
            if most_recent
            else None
        ),
        "reading_streak_days": streak,
        "notes": count_all_notes(data_dir=session.data_dir),
        "bookmarks": count_all_bookmarks(data_dir=session.data_dir),
        "glossary_terms": count_learned_terms(data_dir=session.data_dir),
        "history_since": oldest_history_at,
    }
