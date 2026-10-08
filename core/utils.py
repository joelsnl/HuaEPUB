"""Small shared helpers used by the GUI and builders."""

from __future__ import annotations

import os
import re
import sys
from typing import List
from urllib.parse import urlparse

# http(s) URLs, allowing common novel-site punctuation in paths
URL_RE = re.compile(r'https?://[^\s<>\'"\]]+', re.IGNORECASE)

# Windows-forbidden filename characters
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Env vars PyInstaller / SSL stacks may leave pointing at a dead _MEI* folder
_STALE_ENV_KEYS = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "PYTHONHOME",
    "PYTHONPATH",
    "_MEIPASS2",
)


def sanitize_runtime_env() -> list:
    """
    Drop inherited env that breaks TLS after a post-update relaunch.

    The update helper is spawned by the old frozen process, so it (and any
    child it Start-Processes) can inherit SSL_CERT_FILE / CURL_CA_BUNDLE
    pointing at the old `_MEI*` extract dir. Once that dir is deleted,
    curl_cffi fails with error 77 and downloads fail until a
    clean manual restart.
    """
    cleared = []
    meipass = getattr(sys, "_MEIPASS", None)
    meipass_norm = os.path.normcase(os.path.abspath(meipass)) if meipass else ""

    for key in _STALE_ENV_KEYS:
        val = os.environ.get(key)
        if not val:
            continue
        drop = False
        if key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            if not os.path.isfile(val):
                drop = True
            elif "_MEI" in val.replace("\\", "/"):
                # Cert path from another PyInstaller extract — not ours
                if not meipass_norm or meipass_norm not in os.path.normcase(os.path.abspath(val)):
                    drop = True
        elif getattr(sys, "frozen", False):
            # Frozen apps should not keep parent PYTHON* / _MEIPASS2 leftovers
            drop = True
        if drop:
            os.environ.pop(key, None)
            cleared.append(key)
    return cleared


def in_pytest() -> bool:
    """True while a pytest test is running (not merely because pytest is imported)."""
    return "pytest" in sys.modules and bool(os.environ.get("PYTEST_CURRENT_TEST"))


def format_count(n: int) -> str:
    """Grouped integer: 51399 → 51,399."""
    return f"{int(n):,}"


def format_ratio(done: int, total: int) -> str:
    """Progress pair: 4/20, or 1,204/3,000."""
    return f"{format_count(done)}/{format_count(total)}"


def plural(n: int, singular: str, plural_form: str = "") -> str:
    """'1 novel' or '3 novels'."""
    n = int(n)
    word = singular if abs(n) == 1 else (plural_form or f"{singular}s")
    return f"{format_count(n)} {word}"


def format_bytes(n: int) -> str:
    """File size for cache and download labels."""
    n = max(0, int(n))
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    if n < 1024 ** 3:
        value = n / (1024 ** 2)
        return f"{value:.0f} MB" if value >= 10 else f"{value:.1f} MB"
    value = n / (1024 ** 3)
    return f"{value:.0f} GB" if value >= 10 else f"{value:.2f} GB"


def format_eta(seconds: float) -> str:
    """Duration for the progress line: 45s, 3m 20s, 1h 2m. Zero units are omitted."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        minutes, secs = divmod(seconds, 60)
        if secs == 0:
            return f"{minutes}m"
        return f"{minutes}m {secs}s"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"


def pipeline_phase(status: str) -> str:
    """The pipeline phase a status line names: polishing / writing / translating / fetching, or ''."""
    text = (status or "").lower()
    if "polish" in text:
        return "polishing"
    if "writing epub" in text or text.startswith("writing"):
        return "writing"
    if "translat" in text:
        return "translating"
    if "fetch" in text or "chapter" in text or "download" in text:
        return "fetching"
    return ""


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
