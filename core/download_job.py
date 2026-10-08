# Author: joelsnl and Anthropic Claude
"""
Local-only incomplete download job (active_download.json in ~/.huaepub/).

Chapter cache plus this file let a download resume after Pause, app close,
or PC shutdown. The desktop and server mode
build and decode the job dicts here, so both resume the same file.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.parser import Chapter, NovelInfo, get_parser_for_url
from core.settings import get_data_dir

JOB_FILE = "active_download.json"
JOB_VERSION = 1

_lock = threading.Lock()


def job_path(data_dir: Optional[Path] = None) -> Path:
    return (data_dir or get_data_dir()) / JOB_FILE


def load_job(data_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Return the incomplete job dict, or None."""
    path = job_path(data_dir)
    try:
        if not path.exists():
            return None
        with _lock:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        if not isinstance(data, dict) or data.get("version") != JOB_VERSION:
            return None
        if data.get("kind") not in (
            "single", "multi", "library_update", "library_update_all"
        ):
            return None
        return data
    except Exception:
        return None


def save_job(job: Dict[str, Any], data_dir: Optional[Path] = None) -> None:
    """Atomically persist the active job. Never raises."""
    if not isinstance(job, dict):
        return
    path = job_path(data_dir)
    try:
        job = dict(job)
        job["version"] = JOB_VERSION
        job["updated_at"] = time.time()
        data_dir = path.parent
        data_dir.mkdir(parents=True, exist_ok=True)
        from core.atomic_io import atomic_write_json
        with _lock:
            atomic_write_json(path, job, fsync=False, tmp_suffix=".tmp")
    except Exception as e:
        print(f"Warning: could not save download job: {e}")


def clear_job(data_dir: Optional[Path] = None) -> None:
    """Remove the active job file. Never raises."""
    path = job_path(data_dir)
    try:
        with _lock:
            if path.exists():
                path.unlink()
            tmp = path.with_suffix(".tmp")
            if tmp.exists():
                tmp.unlink()
    except Exception:
        pass


def chapters_to_job(chapters: List[Chapter]) -> List[Dict[str, Any]]:
    return [
        {"url": c.url, "title": c.title or "", "index": int(c.index or 0)}
        for c in chapters
        if c and c.url
    ]


def chapters_from_job(items: List[Dict[str, Any]]) -> List[Chapter]:
    out: List[Chapter] = []
    for i, item in enumerate(items or []):
        if not isinstance(item, dict) or not item.get("url"):
            continue
        out.append(Chapter(
            title=item.get("title") or f"Chapter {i + 1}",
            url=item["url"],
            index=int(item.get("index") or i),
        ))
    return out


def novel_info_to_job(info: Optional[NovelInfo]) -> Dict[str, Any]:
    if not info:
        return {}
    return {
        "title": info.title or "",
        "author": info.author or "Unknown",
        "description": info.description or "",
        "cover_url": info.cover_url or "",
        "language": info.language or "zh",
        "tags": list(info.tags or []),
        "source_url": info.source_url or "",
    }


def novel_info_from_job(data: Optional[Dict[str, Any]]) -> Optional[NovelInfo]:
    if not data or not isinstance(data, dict):
        return None
    title = (data.get("title") or "").strip()
    source = (data.get("source_url") or "").strip()
    if not title and not source:
        return None
    return NovelInfo(
        title=title or "Untitled",
        author=data.get("author") or "Unknown",
        description=data.get("description") or "",
        cover_url=data.get("cover_url") or None,
        language=data.get("language") or "zh",
        tags=list(data.get("tags") or []),
        source_url=source,
    )


def book_job(
    kind: str,
    info: NovelInfo,
    chapters: List[Chapter],
    output_path: str,
    options: Dict[str, Any],
    *,
    translated_title: str = "",
    source_url: str = "",
    title: str = "",
) -> Dict[str, Any]:
    """The resume payload for one book: ``single`` or ``library_update``."""
    return {
        "kind": kind,
        "status": "running",
        "source_url": source_url or info.source_url or "",
        "title": title or info.title or "",
        "translated_title": translated_title or "",
        "info": novel_info_to_job(info),
        "chapters": chapters_to_job(chapters),
        "output_path": output_path,
        "options": options,
    }


