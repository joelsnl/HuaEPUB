"""Small shared helpers used by the GUI and builders."""

from __future__ import annotations

import re
from typing import List
from urllib.parse import urlparse

# http(s) URLs, allowing common novel-site punctuation in paths
URL_RE = re.compile(r'https?://[^\s<>\'"\]]+', re.IGNORECASE)

# Windows-forbidden filename characters
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def format_eta(seconds: float) -> str:
    """Format a duration like '3m 20s' or '1h 12m'."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def safe_filename(title: str, max_length: int = 120) -> str:
    """
    Build a filesystem-safe EPUB basename from a (preferably English) title.
    Keeps the full title when possible instead of shortening to First...Last.
    """
    clean = (title or "").strip()
    clean = _UNSAFE_FILENAME_RE.sub("", clean)
    # Normalize whitespace; allow letters/digits/spaces and a few separators
    clean = "".join(c for c in clean if c.isalnum() or c in " ._-()'&,+")
    clean = " ".join(clean.split()).strip(" .")
    if not clean:
        return "novel"
    if len(clean) > max_length:
        clean = clean[:max_length].rstrip(" ._-")
    return clean or "novel"


def extract_urls(text: str) -> List[str]:
    """
    Pull unique http(s) URLs out of a free-text block (one per line or inline).
    Preserves first-seen order; strips trailing punctuation commonly left by paste.
    """
    if not text:
        return []
    seen = set()
    urls: List[str] = []
    for match in URL_RE.findall(text):
        url = match.rstrip(".,;:!?)>\"]'")
        if url in seen:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def looks_like_url(text: str) -> bool:
    """True if the clipboard/text is essentially a single http(s) URL."""
    text = (text or "").strip()
    if not text or "\n" in text or " " in text:
        # Multi-line / multi-word: still OK if extract_urls finds something
        urls = extract_urls(text)
        return len(urls) >= 1 and len(text) < 2000
    return bool(URL_RE.fullmatch(text.rstrip(".,;:!?)>\"]'")))
