# Author: joelsnl and Anthropic Claude
"""What the translate stage tells the UI: status lines, ETAs and retry-pass messages.

Pure helpers plus ``TranslationProgress``, the translator's progress callback for one build.
No Qt imports. ETAs only count work that went to the network, so cached chapters never show
"0s left".
"""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

from core.gtx_throttle import GtxThrottle
from core.parser import Chapter
from core.translator import THROTTLED_BACKENDS
from core.utils import format_count, format_eta, format_ratio


def eta_from_network_samples(
    network_elapsed: float,
    network_done: int,
    network_remaining: int,
) -> str:
    """ETA text from uncached/network samples only. Empty until we have a sample."""
    if network_done < 1 or network_remaining <= 0 or network_elapsed <= 0:
        return ""
    avg = network_elapsed / network_done
    return f" · {format_eta(avg * network_remaining)} left"


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
    return f" · {format_eta(packs_remaining * (elapsed / packs_done))} left"


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


def engine_eta(
    elapsed: float,
    completed: int,
    remaining: int,
    *,
    min_samples: int,
) -> str:
    if completed < min_samples or remaining <= 0 or elapsed <= 0:
        return ""
    return f" · {format_eta(remaining * (elapsed / completed))} left"


def chapter_note_for_slot(
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
        return f" · ch {format_ratio(idx + 1, len(chapters))} {title}"
    return ""


def _pass_request_bound(translator) -> int:
    """How many unique GETs this pass can still send (0 if unknown)."""
    try:
        unique = int(getattr(translator, "_unique_requests", 0) or 0)
        if unique > 0:
            return unique
        return int(getattr(translator, "total", 0) or 0)
    except Exception:
        return 0


def planned_in_flight(translator) -> int:
    """In-flight for the footer: live gate, else this pass's planned GETs.

    Never report the first-pass ceiling (200) as "in flight" on a 1–2
    leftover retry. Cap by unique requests / pass size.
    """
    try:
        bound = _pass_request_bound(translator)
        gate = getattr(translator, "_gtx", None)
        if gate is not None:
            cur = int(getattr(gate, "current", 0) or 0)
            if cur > 0:
                return min(cur, bound) if bound > 0 else cur
            planned = int(getattr(translator, "_in_flight", 0) or 0)
            if planned > 0:
                return min(planned, bound) if bound > 0 else planned
            if bound > 0:
                return min(int(getattr(gate, "limit", 0) or 0), bound)
            return 0
        planned = int(getattr(translator, "_in_flight", 0) or 0)
        if bound > 0 and planned > 0:
            return min(planned, bound)
        return planned
    except Exception:
        return 0


def zero_n_in_flight(translator) -> int:
    """Planned in-flight before the first GET of a pass returns."""
    planned = planned_in_flight(translator)
    if planned > 0:
        return planned
    bound = _pass_request_bound(translator)
    backend = (getattr(translator, "backend", "") or "").strip().lower()
    if bound > 0:
        if backend in THROTTLED_BACKENDS:
            gate = getattr(translator, "_gtx", None)
            cap = (
                int(getattr(gate, "limit", 0) or GtxThrottle.START_LIMIT)
                if gate is not None
                else bound
            )
            return max(1, min(cap, bound))
        return bound
    if backend in THROTTLED_BACKENDS:
        return GtxThrottle.START_LIMIT
    return 0


def translation_status_line(
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
    ratio = format_ratio(completed, total)
    cache_note = ""
    if cache_hits and completed:
        cache_note = f" · {format_count(min(cache_hits, completed))} cached"
    pack_note = (
        f" · {format_ratio(pack_done, pack_total)} packs" if pack_total else ""
    )
    unique_note = ""
    if unique_requests > 0 and network_requests <= 0:
        unique_note = f" · {format_count(unique_requests)} unique requests"
    flight_note = f" · {format_count(in_flight)} in flight" if in_flight else ""
    if retry_pass > 0:
        prefix = f"{engine} · Retry pass {retry_pass}: {ratio}"
    else:
        prefix = f"{engine} · Translating: {ratio}"
    return (
        f"{prefix}{cache_note}{pack_note}{unique_note}{flight_note}"
        f"{chapter_note}{eta}"
    )


class TranslationProgress:
    """Status, ETA and retry-pass reporting while the translate pass runs.

    ``update`` is the translator's progress callback and ``retry_pass`` its pass callback.
    ``current_step`` is where the build's progress bar stands, kept for later messages.
    """

    def __init__(self, translator, chapters, all_texts, progress_callback, total_steps,
                 current_step):
        self.translator = translator
        self.chapters = chapters
        self.all_texts = all_texts
        self.progress_callback = progress_callback
        self.total_steps = total_steps
        self.current_step = current_step
        self.net_clock: Optional[float] = None
        self.requests_at_clock = 0
        self.retry_pass_number = 0

    def _stat(self, key: str) -> int:
        stats = getattr(self.translator, "stats", None) or {}
        try:
            return int(stats.get(key, 0) or 0)
        except Exception:
            return 0

    def _network_requests(self) -> int:
        return self._stat("requests")

    def _pack_progress(self) -> Tuple[int, int]:
        done = int(getattr(self.translator, "pack_done", 0) or 0)
        total = int(getattr(self.translator, "pack_total", 0) or 0)
        return done, total

    def _chapter_note(self, completed: int) -> str:
        slot = int(getattr(self.translator, "_progress_source_index", -1) or -1)
        return chapter_note_for_slot(self.all_texts, self.chapters, completed, slot)

    def _eta(self, backend: str, completed: int, total: int, requests: int,
             pack_done: int, pack_total: int) -> str:
        if self.net_clock is None:
            return ""
        elapsed = time.monotonic() - self.net_clock
        if pack_total > 0:
            return eta_from_pack_samples(elapsed, pack_done, max(0, pack_total - pack_done))
        net_done = max(0, requests - self.requests_at_clock)
        if backend in ("ctranslate2", "ollama"):
            min_samples = 1
        elif backend in ("google", "google_html", "google_gtx", "microsoft"):
            min_samples = min(8, max(2, total // 50))
        else:
            min_samples = 2
        return engine_eta(elapsed, net_done, total - completed, min_samples=min_samples)

    def update(self, completed, total):
        if not self.progress_callback or total <= 0:
            return
        requests = self._network_requests()
        pack_done, pack_total = self._pack_progress()
        backend = (getattr(self.translator, "backend", "") or "").strip().lower()
        if requests > 0 and self.net_clock is None:
            self.net_clock = time.monotonic()
            self.requests_at_clock = max(0, requests - 1)
        eta = self._eta(backend, completed, total, requests, pack_done, pack_total)
        in_flight = planned_in_flight(self.translator)
        if completed <= 0 and in_flight <= 0:
            in_flight = zero_n_in_flight(self.translator)
        status = translation_status_line(
            translator_progress_label(backend),
            completed,
            total,
            retry_pass=self.retry_pass_number,
            cache_hits=self._stat("cache_hits"),
            pack_done=pack_done,
            pack_total=pack_total,
            in_flight=in_flight,
            unique_requests=self._unique_requests(),
            chapter_note=self._chapter_note(completed),
            eta=eta,
            network_requests=requests,
        )
        n_ch = max(len(self.chapters), 1)
        current = n_ch + (completed / total if total else 0.0) * n_ch
        if total > 0 and current <= n_ch:
            current = n_ch + 0.25
        self.current_step = current
        self.progress_callback(current, self.total_steps, status)

    def _unique_requests(self) -> int:
        try:
            return int(getattr(self.translator, "_unique_requests", 0) or 0)
        except Exception:
            return 0

    def retry_pass(self, pass_number, remaining, total_segments, cooldown):
        self.retry_pass_number = pass_number
        self.net_clock = None
        self.requests_at_clock = self._network_requests()
        if not self.progress_callback:
            return
        engine = translator_progress_label(getattr(self.translator, "backend", "") or "")
        if cooldown > 0:
            message = (f"{engine} · Retry pass {pass_number}: cooling down "
                       f"{int(cooldown)}s ({remaining} left)...")
        else:
            message = f"{engine} · Retry pass {pass_number}: retrying {remaining} segments..."
        self.progress_callback(self.current_step, self.total_steps, message)
