# Author: joelsnl and Anthropic Claude
"""
UI-agnostic download orchestration: pause/cancel, chapter cache, EPUB build.

Qt (or any other UI) wraps this with threads/signals. No Qt imports here.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.cache import NovelCache
from core.cleaner import ContentCleaner
from core.download_job import save_job
from core.epub_builder import EPUBBuilder, TranslatedEPUBBuilder
from core.library import LibraryStore
from core.parser import Chapter, NovelInfo
from core.settings import get_default_books_dir
from core.translator import GoogleTranslator
from core.utils import format_eta, safe_filename
from core.security import safe_epub_basename


class DownloadCancelled(Exception):
    """Raised when the user cancels a download."""


StatusFn = Callable[[str], None]
ProgressFn = Callable[[float], None]
PersistFn = Callable[[], None]


@dataclass
class DownloadControl:
    """Shared pause/cancel flags (mutated from the UI thread)."""
    cancel_requested: bool = False
    is_paused: bool = False
    is_downloading: bool = False
    active_job: Optional[dict] = field(default=None)
    job_save_counter: int = 0
    data_dir: Optional[Path] = None

    def request_cancel(self):
        self.cancel_requested = True
        self.is_paused = False

    def toggle_pause(self) -> bool:
        """Flip pause; return new paused state."""
        self.is_paused = not self.is_paused
        return self.is_paused

    def persist_job(self, force: bool = False):
        if not self.active_job or not self.data_dir:
            return
        self.job_save_counter += 1
        if not force and self.job_save_counter % 10 != 0:
            return
        self.active_job["status"] = "paused" if self.is_paused else "running"
        save_job(self.active_job, self.data_dir)

    def wait_while_paused(self, set_status: Optional[StatusFn] = None) -> float:
        if not self.is_paused:
            return 0.0
        if set_status:
            set_status("Paused — click Resume to continue")
        t0 = time.monotonic()
        while self.is_paused:
            if self.cancel_requested:
                raise DownloadCancelled()
            time.sleep(0.2)
        return time.monotonic() - t0

    def interruptible_delay(self, seconds: float, set_status: Optional[StatusFn] = None) -> float:
        paused_total = 0.0
        end = time.monotonic() + max(0.0, seconds)
        while True:
            paused_total += self.wait_while_paused(set_status)
            if self.cancel_requested:
                raise DownloadCancelled()
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.2, remaining))
        return paused_total


def downloads_folder(output_dir: str = "") -> Path:
    custom = (output_dir or "").strip()
    if custom:
        path = Path(custom)
        if path.exists():
            return path
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except Exception:
            print(f"Warning: chosen output folder unavailable: {custom}")
    return get_default_books_dir()


def epub_path(
    folder: Path,
    title: str,
    *,
    preferred_name: str = "",
    preferred_path: str = "",
) -> str:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_epub_basename(preferred_name or preferred_path or "")
    if name:
        stem = Path(name).stem
        stem = re.sub(r" \(\d+\)$", "", stem)
        name = f"{stem}.epub"
    else:
        name = f"{safe_filename(title)}.epub"
    dest = (folder / name).resolve()
    try:
        dest.relative_to(folder.resolve())
    except ValueError:
        name = f"{safe_filename(title)}.epub"
        dest = (folder / name).resolve()
    return str(dest)


def record_successful_download(
    library_store: LibraryStore,
    info: NovelInfo,
    chapters: List[Chapter],
    translated_title: Optional[str],
    output_path: str,
):
    if not info:
        return
    display_title = translated_title or info.title
    last_ch = chapters[-1] if chapters else None
    try:
        epub_name = Path(output_path).name if output_path else ""
        library_store.add_history(
            source_url=info.source_url,
            title=info.title,
            translated_title=display_title,
            author=info.author,
            chapter_count=len(chapters),
            output_path=output_path,
        )
        library_store.upsert_library(
            source_url=info.source_url,
            title=info.title,
            translated_title=display_title,
            author=info.author,
            cover_url=info.cover_url or "",
            chapter_count=len(chapters),
            last_chapter_url=last_ch.url if last_ch else "",
            last_chapter_title=last_ch.title if last_ch else "",
            output_path=output_path,
            epub_filename=epub_name,
        )
    except Exception as e:
        print(f"Warning: failed to update library/history: {e}")


def download_chapters_with_cache(
    *,
    control: DownloadControl,
    cache: NovelCache,
    parser: Any,
    chapters: List[Chapter],
    book_key: str,
    use_cache: bool,
    set_status: StatusFn,
    set_progress: ProgressFn,
) -> List[str]:
    """
    Sequential chapter download with cache + retry.
    Returns titles still failed after retry. Raises DownloadCancelled.
    """
    total = len(chapters)
    delay = getattr(parser, "request_delay", 2.0)
    failed: List[Chapter] = []
    start_time = time.monotonic()
    paused_for = 0.0

    for idx, chapter in enumerate(chapters):
        paused_for += control.wait_while_paused(set_status)
        if control.cancel_requested:
            raise DownloadCancelled()

        set_progress((idx + 1) / total)

        eta_text = ""
        if idx >= 3:
            elapsed = max(0.001, (time.monotonic() - start_time) - paused_for)
            avg = elapsed / idx
            eta_text = f"  (ETA {format_eta(avg * (total - idx))})"

        cached = cache.get_chapter(chapter.url) if use_cache else None
        if cached:
            chapter.content = cached
            set_status(f"Chapter [{idx + 1}/{total}] from cache{eta_text}")
            control.persist_job()
            continue

        set_status(f"Downloading [{idx + 1}/{total}]: {chapter.title[:40]}{eta_text}")
        try:
            chapter.content = parser.get_chapter_content(chapter)
            if use_cache:
                cache.put_chapter(book_key, chapter.url, chapter.title, chapter.content)
        except Exception as e:
            print(f"  Chapter [{idx + 1}/{total}] failed: {chapter.title}: {e}")
            failed.append(chapter)

        control.persist_job()
        if idx < total - 1:
            paused_for += control.interruptible_delay(delay, set_status)

    still_failed: List[str] = []
    if failed:
        set_status(f"Retrying {len(failed)} failed chapter(s)...")
        print(f"Retrying {len(failed)} failed chapter(s)...")
        for chapter in failed:
            paused_for += control.wait_while_paused(set_status)
            if control.cancel_requested:
                raise DownloadCancelled()
            paused_for += control.interruptible_delay(delay, set_status)
            try:
                chapter.content = parser.get_chapter_content(chapter)
                if use_cache:
                    cache.put_chapter(book_key, chapter.url, chapter.title, chapter.content)
                print(f"  Retry succeeded: {chapter.title}")
            except Exception as e:
                print(f"  Retry failed: {chapter.title}: {e}")
                chapter.content = "<p>[Chapter could not be downloaded from the source site.]</p>"
                still_failed.append(chapter.title)

    return still_failed


def translator_backend_kwargs(
    settings: Dict[str, Any],
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Backend args shared by make_translator / build_epub / FetchWorker."""
    o = options or {}
    return {
        "backend": o.get("backend") or settings.get("translation_backend", "google"),
        "libretranslate_url": (
            o.get("libretranslate_url")
            or settings.get("libretranslate_url", "https://libretranslate.com")
        ),
        "ollama_url": (
            o.get("ollama_url")
            or settings.get("ollama_url", "http://127.0.0.1:11434")
        ),
        "ollama_model": (
            o.get("ollama_model")
            or settings.get("ollama_model", "qwen2.5:3b")
        ),
    }


