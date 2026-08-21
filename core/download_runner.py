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
from typing import Any, Callable, Dict, List, Optional, Tuple

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


@dataclass
class EpubBuildResult:
    """Outcome of phase-2 EPUB build (translation + write)."""
    output_path: str
    translation_warnings: List[Tuple[str, int]] = field(default_factory=list)
    polish_cancelled: bool = False
    heuristic_chapters: List[str] = field(default_factory=list)


def format_completion_notes(
    failed_chapters: Optional[List[str]] = None,
    translation_warnings: Optional[List[Tuple[str, int]]] = None,
    polish_cancelled: bool = False,
    heuristic_chapters: Optional[List[str]] = None,
) -> str:
    """Extra lines for the completion dialog. Empty if the run was clean."""
    parts: List[str] = []
    if polish_cancelled:
        parts.append(
            "Polish was stopped — EPUB saved with machine translation "
            "(already-polished sentences were kept)."
        )
    if failed_chapters:
        parts.append(f"{len(failed_chapters)} chapter(s) had placeholders.")
    if heuristic_chapters:
        parts.append(
            f"{len(heuristic_chapters)} chapter(s) used a generic content guess "
            "(the site's content selector missed). Check those chapters if the text looks wrong."
        )
        for title in heuristic_chapters[:6]:
            label = (title[:50] + "…") if len(title) > 50 else title
            parts.append(f"  • {label}")
        extra = len(heuristic_chapters) - 6
        if extra > 0:
            parts.append(f"  • … and {extra} more")
    if translation_warnings:
        parts.append(
            f"{len(translation_warnings)} chapter(s) still have significant Chinese."
        )
        for title, count in translation_warnings[:8]:
            label = (title[:50] + "…") if len(title) > 50 else title
            parts.append(f"  • {label}: {count} chars")
        extra = len(translation_warnings) - 8
        if extra > 0:
            parts.append(f"  • … and {extra} more")
    return "\n".join(parts)


def eta_from_network_samples(
    network_elapsed: float,
    network_done: int,
    network_remaining: int,
) -> str:
    """ETA text from uncached/network samples only. Empty until we have a sample."""
    if network_done < 1 or network_remaining <= 0 or network_elapsed <= 0:
        return ""
    avg = network_elapsed / network_done
    return f"  (ETA {format_eta(avg * network_remaining)})"


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

    ETA is based only on uncached (network) chapters so library updates that
    reuse hundreds of cached chapters do not report "ETA 0s".
    """
    total = len(chapters)
    delay = getattr(parser, "request_delay", 2.0)
    failed: List[Chapter] = []
    paused_for = 0.0

    cached_html: List[Optional[str]] = [
        (cache.get_chapter(ch.url) if use_cache else None) for ch in chapters
    ]
    uncached_total = sum(1 for hit in cached_html if hit is None)
    uncached_done = 0
    cached_done = 0
    network_elapsed = 0.0

    for idx, chapter in enumerate(chapters):
        paused_for += control.wait_while_paused(set_status)
        if control.cancel_requested:
            raise DownloadCancelled()

        set_progress((idx + 1) / total)

        remaining_uncached = uncached_total - uncached_done
        eta_text = eta_from_network_samples(
            network_elapsed, uncached_done, remaining_uncached
        )
        hit = cached_html[idx]
        if hit is not None:
            chapter.content = hit
            cached_done += 1
            extra = ""
            if uncached_total:
                extra = f" · {uncached_done}/{uncached_total} new"
            set_status(f"Cached {cached_done}/{total}{extra}{eta_text}")
            control.persist_job()
            continue

        set_status(
            f"Downloading [{uncached_done + 1}/{uncached_total}]: "
            f"{chapter.title[:40]}{eta_text}"
        )
        t0 = time.monotonic()
        paused_here = 0.0
        try:
            chapter.content = parser.get_chapter_content(chapter)
            if use_cache:
                cache.put_chapter(book_key, chapter.url, chapter.title, chapter.content)
        except Exception as e:
            print(f"  Chapter [{idx + 1}/{total}] failed: {chapter.title}: {e}")
            failed.append(chapter)

        control.persist_job()
        if idx < total - 1:
            paused_here += control.interruptible_delay(delay, set_status)
        paused_for += paused_here
        uncached_done += 1
        network_elapsed += max(0.0, (time.monotonic() - t0) - paused_here)

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
) -> EpubBuildResult:
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
            set_progress(current / max(total_steps, 1))
            set_status(status)

        builder.build_with_translation(info, chapters, output_path, progress_cb)
        return EpubBuildResult(
            output_path=output_path,
            translation_warnings=builder.get_translation_warnings(),
            polish_cancelled=bool(builder.polish_cancelled),
            heuristic_chapters=[
                ch.title for ch in chapters if getattr(ch, "used_heuristic", False)
            ],
        )

    builder = EPUBBuilder(cleaner=cleaner, image_cache=cache)

    def progress_cb(current, total_steps, status):
        if control.cancel_requested:
            raise DownloadCancelled()
        set_progress(current / max(total_steps, 1))
        set_status(status)

    builder.build(info, chapters, output_path, progress_cb)
    return EpubBuildResult(
        output_path=output_path,
        heuristic_chapters=[
            ch.title for ch in chapters if getattr(ch, "used_heuristic", False)
        ],
    )


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
) -> Tuple[List[str], EpubBuildResult]:
    """
    Full single-novel download + EPUB. Progress 0..1 overall.
    Returns (failed chapter titles, build result). Raises DownloadCancelled.
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

    build_result = build_epub(
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
    return failed, build_result