def multi_job(novels: List[Dict[str, Any]], options: Dict[str, Any]) -> Dict[str, Any]:
    """The resume payload for a Multi download (``novels`` as the Multi tab holds them)."""
    return {
        "kind": "multi",
        "status": "running",
        "options": options,
        "novels": [
            {
                "source_url": n["url"],
                "title": n["info"].title,
                "translated_title": n.get("translated_title") or "",
                "info": novel_info_to_job(n["info"]),
                "chapters": chapters_to_job(n["chapters"]),
                "done": False,
            }
            for n in novels
        ],
    }


def library_update_all_job(entries: list, options: Dict[str, Any]) -> Dict[str, Any]:
    """The resume payload for an Update / Update All batch."""
    return {
        "kind": "library_update_all",
        "status": "running",
        "options": options,
        "entries": [
            {
                "source_url": e.source_url,
                "title": e.title or "",
                "translated_title": e.translated_title or "",
                "done": False,
            }
            for e in entries
        ],
    }


def single_from_job(job: Dict[str, Any]) -> Optional[Tuple[str, Any, NovelInfo, List[Chapter]]]:
    """``(url, parser, info, chapters)`` for a saved Single job, or None if it cannot resume."""
    info = novel_info_from_job(job.get("info"))
    chapters = chapters_from_job(job.get("chapters") or [])
    url = (job.get("source_url") or (info.source_url if info else "")).strip()
    parser = get_parser_for_url(url)
    if not parser or not chapters:
        return None
    if not info:
        info = NovelInfo(title=job.get("title") or "Untitled", source_url=url)
    return url, parser, info, chapters


def novels_from_job(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The unfinished novels of a saved Multi job, ready to download again."""
    novels = []
    for item in job.get("novels") or []:
        if item.get("done"):
            continue
        url = (item.get("source_url") or "").strip()
        chapters = chapters_from_job(item.get("chapters") or [])
        info = novel_info_from_job(item.get("info"))
        parser = get_parser_for_url(url) if url else None
        if not parser or not chapters or not info:
            continue
        novels.append({
            "url": url, "parser": parser, "info": info,
            "chapters": chapters, "status": "fetched",
            "translated_title": item.get("translated_title") or "",
        })
    return novels


def entries_from_job(job: Dict[str, Any], library_store) -> list:
    """Library entries a saved Update All job has not finished yet."""
    entries = []
    for e in job.get("entries") or []:
        if e.get("done"):
            continue
        ent = library_store.get_library_entry(e.get("source_url") or "")
        if ent:
            entries.append(ent)
    return entries


def job_display_title(job: Dict[str, Any]) -> str:
    """Short label for banners / dialogs."""
    kind = job.get("kind")
    if kind == "multi":
        novels = job.get("novels") or []
        pending = [n for n in novels if not n.get("done")]
        total = len(novels)
        done = total - len(pending)
        if pending:
            name = pending[0].get("translated_title") or pending[0].get("title") or "novel"
            return f"Multi-download ({done}/{total} done) — {name}"
        return f"Multi-download ({total} novels)"
    if kind == "library_update_all":
        entries = job.get("entries") or []
        pending = [e for e in entries if not e.get("done")]
        total = len(entries)
        done = total - len(pending)
        if pending:
            name = pending[0].get("translated_title") or pending[0].get("title") or "novel"
            return f"Update All ({done}/{total} done) — {name}"
        return f"Update All ({total} novels)"
    title = job.get("translated_title") or job.get("title") or "Novel"
    if kind == "library_update":
        return f"Library update — {title}"
    return title


def job_chapter_urls(job: Dict[str, Any]) -> List[str]:
    """URLs used to estimate cache progress for the current unfinished work."""
    kind = job.get("kind")
    if kind == "multi":
        for novel in job.get("novels") or []:
            if not novel.get("done"):
                return [c.get("url") for c in (novel.get("chapters") or []) if c.get("url")]
        return []
    if kind == "library_update_all":
        # Progress is per-book; show first pending book's chapters if stored
        for entry in job.get("entries") or []:
            if not entry.get("done"):
                return [c.get("url") for c in (entry.get("chapters") or []) if c.get("url")]
        return []
    return [c.get("url") for c in (job.get("chapters") or []) if c.get("url")]
