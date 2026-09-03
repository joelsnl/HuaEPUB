# Author: joelsnl and Anthropic Claude
"""
UI-agnostic download orchestration: pause/cancel, chapter cache, EPUB build.

Qt (or any other UI) wraps this with threads/signals. No Qt imports here.

Cancel during chapter fetch or Chinese→English translation raises
DownloadCancelled and writes no EPUB. Cancel during polish still writes
the EPUB (EpubBuildResult.polish_cancelled). ETA uses uncached/network
samples only. Completion notes cover leftover Chinese, heuristic chapters,
and a cancelled polish pass.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.cache import NovelCache
from core.cleaner import ContentCleaner, count_chinese_chars, is_chinese
from core.download_job import save_job
from core.epub_builder import EPUBBuilder, TranslatedEPUBBuilder
from core.library import LibraryStore
from core.parser import Chapter, NovelInfo
from core.settings import get_default_books_dir
from core.translation.glossary import normalize_glossary_mode
from core.translation.novel_translator import NovelTranslator
from core.gtx_throttle import GtxThrottle
from core.translator import THROTTLED_BACKENDS
from core.utils import format_eta, safe_filename
from core.security import safe_epub_basename


def _learn_site_junk(cleaner, chapters, set_status=None) -> None:
    """Repeating ads from the first chapters. Independent of Polish / llama.cpp."""
    if cleaner is None:
        return
    try:
        from core.ad_detect import learn_site_junk

        learn_site_junk(cleaner, chapters, set_status=set_status)
    except Exception as exc:
        print(f"  Site-ad learning skipped: {exc}")


class DownloadCancelled(Exception):
    """User cancelled during fetch or translation — do not write an EPUB."""


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


_WARNING_MARKERS = (
    "placeholder",
    "failed",
    "polish was stopped",
    "significant chinese",
    "generic content guess",
)


def completion_has_warnings(body: str) -> bool:
    """True when a completion dialog body is not a clean success."""
    text = body or ""
    low = text.lower()
    if any(marker in low for marker in _WARNING_MARKERS):
        return True
    for pat in (r"Completed:\s*(\d+)/(\d+)", r"Update All:\s*(\d+)/(\d+)"):
        match = re.search(pat, text)
        if match and int(match.group(1)) != int(match.group(2)):
            return True
    return False


def completion_dialog_title(body: str, ok_title: str) -> str:
    """Never use a success title when warnings or failures are in the body."""
    if completion_has_warnings(body):
        return "Saved with warnings"
    return ok_title


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


def eta_from_pack_samples(
    elapsed: float,
    packs_done: int,
    packs_remaining: int,
    *,
    min_samples: int = 2,
) -> str:
    """ETA from completed packed gtx requests, not raw paragraphs."""
    if packs_done < min_samples or packs_remaining <= 0 or elapsed <= 0:
        return ""
    return f"  (ETA {format_eta(packs_remaining * (elapsed / packs_done))})"


def translator_progress_label(backend: str) -> str:
    """Short name for the status bar (every translation engine)."""
    key = (backend or "").strip().lower()
    return {
        "google": "Google",
        "google_html": "Google HTML",
        "google_gtx": "Google Old",
        "microsoft": "Microsoft",
        "libretranslate": "LibreTranslate",
        "ollama": "Ollama",
        "ctranslate2": "Offline NMT",
    }.get(key, "Translate")


def _engine_eta(
    elapsed: float,
    completed: int,
    remaining: int,
    *,
    min_samples: int,
) -> str:
    if completed < min_samples or remaining <= 0 or elapsed <= 0:
        return ""
    return f"  (ETA {format_eta(remaining * (elapsed / completed))})"


def _chapter_note_for_slot(
    all_texts: List[Tuple[str, int, str]],
    chapters: List[Chapter],
    completed: int,
    progress_source_index: int = -1,
) -> str:
    if not all_texts:
        return ""
    slot = int(progress_source_index)
    if slot < 0:
        if completed <= 0:
            return ""
        slot = completed - 1
    slot = min(max(slot, 0), len(all_texts) - 1)
    kind, idx, _src = all_texts[slot]
    if kind == "title":
        return " · novel title"
    if kind == "author":
        return " · author"
    if kind == "description":
        return " · description"
    if kind in ("content", "chapter_title") and 0 <= idx < len(chapters):
        title = (chapters[idx].title or "").strip()
        if len(title) > 28:
            title = title[:28] + "…"
        return f" · ch {idx + 1}/{len(chapters)} {title}"
    return ""


def _planned_in_flight(translator) -> int:
    """In-flight for the footer: live gate, else start cap, else _in_flight."""
    try:
        gate = getattr(translator, "_gtx", None)
        if gate is not None:
            cur = int(getattr(gate, "current", 0) or 0)
            if cur > 0:
                return cur
            planned = int(getattr(translator, "_in_flight", 0) or 0)
            if planned > 0:
                return planned
            return int(getattr(gate, "limit", 0) or GtxThrottle.START_LIMIT)
        return int(getattr(translator, "_in_flight", 0) or 0)
    except Exception:
        return 0


def _zero_n_in_flight(translator) -> int:
    """8 in flight for unofficial Google/Microsoft before the first GET returns."""
    backend = (getattr(translator, "backend", "") or "").strip().lower()
    if backend in THROTTLED_BACKENDS:
        planned = _planned_in_flight(translator)
        return planned if planned > 0 else GtxThrottle.START_LIMIT
    return _planned_in_flight(translator)


def _translation_status_line(
    engine: str,
    completed: int,
    total: int,
    *,
    retry_pass: int = 0,
    cache_hits: int = 0,
    pack_done: int = 0,
    pack_total: int = 0,
    in_flight: int = 0,
    unique_requests: int = 0,
    chapter_note: str = "",
    eta: str = "",
    network_requests: int = 0,
) -> str:
    cache_note = ""
    if cache_hits and completed:
        cache_note = f" · {min(cache_hits, completed)} cached"
    pack_note = f" · {pack_done}/{pack_total} packs" if pack_total else ""
    unique_note = ""
    if unique_requests > 0 and network_requests <= 0:
        unique_note = f" · {unique_requests} unique requests"
    flight_note = f" · {in_flight} in flight" if in_flight else ""
    if retry_pass > 0:
        prefix = f"{engine} · Retry pass {retry_pass}: {completed}/{total}"
    else:
        prefix = f"{engine} · Translating: {completed}/{total}"
    return (
        f"{prefix}{cache_note}{pack_note}{unique_note}{flight_note}"
        f"{chapter_note}{eta}"
    )


StatusFn = Callable[[str], None]
ProgressFn = Callable[..., None]
PersistFn = Callable[[], None]


def _forward_progress(
    set_progress: ProgressFn,
    set_status: StatusFn,
    fraction: float,
    status: str = "",
) -> None:
    """Update the bar and always push live copy through set_status.

    Wrappers may accept ``(fraction, status)`` or only ``(fraction)``. Live
    footer text must not depend on TypeError from the two-arg call — that
    skip left the UI on static lines like "Translating chapters…".
    """
    try:
        set_progress(fraction, status)
    except TypeError:
        set_progress(fraction)
    if status:
        set_status(status)


@dataclass
class DownloadControl:
    """Shared pause/cancel flags (mutated from the UI thread)."""
    cancel_requested: bool = False
    is_paused: bool = False
    is_downloading: bool = False
    active_job: Optional[dict] = field(default=None)
    job_save_counter: int = 0
    data_dir: Optional[Path] = None
    translator: Any = None

    def request_cancel(self):
        self.cancel_requested = True
        self.is_paused = False
        t = self.translator
        cancel = getattr(t, "cancel", None) if t is not None else None
        if callable(cancel):
            cancel()

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


def _bind_translator(control: DownloadControl, translator) -> None:
    """So Cancel/Pause from the UI thread reach in-flight Google workers."""
    if control is None:
        return
    bind = getattr(translator, "bind_control", None) if translator is not None else None
    if callable(bind):
        bind(control)
    else:
        control.translator = translator


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
            description=getattr(info, "description", "") or "",
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
    translator=None,
    cleaner=None,
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
    _bind_translator(control, translator)

    def _cancel_download():
        if translator is not None and hasattr(translator, "cancel"):
            translator.cancel()
        raise DownloadCancelled()

    # Cheap COUNT for ETA — do not load every chapter's HTML before the
    # first GET. Per-URL content is read in the loop (old interleaved path).
    uncached_total = total
    if use_cache and total:
        if control.cancel_requested:
            _cancel_download()
        counter = getattr(cache, "count_cached_urls", None)
        if callable(counter):
            try:
                cached_n = int(counter([ch.url for ch in chapters]) or 0)
            except Exception:
                cached_n = 0
            uncached_total = max(0, total - min(max(cached_n, 0), total))
        if control.cancel_requested:
            _cancel_download()
    elif total:
        set_status(f"Starting download ({total} chapters)…")
    uncached_done = 0
    cached_done = 0
    network_elapsed = 0.0
    last_cache_ui = 0.0

    for idx, chapter in enumerate(chapters):
        paused_for += control.wait_while_paused(set_status)
        if control.cancel_requested:
            _cancel_download()

        remaining_uncached = uncached_total - uncached_done
        eta_text = eta_from_network_samples(
            network_elapsed, uncached_done, remaining_uncached
        )
        frac = (idx + 1) / total
        hit = None
        if use_cache:
            try:
                hit = cache.get_chapter(chapter.url)
            except Exception:
                hit = None
            if control.cancel_requested:
                _cancel_download()
        if hit is not None:
            chapter.content = hit
            cached_done += 1
            extra = ""
            if uncached_total:
                extra = f" · {uncached_done}/{uncached_total} new"
            now = time.monotonic()
            if now - last_cache_ui >= 0.07 or cached_done == total:
                last_cache_ui = now
                _forward_progress(
                    set_progress,
                    set_status,
                    frac,
                    f"Cached {cached_done}/{total}{extra}{eta_text}",
                )
                time.sleep(0)
            _learn_site_junk(cleaner, chapters, set_status)
            _prefetch_chapter(translator, cleaner, chapter, control)
            control.persist_job()
            continue

        if uncached_total <= uncached_done:
            uncached_total = uncached_done + 1
        _forward_progress(
            set_progress,
            set_status,
            frac,
            f"Fetching chapters [{uncached_done + 1}/{uncached_total}]: "
            f"{chapter.title[:40]}{eta_text}",
        )
        t0 = time.monotonic()
        paused_here = 0.0
        try:
            chapter.content = parser.get_chapter_content(chapter)
            if use_cache:
                cache.put_chapter(book_key, chapter.url, chapter.title, chapter.content)
            _learn_site_junk(cleaner, chapters, set_status)
            _prefetch_chapter(translator, cleaner, chapter, control)
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
        set_status(f"Retrying failed chapters ({len(failed)})…")
        print(f"Retrying {len(failed)} failed chapter(s)...")
        for chapter in failed:
            paused_for += control.wait_while_paused(set_status)
            if control.cancel_requested:
                _cancel_download()
            paused_for += control.interruptible_delay(delay, set_status)
            try:
                chapter.content = parser.get_chapter_content(chapter)
                if use_cache:
                    cache.put_chapter(book_key, chapter.url, chapter.title, chapter.content)
                _learn_site_junk(cleaner, chapters, set_status)
                _prefetch_chapter(translator, cleaner, chapter, control)
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
        "glossary_mode": normalize_glossary_mode(
            o.get("glossary")
            or o.get("glossary_mode")
            or o.get("translation_glossary")
            or settings.get("translation_glossary")
            or "auto"
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


def translate_then_build(
    builder: TranslatedEPUBBuilder,
    novel_info: NovelInfo,
    chapters: List[Chapter],
    output_path: str,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> str:
    """
    Clean, translate (and optionally polish), apply at text nodes, write EPUB.

    Lives here so cancel/ETA/warnings stay on the runner. The builder only
    extracts/applies nodes and writes the file.
    """
    translator = builder.translator
    if not translator:
        return builder.build(novel_info, chapters, output_path, progress_callback)

    load_gloss = getattr(translator, "configure_glossary", None)
    if callable(load_gloss):
        try:
            load_gloss(novel_info, chapters)
        except Exception as exc:
            print(f"  Glossary skipped: {exc}")
    wait_prefetch = getattr(translator, "wait_prefetch", None)
    if callable(wait_prefetch):
        wait_prefetch()

    builder.chapters_with_chinese = []
    builder.polish_cancelled = False
    total_steps = max(len(chapters) * 2, 1)
    current_step = 0
    last_clean_ui = 0.0

    if progress_callback:
        progress_callback(0, total_steps, "Preparing for translation...")

    _learn_site_junk(
        builder.cleaner,
        chapters,
        set_status=(
            (lambda s: progress_callback(0, total_steps, s)) if progress_callback else None
        ),
    )

    all_texts: List[Tuple[str, int, str]] = []

    if is_chinese(novel_info.title):
        all_texts.append(("title", 0, novel_info.title))
        print(f"Will translate title: {novel_info.title}")
    if is_chinese(novel_info.author):
        all_texts.append(("author", 0, novel_info.author))
        print(f"Will translate author: {novel_info.author}")
    if novel_info.description and is_chinese(novel_info.description):
        all_texts.append(("description", 0, novel_info.description))
        print("Will translate description")
    for idx, chapter in enumerate(chapters):
        if is_chinese(chapter.title):
            all_texts.append(("chapter_title", idx, chapter.title))
    print(
        f"Will translate {sum(1 for t in all_texts if t[0] == 'chapter_title')} "
        "chapter titles"
    )

    for idx, chapter in enumerate(chapters):
        current_step += 1
        now = time.monotonic()
        if progress_callback and (
            idx == 0
            or idx + 1 == len(chapters)
            or now - last_clean_ui >= 0.07
        ):
            last_clean_ui = now
            progress_callback(
                current_step, total_steps, f"Cleaning: {chapter.title[:30]}..."
            )
            time.sleep(0)
        cleaned = getattr(chapter, "cleaned_html", "") or ""
        if cleaned:
            chapter.content = cleaned
        elif builder.cleaner:
            chapter.content = builder.cleaner.clean_html(chapter.content)
        for text in builder._extract_text_segments(chapter.content):
            if is_chinese(text) and len(text.strip()) > 0:
                all_texts.append(("content", idx, text))

    engine = translator_progress_label(getattr(translator, "backend", "") or "")
    print(f"Total segments to translate: {len(all_texts)}")
    n_seg = max(len(all_texts), 1)
    if progress_callback:
        # Leave "Starting download…" as soon as N is known — before harvest
        # (pypinyin on 686 chapters can take several seconds) and before HTTP.
        n_ch = max(len(chapters), 1)
        progress_callback(
            n_ch + 0.25,
            total_steps,
            _translation_status_line(
                engine,
                0,
                n_seg,
                in_flight=_zero_n_in_flight(translator),
            ),
        )
        time.sleep(0)

    harvest = getattr(translator, "harvest_names_from_texts", None)
    if callable(harvest):
        if progress_callback:
            progress_callback(
                current_step, total_steps, "Learning character names…"
            )
        try:
            harvest(
                [item[2] for item in all_texts],
                novel_title=getattr(novel_info, "title", "") or "",
            )
        except Exception as exc:
            print(f"  Name harvest skipped: {exc}")

    # Qwen classify is Help → Polish glossaries / startup modal, not this pass.

    if progress_callback:
        n_ch = max(len(chapters), 1)
        progress_callback(
            n_ch + 0.25,
            total_steps,
            _translation_status_line(
                engine,
                0,
                n_seg,
                in_flight=_zero_n_in_flight(translator),
            ),
        )
        time.sleep(0)

    if all_texts:
        texts_to_translate = [t[2] for t in all_texts]
        net_clock: Optional[float] = None
        requests_at_clock = 0
        retry_pass_num = 0

        def _network_requests() -> int:
            stats = getattr(translator, "stats", None) or {}
            try:
                return int(stats.get("requests", 0) or 0)
            except Exception:
                return 0

        def _pack_progress() -> Tuple[int, int]:
            done = int(getattr(translator, "pack_done", 0) or 0)
            total = int(getattr(translator, "pack_total", 0) or 0)
            return done, total

        def _chapter_note(completed: int) -> str:
            slot = int(getattr(translator, "_progress_source_index", -1) or -1)
            return _chapter_note_for_slot(all_texts, chapters, completed, slot)

        def translate_progress(completed, total):
            nonlocal current_step, net_clock, requests_at_clock
            if not progress_callback or total <= 0:
                return
            eta = ""
            requests = _network_requests()
            pack_done, pack_total = _pack_progress()
            backend = (getattr(translator, "backend", "") or "").strip().lower()
            engine = translator_progress_label(backend)
            if requests > 0 and net_clock is None:
                net_clock = time.monotonic()
                requests_at_clock = max(0, requests - 1)
            if net_clock is not None:
                elapsed = time.monotonic() - net_clock
                if pack_total > 0:
                    remaining_packs = max(0, pack_total - pack_done)
                    eta = eta_from_pack_samples(elapsed, pack_done, remaining_packs)
                else:
                    net_done = max(0, requests - requests_at_clock)
                    remaining = total - completed
                    if backend in ("ctranslate2", "ollama"):
                        min_samples = 1
                    elif backend in (
                        "google", "google_html", "google_gtx", "microsoft",
                    ):
                        min_samples = min(8, max(2, total // 50))
                    else:
                        min_samples = 2
                    eta = _engine_eta(
                        elapsed, net_done, remaining, min_samples=min_samples
                    )
            hits = 0
            stats = getattr(translator, "stats", None) or {}
            try:
                hits = int(stats.get("cache_hits", 0) or 0)
            except Exception:
                pass
            in_flight = _planned_in_flight(translator)
            unique_requests = 0
            try:
                unique_requests = int(
                    getattr(translator, "_unique_requests", 0) or 0
                )
            except Exception:
                unique_requests = 0
            if completed <= 0 and in_flight <= 0:
                in_flight = _zero_n_in_flight(translator)
            status = _translation_status_line(
                engine,
                completed,
                total,
                retry_pass=retry_pass_num,
                cache_hits=hits,
                pack_done=pack_done,
                pack_total=pack_total,
                in_flight=in_flight,
                unique_requests=unique_requests,
                chapter_note=_chapter_note(completed),
                eta=eta,
                network_requests=requests,
            )
            n_ch = max(len(chapters), 1)
            frac_done = completed / total if total else 0.0
            current = n_ch + frac_done * n_ch
            if total > 0 and current <= n_ch:
                current = n_ch + 0.25
            current_step = current
            progress_callback(current, total_steps, status)

        def on_retry_pass(pass_number, remaining, total_segments, cooldown):
            nonlocal net_clock, retry_pass_num, requests_at_clock
            retry_pass_num = pass_number
            net_clock = None
            requests_at_clock = _network_requests()
            if not progress_callback:
                return
            engine = translator_progress_label(
                getattr(translator, "backend", "") or ""
            )
            if cooldown > 0:
                progress_callback(
                    current_step,
                    total_steps,
                    f"{engine} · Retry pass {pass_number}: cooling down "
                    f"{int(cooldown)}s ({remaining} left)...",
                )
            else:
                progress_callback(
                    current_step,
                    total_steps,
                    f"{engine} · Retry pass {pass_number}: retrying "
                    f"{remaining} segments...",
                )

        if hasattr(translator, "translate_texts_with_retry"):
            translated = translator.translate_texts_with_retry(
                texts_to_translate,
                translate_progress,
                is_chinese_fn=lambda t: is_chinese(t),
                count_chinese_fn=lambda t: count_chinese_chars(t),
                pass_callback=on_retry_pass,
            )
        else:
            translated = translator.translate_texts(
                texts_to_translate, translate_progress
            )

        if getattr(translator, "_cancel_requested", False):
            raise DownloadCancelled()

        if builder.polish and hasattr(translator, "polish_texts"):
            polish_start = time.monotonic()

            def polish_progress(completed, total):
                if not progress_callback or total <= 0:
                    return
                eta = ""
                if completed > 0 and completed < total:
                    elapsed = time.monotonic() - polish_start
                    eta = _engine_eta(
                        elapsed,
                        completed,
                        total - completed,
                        min_samples=1,
                    )
                progress_callback(
                    int(len(chapters) * 1.5),
                    total_steps,
                    f"Polishing English: {completed}/{total}{eta}",
                )

            print(f"Polishing {len(translated)} segments (KEEP/REPLACE, local LLM)...")
            translated = translator.polish_texts(translated, polish_progress)
            if getattr(translator, "_cancel_requested", False):
                builder.polish_cancelled = True
                print(
                    "Polish cancelled — packaging EPUB with machine translation "
                    "(already-polished spans kept)."
                )

        builder.apply_translations(novel_info, chapters, all_texts, translated)

    if builder.verify_translation:
        builder._verify_translations(chapters)

    for idx, chapter in enumerate(chapters):
        if not chapter.content or len(chapter.content.strip()) < 10:
            print(f"Warning: Chapter {idx} '{chapter.title}' has empty/minimal content")
            if not chapter.content:
                chapter.content = "<p>Chapter content not available.</p>"

    print("Building EPUB with translated content...")
    print(f"  Final title: {novel_info.title}")
    print(f"  Final author: {novel_info.author}")
    return builder.build(
        novel_info, chapters, output_path, progress_callback,
        skip_html_clean=True,
    )


def make_translator(
    *,
    cache: NovelCache,
    max_workers: int,
    backend: str = "google",
    libretranslate_url: str = "https://libretranslate.com",
    ollama_url: str = "http://127.0.0.1:11434",
    ollama_model: str = "qwen2.5:3b",
    glossary_mode: str = "auto",
) -> NovelTranslator:
    return NovelTranslator(
        max_workers=max_workers,
        backend=backend,
        libretranslate_url=libretranslate_url,
        ollama_url=ollama_url,
        ollama_model=ollama_model,
        persistent_cache=cache,
        glossary_mode=glossary_mode,
    )


_PREFETCH_DURING_FETCH = frozenset({
    "libretranslate",
    "ctranslate2",
    "offline",
    "offline_nmt",
    "nmt",
    "opus",
})


def backend_prefetches_during_fetch(backend: str) -> bool:
    """True when translation should be constructed before the scrape loop.

    LibreTranslate (and Offline NMT once the model is on disk) overlap
    translation with ``request_delay``. Google / Microsoft / Ollama do not
    hit their engines during prefetch, so fetch must not wait on them.
    """
    return (backend or "google").strip().lower() in _PREFETCH_DURING_FETCH


def prepare_translation(
    *,
    cache: NovelCache,
    workers: int,
    backend: str,
    libretranslate_url: str,
    ollama_url: str,
    ollama_model: str,
    clean: bool,
    translate: bool,
    glossary_mode: str = "auto",
    novel_info=None,
    chapters=None,
) -> Tuple[Optional[NovelTranslator], Optional[ContentCleaner]]:
    """Create the translator (and cleaner). Call before fetch only when prefetch helps."""
    cleaner = ContentCleaner() if clean else None
    if not translate:
        return None, cleaner
    translator = make_translator(
        cache=cache,
        max_workers=workers,
        backend=backend,
        libretranslate_url=libretranslate_url,
        ollama_url=ollama_url,
        ollama_model=ollama_model,
        glossary_mode=glossary_mode,
    )
    cfg = getattr(translator, "configure_glossary", None)
    if callable(cfg):
        try:
            cfg(novel_info, chapters, mode=glossary_mode)
        except Exception as exc:
            print(f"  Glossary skipped: {exc}")
    return translator, cleaner


def engines_for_chapter_fetch(
    *,
    cache: NovelCache,
    workers: int,
    backend: str,
    libretranslate_url: str,
    ollama_url: str,
    ollama_model: str,
    clean: bool,
    translate: bool,
    glossary_mode: str = "auto",
    novel_info=None,
    chapters=None,
) -> Tuple[Optional[NovelTranslator], Optional[ContentCleaner]]:
    """Translator before scrape only for engines that prefetch during fetch.

    Google still gets a glossary fingerprint on the final translate pass
    (``translate_then_build`` → ``configure_glossary``).
    """
    if translate and backend_prefetches_during_fetch(backend):
        return prepare_translation(
            cache=cache,
            workers=workers,
            backend=backend,
            libretranslate_url=libretranslate_url,
            ollama_url=ollama_url,
            ollama_model=ollama_model,
            clean=clean,
            translate=True,
            glossary_mode=glossary_mode,
            novel_info=novel_info,
            chapters=chapters,
        )
    return None, ContentCleaner() if clean else None


def _note_translated_chapter(control: DownloadControl, chapter: Chapter) -> None:
    job = getattr(control, "active_job", None)
    if not job or not getattr(chapter, "url", ""):
        return
    urls = job.setdefault("translated_urls", [])
    if chapter.url not in urls:
        urls.append(chapter.url)
    try:
        control.persist_job()
    except Exception:
        pass


def _prefetch_chapter(translator, cleaner, chapter, control=None) -> None:
    prefetch = getattr(translator, "prefetch_chapter", None)
    html = getattr(chapter, "content", "") if chapter is not None else ""
    if translator is None or not html or not callable(prefetch):
        return

    def on_applied(ch):
        if control is not None:
            _note_translated_chapter(control, ch)

    try:
        prefetch(html, cleaner=cleaner, chapter=chapter, on_applied=on_applied)
    except TypeError:
        try:
            prefetch(html, cleaner=cleaner)
        except Exception as exc:
            print(f"  Translation prefetch skipped: {exc}")
    except Exception as exc:
        print(f"  Translation prefetch skipped: {exc}")


def speculative_prefetch_cached_chapters(
    *,
    cache: NovelCache,
    chapters: List[Chapter],
    translator,
    cleaner=None,
) -> int:
    """
    Warm translation for already-cached chapter HTML (no extra site fetches).
    Used after Library Check when Translate is on.
    """
    if translator is None or cache is None or not chapters:
        return 0
    warmed = 0
    for chapter in chapters:
        html = ""
        try:
            html = cache.get_chapter(chapter.url) or ""
        except Exception:
            html = ""
        if not html:
            continue
        chapter.content = html
        _prefetch_chapter(translator, cleaner, chapter)
        warmed += 1
    wait = getattr(translator, "wait_prefetch", None)
    if callable(wait):
        try:
            wait()
        except Exception:
            pass
    return warmed


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
    glossary_mode: str = "auto",
    translator=None,
    cleaner=None,
) -> EpubBuildResult:
    """Phase 2: build EPUB (optionally with translation). Progress 0..1 within this phase."""
    polish = bool(ollama_polish) and backend != "ollama"
    if translate and polish:
        _forward_progress(
            set_progress, set_status, 0.0, "Translating, then polishing English…"
        )
    elif translate:
        _forward_progress(set_progress, set_status, 0.0, "Translating chapters…")
    else:
        set_status("Writing EPUB…")
    if cleaner is None:
        cleaner = ContentCleaner() if clean else None
    _learn_site_junk(cleaner, chapters, set_status=set_status)
    if translate:
        translator = translator or make_translator(
            cache=cache,
            max_workers=workers,
            backend=backend,
            libretranslate_url=libretranslate_url,
            ollama_url=ollama_url,
            ollama_model=ollama_model,
            glossary_mode=glossary_mode,
        )
    else:
        translator = None

    _bind_translator(control, translator)

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
            _forward_progress(
                set_progress,
                set_status,
                current / max(total_steps, 1),
                status,
            )

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
        _forward_progress(
            set_progress,
            set_status,
            current / max(total_steps, 1),
            status,
        )

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
    glossary_mode: str = "auto",
) -> Tuple[List[str], EpubBuildResult]:
    """
    Full single-novel download + EPUB. Progress 0..1 overall.
    Returns (failed chapter titles, build result). Raises DownloadCancelled.
    """
    book_key = info.source_url if info else ""
    if translate and backend_prefetches_during_fetch(backend):
        set_status("Preparing translation…")
        try:
            set_progress(0)
        except TypeError:
            pass
    translator, cleaner = engines_for_chapter_fetch(
        cache=cache,
        workers=workers,
        backend=backend,
        libretranslate_url=libretranslate_url,
        ollama_url=ollama_url,
        ollama_model=ollama_model,
        clean=clean,
        translate=translate,
        glossary_mode=glossary_mode,
        novel_info=info,
        chapters=chapters,
    )

    def set_prog_dl(f, status=""):
        try:
            set_progress(f / 2, status)
        except TypeError:
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
        translator=translator,
        cleaner=cleaner,
    )

    def set_prog_build(f, status=""):
        try:
            set_progress(0.5 + f * 0.5, status)
        except TypeError:
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
        glossary_mode=glossary_mode,
        set_status=set_status,
        set_progress=set_prog_build,
        translator=translator,
        cleaner=cleaner,
    )

    record_successful_download(
        library_store, info, chapters, translated_title, output_path
    )
    return failed, build_result
