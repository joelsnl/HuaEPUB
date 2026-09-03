# Author: joelsnl and Anthropic Claude
"""
Local reading position (which chapter, optional scroll).

Stored as reading.json in ~/.huaepub/. Never Drive-synced — same rule as
cache.db and active_download.json.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.settings import get_data_dir

READING_FILE = "reading.json"

_lock = threading.Lock()


def _path(data_dir: Optional[Path] = None) -> Path:
    return Path(data_dir or get_data_dir()) / READING_FILE


def _load_unlocked(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_unlocked(path: Path, data: Dict[str, Any]) -> None:
    from core.atomic_io import atomic_write_json

    atomic_write_json(path, data, fsync=True, ensure_ascii=False)


def get_position(source_url: str, *, data_dir: Optional[Path] = None) -> Optional[dict]:
    url = (source_url or "").strip()
    if not url:
        return None
    try:
        with _lock:
            data = _load_unlocked(_path(data_dir))
        raw = data.get(url)
        if not isinstance(raw, dict):
            return None
        try:
            index = int(raw.get("chapter_index") or 0)
        except (TypeError, ValueError):
            index = 0
        try:
            scroll = float(raw.get("scroll") or 0.0)
        except (TypeError, ValueError):
            scroll = 0.0
        return {
            "chapter_url": str(raw.get("chapter_url") or ""),
            "chapter_index": max(0, index),
            "scroll": min(1.0, max(0.0, scroll)),
            "updated_at": float(raw.get("updated_at") or 0),
        }
    except Exception:
        return None


def set_position(
    source_url: str,
    *,
    chapter_url: str = "",
    chapter_index: int = 0,
    scroll: float = 0.0,
    data_dir: Optional[Path] = None,
) -> None:
    url = (source_url or "").strip()
    if not url:
        return
    try:
        index = int(chapter_index or 0)
    except (TypeError, ValueError):
        index = 0
    try:
        ratio = float(scroll or 0.0)
    except (TypeError, ValueError):
        ratio = 0.0
    entry = {
        "chapter_url": str(chapter_url or ""),
        "chapter_index": max(0, index),
        "scroll": min(1.0, max(0.0, ratio)),
        "updated_at": time.time(),
    }
    try:
        with _lock:
            path = _path(data_dir)
            data = _load_unlocked(path)
            data[url] = entry
            _write_unlocked(path, data)
    except Exception:
        pass


def clear_position(source_url: str, *, data_dir: Optional[Path] = None) -> None:
    url = (source_url or "").strip()
    if not url:
        return
    try:
        with _lock:
            path = _path(data_dir)
            data = _load_unlocked(path)
            if url not in data:
                return
            data.pop(url, None)
            _write_unlocked(path, data)
    except Exception:
        pass
