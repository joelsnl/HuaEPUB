# Author: joelsnl and Anthropic Claude
"""Everything the routes share: the app session, the task slot, sign-in."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

from core.download_runner import downloads_folder
from core.settings import get_default_books_dir
from web.auth import LoginLimiter, OpenLinks, ServerSecrets
from web.preview import PreviewStore, build_preview, translate_preview
from web.tasks import TaskManager

MODES = ("lan", "remote")


def book_roots(session) -> List[Path]:
    """Folders the server may hand EPUBs out of (the books folders only)."""
    roots = [get_default_books_dir()]
    custom = (getattr(session, "output_dir", "") or session.settings.get("output_dir") or "").strip()
    if custom:
        roots.append(downloads_folder(custom))
    return roots


@dataclass
class ServerContext:
    session: Any
    tasks: TaskManager
    secrets: ServerSecrets
    mode: str = "lan"
    version: str = ""
    https: bool = False
    hsts: bool = False
    previews: PreviewStore = field(default_factory=lambda: PreviewStore(limit=64))
    limiter: LoginLimiter = field(default_factory=LoginLimiter)
    open_links: OpenLinks = field(default_factory=OpenLinks)
    preview_builder: Callable = build_preview
    preview_translator: Callable = translate_preview
    check_status: Dict[str, dict] = field(default_factory=dict)
    check_lock: threading.Lock = field(default_factory=threading.Lock)
    readers: Any = None

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if self.mode == "remote" and not self.https:
            raise ValueError("Remote mode needs HTTPS")
        if self.readers is None:
            from web.reader_api import ReaderStore

            self.readers = ReaderStore()

    @property
    def remote(self) -> bool:
        return self.mode == "remote"