def epub_translate_kwargs(
    settings: Dict[str, Any],
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Like translator_backend_kwargs, plus the optional Ollama polish pass."""
    o = options or {}
    kwargs = translator_backend_kwargs(settings, options)
    kwargs["ollama_polish"] = bool(
        o.get("ollama_polish", settings.get("ollama_polish", False))
    )
    return kwargs


def make_translator(
    *,
    cache: NovelCache,
    max_workers: int,
    backend: str = "google",
    libretranslate_url: str = "https://libretranslate.com",
    ollama_url: str = "http://127.0.0.1:11434",
    ollama_model: str = "qwen2.5:3b",
) -> GoogleTranslator:
    return GoogleTranslator(
        max_workers=max_workers,
        backend=backend,
        libretranslate_url=libretranslate_url,
        ollama_url=ollama_url,
        ollama_model=ollama_model,
        persistent_cache=cache,
    )


def build_epub(
    *,
    control: DownloadControl,
    cache: NovelCache,
    info: NovelInfo,
    chapters: List[Chapter],
    output_path: str,
    clean: bool,
    translate: bool,
    workers: int,
    backend: str,
    libretranslate_url: str,
    set_status: StatusFn,
    set_progress: ProgressFn,
    ollama_url: str = "http://127.0.0.1:11434",
    ollama_model: str = "qwen2.5:3b",
    ollama_polish: bool = False,
):
    """Phase 2: build EPUB (optionally with translation). Progress 0..1 within this phase."""
    polish = bool(ollama_polish) and backend != "ollama"
    if translate and polish:
        set_status("Translating, then polishing English…")
    else:
        set_status("Building EPUB...")
    cleaner = ContentCleaner() if clean else None
    translator = None
    if translate:
        translator = make_translator(
            cache=cache,
            max_workers=workers,
            backend=backend,
            libretranslate_url=libretranslate_url,
            ollama_url=ollama_url,
            ollama_model=ollama_model,
        )

    if translator:
        builder = TranslatedEPUBBuilder(
            cleaner=cleaner,
            translator=translator,
            image_cache=cache,
            polish=polish,
        )

        def progress_cb(current, total_steps, status):
            if control.cancel_requested:
                translator.cancel()
                return
            set_progress(current / max(total_steps, 1))
            set_status(status)

        builder.build_with_translation(info, chapters, output_path, progress_cb)
    else:
        builder = EPUBBuilder(cleaner=cleaner, image_cache=cache)

        def progress_cb(current, total_steps, status):
            set_progress(current / max(total_steps, 1))
            set_status(status)

        builder.build(info, chapters, output_path, progress_cb)


def run_single_download(
    *,
    control: DownloadControl,
    cache: NovelCache,
    library_store: LibraryStore,
    parser: Any,
    info: NovelInfo,
    chapters: List[Chapter],
    output_path: str,
    translated_title: Optional[str],
    use_cache: bool,
    clean: bool,
    translate: bool,
    workers: int,
    backend: str,
    libretranslate_url: str,
    set_status: StatusFn,
    set_progress: ProgressFn,
    ollama_url: str = "http://127.0.0.1:11434",
    ollama_model: str = "qwen2.5:3b",
    ollama_polish: bool = False,
) -> List[str]:
    """
    Full single-novel download + EPUB. Progress 0..1 overall.
    Returns failed chapter titles. Raises DownloadCancelled.
    """
    book_key = info.source_url if info else ""

    def set_prog_dl(f: float):
        set_progress(f / 2)

    failed = download_chapters_with_cache(
        control=control,
        cache=cache,
        parser=parser,
        chapters=chapters,
        book_key=book_key,
        use_cache=use_cache,
        set_status=set_status,
        set_progress=set_prog_dl,
    )

    def set_prog_build(f: float):
        set_progress(0.5 + f * 0.5)

    build_epub(
        control=control,
        cache=cache,
        info=info,
        chapters=chapters,
        output_path=output_path,
        clean=clean,
        translate=translate,
        workers=workers,
        backend=backend,
        libretranslate_url=libretranslate_url,
        ollama_url=ollama_url,
        ollama_model=ollama_model,
        ollama_polish=ollama_polish,
        set_status=set_status,
        set_progress=set_prog_build,
    )

    record_successful_download(
        library_store, info, chapters, translated_title, output_path
    )
    return failed
