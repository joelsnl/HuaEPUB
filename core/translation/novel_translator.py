# Author: joelsnl
"""
Glossary-aware translator used by the EPUB pipeline.

Wraps GoogleTranslator (Google / LibreTranslate / Ollama / CTranslate2).
Chapter downloads stay sequential; prefetch_chapter() only queues already
fetched HTML onto a side thread so scrape speed is unchanged. Google does
not hit unofficial gtx during prefetch (that 429'd the IP before the
200-wide pass). LibreTranslate still overlaps translation with request_delay.
Offline NMT prefetch runs only when the opus-mt files are already on disk —
chapter fetch must not pull ~320 MB or fall back to Google on every chapter.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.cache import chapter_fingerprint, normalize_cache_source
from core.cleaner import is_chinese
from core.translator import (
    DEFAULT_GOOGLE_WORKERS,
    MAX_PACKED_WORKERS,
    GoogleTranslator,
    RateLimitedError,
    is_usable_translation,
)
from core.translation.glossary import (
    GlossaryEngine,
    build_novel_glossary,
    looks_like_xianxia,
    normalize_glossary_mode,
)
from core.translation.nmt import (
    CTranslate2Engine,
    ensure_nmt_model,
    nmt_download_failed,
    nmt_model_ready,
    nmt_runtime_available,
)
from core.translation.pack import (
    PACK_CHAR_LIMIT,
    group_by_char_budget,
    pack_mt_segments,
    unpack_mt_segments,
)

# CTranslate2 translate_batch already uses max_batch_size=32 internally.
# Slice here too so the status bar can move instead of freezing on 15k segments.
NMT_UI_CHUNK = 32


class NovelTranslator(GoogleTranslator):
    """
    Same public API as GoogleTranslator, plus glossary hooks, Offline NMT,
    Google fallback, and non-blocking chapter prefetch.
    """

    def __init__(
        self,
        source_lang: str = "zh-CN",
        target_lang: str = "en",
        max_workers: int = DEFAULT_GOOGLE_WORKERS,
        request_timeout: int = 15,
        max_retries: int = 5,
        request_interval: float = 0.0,
        backend: str = "google",
        libretranslate_url: str = "https://libretranslate.com",
        ollama_url: str = "http://127.0.0.1:11434",
        ollama_model: str = "qwen2.5:3b",
        persistent_cache=None,
        use_glossary: bool = True,
        extra_terms=None,
        glossary: Optional[GlossaryEngine] = None,
        fallback_backend: str = "google",
        glossary_mode: str = "auto",
    ):
        self._use_glossary = bool(use_glossary)
        self._explicit_glossary = glossary is not None
        self._extra_terms = extra_terms
        self._glossary_mode = normalize_glossary_mode(glossary_mode)
        self._glossary_configured = False
        if glossary is not None:
            self.glossary = glossary
            if extra_terms:
                self.glossary.add_terms(extra_terms)
            self._glossary_configured = True
        elif use_glossary:
            # Wait for configure_glossary() so Auto can skip the xianxia pack.
            self.glossary = None
        else:
            self.glossary = None
            self._glossary_configured = True

        requested = (backend or "google").strip().lower()
        if requested in ("offline", "offline_nmt", "nmt", "opus"):
            requested = "ctranslate2"
        self._fallback_backend = (
            fallback_backend
            if fallback_backend in ("google", "libretranslate")
            else "google"
        )
        self._nmt: Optional[CTranslate2Engine] = None
        self._nmt_fallback_logged = False
        self._nmt_unavailable = False
        self._prefetch_lock = threading.Lock()
        self._prefetch_futures: list[concurrent.futures.Future] = []
        self._prefetch_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._pack_char_limit = PACK_CHAR_LIMIT
        self._pack_disabled = False
        self._unpack_fails = 0
        self._in_prefetch = False
        self._skip_prefetch_translate = False
        self.pack_total = 0
        self.pack_done = 0

        if requested == "ctranslate2" and not nmt_runtime_available():
            print(
                "Offline NMT packages missing "
                "(pip install -r requirements-nmt.txt). Using Google."
            )
            requested = self._fallback_backend
        elif requested == "ctranslate2":
            self._nmt = CTranslate2Engine()

        super().__init__(
            source_lang=source_lang,
            target_lang=target_lang,
            max_workers=max_workers,
            request_timeout=request_timeout,
            max_retries=max_retries,
            request_interval=request_interval,
            backend=requested,
            libretranslate_url=libretranslate_url,
            ollama_url=ollama_url,
            ollama_model=ollama_model,
            persistent_cache=persistent_cache,
        )
        if self.backend == "ctranslate2":
            self.max_workers = max(1, min(int(max_workers or 1), 4))
        elif self.backend in (
            "google", "google_html", "google_gtx", "microsoft", "libretranslate"
        ):
            self.max_workers = max(
                1, min(int(self.max_workers or DEFAULT_GOOGLE_WORKERS), MAX_PACKED_WORKERS)
            )
        self._configured_workers = self.max_workers

    def configure_glossary(
        self,
        novel_info=None,
        chapters=None,
        mode: Optional[str] = None,
        detect_text: str = "",
    ) -> None:
        """
        Attach user names, and the built-in cultivation pack only when needed.

        Call this before prefetch so cache keys include the final fingerprint.
        Idempotent. Auto with no title/TOC yet is deferred so a later call
        can still detect xianxia.
        """
        if self._glossary_configured:
            return
        if self._explicit_glossary or not self._use_glossary:
            self._glossary_configured = True
            return
        title = getattr(novel_info, "title", "") or ""
        desc = getattr(novel_info, "description", "") or ""
        ch_titles = [getattr(c, "title", "") or "" for c in (chapters or [])[:50]]
        blob = (detect_text or "").strip() or "\n".join([title, desc, *ch_titles])
        resolved = normalize_glossary_mode(
            mode if mode is not None else self._glossary_mode
        )
        if resolved == "auto" and not blob.strip():
            return
        self._glossary_mode = resolved
        self.glossary = build_novel_glossary(
            novel_title=title,
            mode=resolved,
            detect_text=blob,
            extra_terms=self._extra_terms,
        )
        self._glossary_configured = True
        n = len(self.glossary) if self.glossary else 0
        if self.glossary is None:
            print("Glossary: off")
            return
        used_pack = resolved == "xianxia" or (
            resolved == "auto" and looks_like_xianxia(blob, title)
        )
        if used_pack:
            print(
                f"Glossary: built-in cultivation pack + your names ({n} terms)"
            )
        elif resolved == "user":
            print(f"Glossary: your names only ({n} term(s))")
        else:
            print(
                "Glossary: your names only — skipped cultivation pack "
                f"(this book does not look like xianxia/wuxia; {n} term(s)). "
                "Set Glossary to Cultivation pack to force it."
            )

    def load_novel_glossary(self, title: str) -> None:
        """Merge ``~/.huaepub/glossaries/<title>.json`` if present."""
        if not title:
            return
        if not self._glossary_configured:
            self.configure_glossary(detect_text=title, mode=self._glossary_mode)
        if self.glossary is None:
            return
        from core.translation.glossary import _load_json_glossary, novel_glossary_path

        extra = _load_json_glossary(novel_glossary_path(title))
        if extra.terms:
            self.glossary.merge(extra, overwrite=True)
            print(f"  Novel glossary: {len(extra.terms)} extra term(s) for {title!r}")

    def harvest_names_from_texts(
        self,
        texts: list,
        *,
        novel_title: str = "",
    ) -> int:
        """Learn character names from this book and merge them for the final pass."""
        from core.translation.harvest import harvest_and_apply

        return harvest_and_apply(self, texts, novel_title=novel_title)

    def classify_glossary_with_qwen(
        self,
        texts: list,
        *,
        novel_title: str = "",
        complete_fn=None,
        cancelled=None,
        log=None,
    ) -> int:
        """Classify mined terms with local Qwen when the polish GGUF is already on disk."""
        from core.translation.qwen_glossary import classify_novel_with_qwen

        if self.glossary is None:
            return 0
        result = classify_novel_with_qwen(
            novel_title=novel_title,
            texts=list(texts or []),
            complete_fn=complete_fn,
            cancelled=cancelled or (lambda: self._cancel_requested),
            log=log,
            apply=True,
            engine=self.glossary,
            allow_download=False,
        )
        return int(result.get("added") or 0) + int(result.get("updated") or 0)

    def _cache_backend(self) -> str:
        base = super()._cache_backend()
        if self.glossary and self.glossary.fingerprint:
            return f"{base}+g{self.glossary.fingerprint}"
        return base

    def _legacy_cache_backends(self) -> List[str]:
        # Pre-glossary rows were stored as plain 'google' / 'libretranslate'.
        if self.backend in ("google", "google_html", "google_gtx"):
            return ["google"]
        if self.backend == "microsoft":
            return []
        if self.backend == "libretranslate":
            return [self.backend]
        return []

    def _get_cached_translation(self, source: str):
        cache = self.persistent_cache
        if cache is None or not source:
            return None
        primary = self._cache_backend()
        hit = cache.get_translation(source, primary)
        if hit:
            return self._accept_cached(source, self._restore(hit, None))
        for alias in self._legacy_cache_backends():
            if alias == primary:
                continue
            hit = cache.get_translation(source, alias)
            if not hit:
                continue
            swept = self._restore(hit, None)
            accepted = self._accept_cached(source, swept)
            if accepted and accepted != hit:
                try:
                    cache.put_translation(source, accepted, primary, commit=False)
                except Exception:
                    pass
            return accepted
        return None

    def _protect(self, text: str):
        if self.glossary is None:
            return text, None
        job = self.glossary.protect(text)
        return job.text, job

    def _restore(self, text: str, job) -> str:
        if self.glossary is None:
            return text
        return self.glossary.restore(text, job)

    def _ensure_nmt(self) -> CTranslate2Engine:
        if self._nmt_unavailable or nmt_download_failed():
            self._nmt_unavailable = True
            raise RuntimeError("Offline NMT model is not available")
        if self._nmt is None:
            self._nmt = CTranslate2Engine()
        if not nmt_model_ready():
            ensure_nmt_model(cancelled=lambda: self._cancel_requested)
            self._nmt = CTranslate2Engine()
        return self._nmt

    def _request_ctranslate2(self, text: str) -> str:
        try:
            return self._ensure_nmt().translate(text)
        except Exception as exc:
            self._nmt_unavailable = True
            if not self._nmt_fallback_logged:
                print(
                    f"  Offline NMT failed ({exc}); "
                    "falling back to Google for remaining segments."
                )
                self._nmt_fallback_logged = True
            return self._request_google(text)

    def _request_translation(self, text: str) -> str:
        protected, job = self._protect(text)
        if self.backend == "ctranslate2":
            raw = self._request_ctranslate2(protected)
        else:
            raw = super()._request_translation(protected)
        return self._restore(raw, job)

    def translate_texts(
        self,
        texts: List[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[str]:
        if not texts:
            return []
        if self._cancel_requested:
            return list(texts)
        if self.backend == "ctranslate2":
            return self._translate_nmt_batch(texts, progress_callback)
        if self.backend == "libretranslate":
            return self._translate_packed(texts, progress_callback)
        # Google: one paragraph per request, up to 200 in flight — the old
        # fast path. Fat packs 429 gtx and the old throttle then pinned us at 4.
        return super().translate_texts(texts, progress_callback)

    def _cache_key(self, source: str) -> str:
        return normalize_cache_source(source)

    def _store_segment(self, source: str, translated: str) -> None:
        if not is_usable_translation(source, translated):
            return
        key = self._cache_key(source)
        if not key or not translated:
            return
        with self.cache_lock:
            self.cache[key] = translated
        if self.persistent_cache is not None:
            self.persistent_cache.put_translation(
                key, translated, self._cache_backend(), commit=False
            )

    def _lookup_segment(self, source: str) -> Optional[str]:
        key = self._cache_key(source)
        if not key:
            return None
        with self.cache_lock:
            hit = self.cache.get(key)
            if not hit:
                stripped = (source or "").strip()
                if stripped != key:
                    hit = self.cache.get(stripped)
        accepted = self._accept_cached(source, hit)
        if accepted:
            return accepted
        hit = self._get_cached_translation(key)
        if not hit and key != (source or "").strip():
            hit = self._get_cached_translation((source or "").strip())
        accepted = self._accept_cached(source, hit)
        if accepted:
            with self.cache_lock:
                self.cache[key] = accepted
        return accepted

    def _hydrate_bulk(self, sources: List[str]) -> Dict[str, str]:
        cache = self.persistent_cache
        if cache is None or not sources:
            return {}
        backends = [self._cache_backend()] + list(self._legacy_cache_backends() or [])
        getter = getattr(cache, "get_translations_bulk", None)
        hits: Dict[str, str] = {}
        if callable(getter):
            try:
                hits = getter(sources, backends) or {}
            except Exception:
                hits = {}
        if not hits:
            for src in sources:
                found = self._get_cached_translation(src)
                if found:
                    hits[src.strip()] = found
        swept: Dict[str, str] = {}
        primary = self._cache_backend()
        for src, text in hits.items():
            restored = self._restore(text, None)
            if not is_usable_translation(src, restored):
                self._forget_cached(src)
                continue
            key = self._cache_key(src)
            swept[key] = restored
            with self.cache_lock:
                self.cache[key] = restored
            if restored != text:
                try:
                    cache.put_translation(key, restored, primary, commit=False)
                except Exception:
                    pass
        return swept

    def _translate_one_raw(self, text: str) -> str:
        """One engine call, no extra progress tick (used when a pack fails)."""
        try:
            out = self._request_translation(text)
            if out and out.strip():
                return out
        except RateLimitedError as exc:
            self._note_throttle(exc.retry_after)
        except Exception:
            pass
        return text

    def _note_unpack_failure(self) -> None:
        with self.stats_lock:
            self._unpack_fails = int(self._unpack_fails or 0) + 1
            if self._unpack_fails >= 2 and not self._pack_disabled:
                self._pack_disabled = True
                print(
                    "  Pack markers were lost; switching to per-paragraph "
                    "Google calls (the old 200-wide path)."
                )

    def _translate_raw_many(
        self,
        segments: List[str],
        fallback: Optional[concurrent.futures.ThreadPoolExecutor] = None,
    ) -> List[str]:
        if not segments:
            return []
        if len(segments) == 1:
            return [self._translate_one_raw(segments[0])]
        if fallback is None:
            return [self._translate_one_raw(seg) for seg in segments]
        futs = [fallback.submit(self._translate_one_raw, seg) for seg in segments]
        return [fut.result() for fut in futs]

    def _translate_pack_group(
        self,
        segments: List[str],
        fallback: Optional[concurrent.futures.ThreadPoolExecutor] = None,
    ) -> List[str]:
        if len(segments) == 1 or self._pack_disabled:
            return self._translate_raw_many(segments, fallback)
        blob = pack_mt_segments(segments)
        try:
            raw = self._request_translation(blob)
        except RateLimitedError as exc:
            self._note_throttle(exc.retry_after)
            try:
                raw = self._request_translation(blob)
            except Exception:
                return self._translate_raw_many(segments, fallback)
        except Exception:
            return self._translate_raw_many(segments, fallback)
        parts = unpack_mt_segments(raw, len(segments))
        if parts is None:
            self._note_unpack_failure()
            return self._translate_raw_many(segments, fallback)
        return parts

    def _translate_packed(
        self,
        texts: List[str],
        progress_callback: Optional[Callable[[int, int], None]],
    ) -> List[str]:
        self.total = len(texts)
        self.completed = 0
        self.failed_texts = []
        self.progress_callback = progress_callback
        self.pack_total = 0
        self.pack_done = 0
        self._unique_requests = 0
        self._progress_source_index = -1
        self._active_source_indices.clear()
        self._emit_progress(force=True)
        time.sleep(0)
        results = list(texts)
        pending_idx: list[int] = []
        pending_src: list[str] = []

        for i, text in enumerate(texts):
            if self._cancel_requested:
                return results
            if not text or not text.strip():
                self._update_progress()
                continue
            key = self._cache_key(text)
            with self.cache_lock:
                cached = self.cache.get(key) or self.cache.get(text.strip())
            accepted = self._accept_cached(text, cached)
            if accepted:
                results[i] = accepted
                with self.stats_lock:
                    self.stats["cache_hits"] += 1
                self._update_progress()
                continue
            pending_idx.append(i)
            pending_src.append(text)

        if pending_src:
            bulk = self._hydrate_bulk(pending_src)
            still_idx: list[int] = []
            still_src: list[str] = []
            for i, text in zip(pending_idx, pending_src):
                hit = bulk.get(self._cache_key(text))
                if hit:
                    results[i] = hit
                    with self.stats_lock:
                        self.stats["cache_hits"] += 1
                    self._update_progress()
                    continue
                still_idx.append(i)
                still_src.append(text)
            pending_idx, pending_src = still_idx, still_src

        if not pending_src:
            self._flush_persistent_cache()
            return results

        if self._pack_disabled:
            groups = [[i] for i in range(len(pending_src))]
        else:
            groups = group_by_char_budget(pending_src, max_chars=self._pack_char_limit)
        self.pack_total = len(groups)
        if len(groups) < len(pending_src):
            print(
                f"  Packed {len(pending_src)} uncached segments into "
                f"{len(groups)} {self.backend} request(s) "
                f"(was {len(pending_src)} separate calls)."
            )

        workers = min(self.max_workers, max(1, len(groups)))
        need_fallback = any(len(group) > 1 for group in groups)
        fallback = (
            concurrent.futures.ThreadPoolExecutor(max_workers=workers)
            if need_fallback else None
        )

        def run_group(group: list[int]) -> tuple[list[int], list[str]]:
            self._wait_if_paused()
            if self._should_cancel():
                return group, [pending_src[j] for j in group]
            for j in group:
                self._mark_source_progress(pending_idx[j], inflight=True)
            try:
                segs = [pending_src[j] for j in group]
                return group, self._translate_pack_group(segs, fallback)
            finally:
                for j in group:
                    self._mark_source_progress(pending_idx[j], inflight=False)

        self._unique_requests = len(groups)
        self._in_flight = min(workers, len(groups))
        self._emit_progress(force=True)
        time.sleep(0)

        pending = iter(groups)
        inflight: Dict[Any, list[int]] = {}
        backlog = min(max(workers * 4, 32), len(groups))

        def _fill() -> None:
            while len(inflight) < backlog:
                try:
                    group = next(pending)
                except StopIteration:
                    return
                inflight[executor.submit(run_group, group)] = group

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        try:
            _fill()
            while inflight:
                if self._should_cancel():
                    break
                finished, _ = concurrent.futures.wait(
                    tuple(inflight),
                    timeout=0.07,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                self._emit_progress()
                if not finished:
                    continue
                for future in finished:
                    group = inflight.pop(future)
                    try:
                        group, translated = future.result()
                    except Exception:
                        continue
                    with self.stats_lock:
                        self.stats["requests"] += 1
                        self.pack_done += 1
                    for j, out in zip(group, translated):
                        i = pending_idx[j]
                        text = pending_src[j]
                        results[i] = out or text
                        if is_usable_translation(text, results[i]):
                            self._store_segment(text, results[i])
                        with self.stats_lock:
                            self.stats["paragraphs_translated"] += 1
                            self.stats["characters_translated"] += len(text)
                        self._update_progress()
                _fill()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            if fallback is not None:
                fallback.shutdown(wait=True, cancel_futures=True)

        self._flush_persistent_cache()
        return results

    def _google_fallback_many(self, texts: List[str]) -> List[str]:
        out: List[str] = []
        for text in texts:
            if self._should_cancel():
                out.append(text)
                continue
            try:
                out.append(self._request_google(text))
            except Exception:
                out.append(text)
        return out

    def _translate_nmt_batch(
        self,
        texts: List[str],
        progress_callback: Optional[Callable[[int, int], None]],
    ) -> List[str]:
        if self._cancel_requested:
            return list(texts)
        self.total = len(texts)
        self.completed = 0
        self.failed_texts = []
        self.progress_callback = progress_callback
        self._progress_source_index = -1
        self._active_source_indices.clear()
        self._unique_requests = 0
        self._in_flight = 0
        self._emit_progress(force=True)
        time.sleep(0)
        results = list(texts)
        pending_idx: list[int] = []
        pending_jobs = []
        pending_src: list[str] = []

        for i, text in enumerate(texts):
            if self._cancel_requested:
                return results
            if not text or not text.strip():
                self._update_progress()
                continue
            cached = self._lookup_segment(text)
            if cached:
                results[i] = cached
                with self.stats_lock:
                    self.stats["cache_hits"] += 1
                self._update_progress()
                continue
            protected, job = self._protect(text)
            pending_idx.append(i)
            pending_jobs.append(job)
            pending_src.append(protected)

        if pending_src and not self._cancel_requested:
            engine = None
            use_google = False
            try:
                engine = self._ensure_nmt()
            except Exception as exc:
                use_google = True
                self._nmt_unavailable = True
                if not self._nmt_fallback_logged:
                    print(f"  Offline NMT batch failed ({exc}); using Google.")
                    self._nmt_fallback_logged = True
            for start in range(0, len(pending_src), NMT_UI_CHUNK):
                if self._cancel_requested:
                    break
                chunk_src = pending_src[start:start + NMT_UI_CHUNK]
                chunk_idx = pending_idx[start:start + NMT_UI_CHUNK]
                chunk_jobs = pending_jobs[start:start + NMT_UI_CHUNK]
                for i in chunk_idx:
                    self._mark_source_progress(i, inflight=True)
                self._in_flight = len(chunk_src)
                self._emit_progress(force=True)
                try:
                    if use_google or engine is None:
                        raw_chunk = self._google_fallback_many(chunk_src)
                    else:
                        raw_chunk = engine.translate_batch(chunk_src)
                except Exception as exc:
                    self._nmt_unavailable = True
                    use_google = True
                    engine = None
                    if not self._nmt_fallback_logged:
                        print(f"  Offline NMT batch failed ({exc}); using Google.")
                        self._nmt_fallback_logged = True
                    raw_chunk = self._google_fallback_many(chunk_src)
                finally:
                    self._in_flight = 0
                    for i in chunk_idx:
                        self._mark_source_progress(i, inflight=False)
                self._store_nmt_outputs(
                    texts, results, chunk_idx, chunk_jobs, raw_chunk
                )
        self._in_flight = 0
        self._flush_persistent_cache()
        return results

    def _store_nmt_outputs(
        self,
        texts: List[str],
        results: List[str],
        pending_idx: list[int],
        pending_jobs,
        raw_out: list[str],
    ) -> None:
        for i, job, translated in zip(pending_idx, pending_jobs, raw_out):
            self._mark_source_progress(i, inflight=False)
            restored = self._restore(translated or texts[i], job)
            results[i] = restored
            if is_usable_translation(texts[i], restored):
                cache_key = texts[i].strip()
                with self.cache_lock:
                    self.cache[cache_key] = restored
                if self.persistent_cache is not None:
                    self.persistent_cache.put_translation(
                        cache_key, restored, self._cache_backend(), commit=False
                    )
            with self.stats_lock:
                self.stats["requests"] += 1
                self.stats["paragraphs_translated"] += 1
                self.stats["characters_translated"] += len(texts[i])
            self._update_progress()

    def _ensure_prefetch_pool(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._prefetch_pool is None:
            # Keep this small. Each job also opens a pack executor; 32×32
            # in-flight gtx calls is what poisoned cache.db with Chinese.
            self._prefetch_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=2
            )
        return self._prefetch_pool

    def prefetch_chapter(
        self,
        html: str,
        cleaner=None,
        chapter=None,
        on_applied=None,
    ) -> None:
        """
        Queue translation of one chapter's Chinese segments.
        Returns immediately so sequential scraping is not delayed.
        When ``chapter`` is set, apply-as-you-go fills translated_content.
        """
        if self._cancel_requested or not html:
            return
        pool = self._ensure_prefetch_pool()
        future = pool.submit(
            self._prefetch_chapter_job, html, cleaner, chapter, on_applied
        )
        with self._prefetch_lock:
            self._prefetch_futures.append(future)

    def _segments_from_html(self, html: str, cleaner=None) -> Tuple[str, List[str]]:
        body = html
        if cleaner is not None:
            try:
                body = cleaner.clean_html(html)
            except Exception:
                body = html
        from core.epub_builder import extract_translatable_segments

        segments = [
            seg for seg in extract_translatable_segments(body)
            if seg and is_chinese(seg)
        ]
        return body, segments

    def translate_and_apply_html(
        self,
        html: str,
        cleaner=None,
        on_partial: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Translate Chinese nodes in HTML and return applied English HTML."""
        if self._cancel_requested or not html:
            return html or ""
        body, segments = self._segments_from_html(html, cleaner)
        if not segments:
            return body
        from core.epub_builder import apply_content_translations

        translated = self.translate_texts(segments)
        pairs = list(zip(segments, translated))
        applied = apply_content_translations(body, pairs)
        if on_partial:
            try:
                on_partial(applied)
            except Exception:
                pass
        return applied

    def _prefetch_chapter_job(self, html: str, cleaner, chapter=None, on_applied=None) -> None:
        if self._cancel_requested:
            return
        body, segments = self._segments_from_html(html, cleaner)
        if not segments:
            return
        from core.epub_builder import apply_content_translations
        cache = self.persistent_cache
        backend = self._cache_backend()
        fp = chapter_fingerprint(body)
        translated = None
        getter = getattr(cache, "get_chapter_translation", None) if cache is not None else None
        if callable(getter):
            try:
                cached_nodes = getter(fp, backend)
            except Exception:
                cached_nodes = None
            if cached_nodes and len(cached_nodes) == len(segments):
                if all(
                    is_usable_translation(src, dest)
                    for src, dest in zip(segments, cached_nodes)
                ):
                    translated = list(cached_nodes)
                    with self.stats_lock:
                        self.stats["cache_hits"] += len(translated)
                else:
                    dropper = getattr(cache, "delete_chapter_translation", None)
                    if callable(dropper):
                        try:
                            dropper(fp, backend)
                        except Exception:
                            pass
        if translated is None:
            if self.backend in ("google", "google_html", "google_gtx", "microsoft"):
                return
            if self.backend == "ctranslate2" and (
                not nmt_model_ready() or nmt_download_failed() or self._nmt_unavailable
            ):
                # Chapter fetch must not pull ~320 MB or fall back to Google
                # on every chapter. The final translate pass downloads once.
                return
            if getattr(self, "_skip_prefetch_translate", False):
                translated = list(segments)
            else:
                old_workers = self.max_workers
                self.max_workers = min(int(old_workers or 1), 8)
                self._in_prefetch = True
                try:
                    translated = self.translate_texts(segments)
                finally:
                    self._in_prefetch = False
                    self.max_workers = max(
                        int(old_workers or 1),
                        int(getattr(self, "_configured_workers", old_workers) or 1),
                    )
            if all(
                is_usable_translation(src, dest)
                for src, dest in zip(segments, translated)
            ):
                putter = (
                    getattr(cache, "put_chapter_translation", None)
                    if cache is not None else None
                )
                if callable(putter):
                    try:
                        putter(fp, backend, translated, commit=False)
                    except Exception:
                        pass
        pairs = list(zip(segments, translated))
        applied = apply_content_translations(body, pairs)
        usable = any(is_usable_translation(src, dest) for src, dest in pairs)
        if chapter is not None:
            chapter.cleaned_html = body
            chapter.translation_pairs = pairs
            chapter.translated_content = applied if usable else ""
            chapter.translation_applied = usable
        if callable(on_applied) and chapter is not None:
            try:
                on_applied(chapter)
            except Exception:
                pass

    def wait_prefetch(self) -> None:
        with self._prefetch_lock:
            futures = list(self._prefetch_futures)
            self._prefetch_futures.clear()
        if not futures:
            return
        concurrent.futures.wait(futures)
        for future in futures:
            try:
                future.result()
            except Exception as exc:
                print(f"  Translation prefetch failed: {exc}")

    def cancel(self):
        super().cancel()
        pool = self._prefetch_pool
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    async def translate_batch(
        self,
        texts: List[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[str]:
        """Async wrapper around the thread-pool / CTranslate2 batch path."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.translate_texts(texts, progress_callback)
        )
