# Author: joelsnl and Anthropic Claude
"""
Google Translate (Free) - Concurrent translation with persistent retry

Persistent retry system:
- Keeps retrying ALL failed translations until everything is done (or cancelled)
- Smart escalating delays between passes: workers scale down, intervals widen,
  cooldowns lengthen, per-request retries increase
- Stall detection: if no progress for 3+ passes, switches to maximum backoff
- Cache is cleared for failed entries before each retry so fresh requests are made
- Cancellable at any point via cancel() method

HTTP: each worker thread keeps a Session (curl_cffi when available) so TCP/TLS
connections are reused. On Windows, urllib3 IPv6 is disabled for translate
requests — broken AAAA routes otherwise add multi-second stalls per call.
"""

import re
import sys
import requests
import time
import threading
import concurrent.futures
from typing import List, Tuple, Dict, Optional, Callable, Any

# Windows often has broken/slow IPv6; urllib3 tries AAAA first and burns the
# connect timeout per address. Prefer IPv4 for requests-based translate calls.
if sys.platform == "win32":
    try:
        import urllib3.util.connection as _urllib3_conn
        _urllib3_conn.HAS_IPV6 = False
    except Exception:
        pass


class GoogleTranslator:
    """
    Concurrent translator with retry logic and multi-pass retry.
    
    Backends:
    - 'google' (default): the free Google Translate endpoint
    - 'libretranslate': a LibreTranslate server (public instance or self-hosted),
      configured via libretranslate_url
    
    Optionally uses a persistent cache (core.cache.NovelCache) so repeated
    runs and recurring phrases across novels cost zero API requests.
    """
    
    ENDPOINT = 'https://translate.googleapis.com/translate_a/single'
    USER_AGENT = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    )
    
    def __init__(
        self,
        source_lang: str = 'zh-CN',
        target_lang: str = 'en',
        max_workers: int = 200,
        request_timeout: int = 15,
        max_retries: int = 5,
        request_interval: float = 0.0,
        backend: str = 'google',
        libretranslate_url: str = 'https://libretranslate.com',
        persistent_cache=None,
    ):
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.max_workers = max_workers
        self.request_timeout = request_timeout
        # (connect, read) — short connect avoids long IPv6 black-hole waits
        self._timeout = (min(5, request_timeout), request_timeout)
        self.max_retries = max_retries
        self.request_interval = request_interval
        self.backend = backend if backend in ('google', 'libretranslate') else 'google'
        self.persistent_cache = persistent_cache
        if self.backend == 'libretranslate':
            from core.security import UnsafeURLError, validate_libretranslate_url
            try:
                # Literal / scheme checks at init; DNS checked per-request
                self.libretranslate_url = validate_libretranslate_url(
                    libretranslate_url, resolve_dns=False
                )
            except UnsafeURLError as e:
                raise ValueError(f"Invalid LibreTranslate URL: {e}") from e
        else:
            self.libretranslate_url = (libretranslate_url or '').rstrip('/')
        
        # Statistics
        self.stats = {
            'requests': 0,
            'paragraphs_translated': 0,
            'characters_translated': 0,
            'cache_hits': 0,
            'errors': 0,
            'retries': 0,
            'retry_passes': 0,
        }
        
        # Thread-safe cache and counters
        self.cache: Dict[str, str] = {}
        self.cache_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.progress_lock = threading.Lock()
        self._thread_local = threading.local()
        
        # Progress tracking
        self.completed = 0
        self.total = 0
        self.progress_callback: Optional[Callable[[int, int], None]] = None
        
        # Failed texts for reporting
        self.failed_texts: List[Tuple[int, str]] = []
        self.failed_lock = threading.Lock()
        
        # Control flag
        self._cancel_requested = False
    
    def cancel(self):
        """Request cancellation of ongoing translation."""
        self._cancel_requested = True
    
    def _get_http_session(self) -> Any:
        """
        Per-thread HTTP session with connection reuse.
        Prefer curl_cffi (Chrome TLS) to match the rest of the app.
        """
        sess = getattr(self._thread_local, "session", None)
        if sess is not None:
            return sess
        try:
            from curl_cffi.requests import Session as CurlSession
            sess = CurlSession(impersonate="chrome120")
            self._thread_local.http_backend = "curl_cffi"
        except ImportError:
            sess = requests.Session()
            sess.headers.update({"User-Agent": self.USER_AGENT})
            try:
                from requests.adapters import HTTPAdapter
                adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
                sess.mount("https://", adapter)
                sess.mount("http://", adapter)
            except Exception:
                pass
            self._thread_local.http_backend = "requests"
        self._thread_local.session = sess
        return sess
    
    # ------------------------------------------------------------------
    # Backend requests
    # ------------------------------------------------------------------
    
    def _request_google(self, text: str) -> str:
        """Translate via the free Google Translate endpoint."""
        params = {
            'client': 'gtx',
            'sl': self.source_lang,
            'tl': self.target_lang,
            'dt': 't',
            'dj': '1',
            'q': text
        }
        session = self._get_http_session()
        headers = {'User-Agent': self.USER_AGENT}
        
        # Use GET for short texts, POST for long texts
        if len(text) <= 1800:
            response = session.get(
                self.ENDPOINT,
                params=params,
                headers=headers,
                timeout=self._timeout,
            )
        else:
            response = session.post(
                self.ENDPOINT,
                data=params,
                headers=headers,
                timeout=self._timeout,
            )
        
        response.raise_for_status()
        data = response.json()
        
        return ''.join(
            s.get('trans', '')
            for s in data.get('sentences', [])
            if 'trans' in s
        )
    
    def _request_libretranslate(self, text: str) -> str:
        """Translate via a LibreTranslate server."""
        from core.security import UnsafeURLError, validate_fetch_url
        try:
            validate_fetch_url(self.libretranslate_url, allow_http=True, resolve_dns=True)
        except UnsafeURLError as e:
            raise ValueError(f"Blocked LibreTranslate URL: {e}") from e
        # LibreTranslate uses plain ISO codes ('zh', not 'zh-CN')
        source = self.source_lang.split('-')[0]
        session = self._get_http_session()
        response = session.post(
            f'{self.libretranslate_url}/translate',
            json={
                'q': text,
                'source': source,
                'target': self.target_lang,
                'format': 'text',
            },
            headers={'User-Agent': self.USER_AGENT},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json().get('translatedText', '')
    
    def _request_translation(self, text: str) -> str:
        if self.backend == 'libretranslate':
            return self._request_libretranslate(text)
        return self._request_google(text)
    
    def _translate_single(self, text: str, index: int) -> Tuple[int, str]:
        """Translate a single text with exponential backoff retry."""
        if self._cancel_requested:
            return (index, text)
            
        if not text or not text.strip():
            return (index, text)
        
        cache_key = text.strip()
        
        # Check in-memory cache
        with self.cache_lock:
            if cache_key in self.cache:
                with self.stats_lock:
                    self.stats['cache_hits'] += 1
                self._update_progress()
                return (index, self.cache[cache_key])
        
        # Check persistent cache
        if self.persistent_cache is not None:
            cached = self.persistent_cache.get_translation(cache_key, self.backend)
            if cached:
                with self.cache_lock:
                    self.cache[cache_key] = cached
                with self.stats_lock:
                    self.stats['cache_hits'] += 1
                self._update_progress()
                return (index, cached)
        
        last_error = None
        for attempt in range(self.max_retries):
            if self._cancel_requested:
                return (index, text)
                
            try:
                translated = self._request_translation(text)
                
                if translated and translated.strip():
                    # Cache the result (memory + persistent)
                    with self.cache_lock:
                        self.cache[cache_key] = translated
                    if self.persistent_cache is not None:
                        self.persistent_cache.put_translation(cache_key, translated, self.backend)
                    
                    with self.stats_lock:
                        self.stats['requests'] += 1
                        self.stats['paragraphs_translated'] += 1
                        self.stats['characters_translated'] += len(text)
                        if attempt > 0:
                            self.stats['retries'] += attempt
                    
                    self._update_progress()
                    
                    if self.request_interval > 0:
                        time.sleep(self.request_interval)
                    
                    return (index, translated)
                else:
                    raise ValueError("Empty translation response")
                    
            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    # Exponential backoff: 2, 4, 8, 16... seconds
                    wait_time = 2 ** (attempt + 1)
                    time.sleep(wait_time)
        
        # All retries failed
        with self.failed_lock:
            preview = text[:50] + '...' if len(text) > 50 else text
            self.failed_texts.append((index, preview))
        
        with self.stats_lock:
            self.stats['errors'] += 1
        
        self._update_progress()
        return (index, text)  # Return original on failure
    
    def _update_progress(self):
        """Update progress counter and call callback if set."""
        with self.progress_lock:
            self.completed += 1
            if self.progress_callback and self.total > 0:
                self.progress_callback(self.completed, self.total)
    
    def translate_texts(
        self,
        texts: List[str],
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> List[str]:
        """
        Translate a list of texts concurrently.
        
        Args:
            texts: List of texts to translate
            progress_callback: Optional callback(completed, total) for progress updates
            
        Returns:
            List of translated texts in same order as input
        """
        if not texts:
            return []
        
        self._cancel_requested = False
        self.total = len(texts)
        self.completed = 0
        self.failed_texts = []
        self.progress_callback = progress_callback
        
        workers = min(self.max_workers, len(texts))
        results = [''] * len(texts)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._translate_single, text, i): i 
                for i, text in enumerate(texts)
            }
            
            for future in concurrent.futures.as_completed(futures):
                if self._cancel_requested:
                    break
                try:
                    index, translated = future.result()
                    results[index] = translated
                except Exception:
                    index = futures[future]
                    results[index] = texts[index]
        
        return results
    
    def translate_texts_with_retry(
        self,
        texts: List[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
        is_chinese_fn=None,
        count_chinese_fn=None,
        pass_callback: Optional[Callable[[int, int, int, float], None]] = None,
        max_retry_passes: int = 8,
    ) -> List[str]:
        """
        Translate texts and keep retrying ALL failures until everything is done.
        
        Uses a smart delay system that escalates between retry passes:
        - Workers scale down:   100 → 50 → 30 → 20 → 10 → 5 (floor)
        - Request interval up:  0 → 0.3 → 0.5 → 1.0 → 1.5 → 2.0 (cap)
        - Cooldown between passes: 5 → 10 → 20 → 30 → 60 → 60 (cap at 60s)
        - Per-request retries increase: base → +1 → +2 (cap at base+3)
        
        Guaranteed to terminate: stops when zero failures remain, when cancelled,
        after max_retry_passes retry passes, or after 3 consecutive passes with
        no progress. Segments that never fully translate keep their best partial
        translation (or original text) so the EPUB build can always proceed.
        
        Args:
            texts: List of texts to translate
            progress_callback: Optional callback(completed, total) for per-text progress
            is_chinese_fn: Function to check if text contains Chinese
            count_chinese_fn: Function to count Chinese chars
            pass_callback: Optional callback(pass_number, remaining, total, cooldown)
                           called at the start of each retry pass
            max_retry_passes: Hard cap on retry passes before giving up
            
        Returns:
            List of translated texts
        """
        if not texts:
            return []
        
        # Use default Chinese detection if not provided
        if is_chinese_fn is None:
            is_chinese_fn = self._contains_chinese
        if count_chinese_fn is None:
            count_chinese_fn = self._count_chinese
        
        # Smart delay escalation tables
        # Each index = retry pass number (0-based), values plateau at the last entry
        WORKER_STEPS    = [0, 50, 30, 20, 10, 5]       # 0 = use initial max_workers
        INTERVAL_STEPS  = [0.0, 0.3, 0.5, 1.0, 1.5, 2.0]
        COOLDOWN_STEPS  = [0, 5, 10, 20, 30, 60]
        EXTRA_RETRIES   = [0, 0, 1, 1, 2, 3]
        
        def _get_step(table, pass_num):
            """Get value from escalation table, clamping to last entry."""
            idx = min(pass_num, len(table) - 1)
            return table[idx]
        
        # ── Pass 1: Full-speed initial translation ──
        results = self.translate_texts(texts, progress_callback)
        
        # ── Retry loop: keep going until nothing left ──
        retry_pass = 0
        prev_failed_count = None  # Track if we're making progress
        stall_count = 0           # How many passes with no improvement
        
        while not self._cancel_requested:
            # Scan for remaining Chinese
            failed_indices = []
            for i, result in enumerate(results):
                if result and is_chinese_fn(result):
                    chinese_count = count_chinese_fn(result)
                    if chinese_count > 5:
                        failed_indices.append(i)
            
            if not failed_indices:
                break  # 🎉 Everything translated

            if retry_pass >= max_retry_passes:
                print(f"\n  ⚠ {len(failed_indices)} segment(s) still contain Chinese after "
                      f"{max_retry_passes} retry passes. Keeping best available text and proceeding.")
                break  # Don't block forever on stubborn segments
            
            # Stall detection: give up after 3 consecutive passes with no progress.
            # These segments are almost always ones the API consistently returns
            # with Chinese still in them (names, terms) - more retries won't help.
            if prev_failed_count is not None and len(failed_indices) >= prev_failed_count:
                stall_count += 1
                if stall_count >= 3:
                    print(f"\n  ⚠ No progress on {len(failed_indices)} segment(s) for "
                          f"{stall_count} passes. Keeping best available text and proceeding.")
                    break
            else:
                stall_count = 0
            prev_failed_count = len(failed_indices)
            
            retry_pass += 1
            
            with self.stats_lock:
                self.stats['retry_passes'] += 1
            
            # ── Smart delay: pick settings for this pass ──
            workers_cap  = _get_step(WORKER_STEPS, retry_pass)
            interval     = _get_step(INTERVAL_STEPS, retry_pass)
            cooldown     = _get_step(COOLDOWN_STEPS, retry_pass)
            extra_retry  = _get_step(EXTRA_RETRIES, retry_pass)
            
            # Resolve actual worker count
            retry_workers = min(
                workers_cap if workers_cap > 0 else self.max_workers,
                len(failed_indices)
            )
            
            # ── Log & callback ──
            print(f"\n  ⟳ Retry pass {retry_pass}: {len(failed_indices)} segments remaining "
                  f"(workers={retry_workers}, interval={interval:.1f}s, "
                  f"cooldown={cooldown}s, retries={self.max_retries + extra_retry})")
            
            if pass_callback:
                pass_callback(retry_pass, len(failed_indices), len(texts), cooldown)
            
            # ── Cooldown between passes ──
            if cooldown > 0:
                print(f"  ⏳ Cooling down for {cooldown}s before retry...")
                # Sleep in 1s chunks so cancellation is responsive
                for _ in range(cooldown):
                    if self._cancel_requested:
                        break
                    time.sleep(1)
            
            if self._cancel_requested:
                break
            
            # ── Clear caches for failed texts (memory + persistent) ──
            with self.cache_lock:
                for i in failed_indices:
                    cache_key = texts[i].strip()
                    self.cache.pop(cache_key, None)
            if self.persistent_cache is not None:
                for i in failed_indices:
                    self.persistent_cache.delete_translation(texts[i].strip(), self.backend)
            
            # ── Apply retry settings ──
            old_interval = self.request_interval
            old_max_retries = self.max_retries
            self.request_interval = max(interval, old_interval)
            self.max_retries = old_max_retries + extra_retry
            
            # Reset progress
            self.total = len(failed_indices)
            self.completed = 0
            
            # ── Translate failed texts ──
            failed_texts = [texts[i] for i in failed_indices]
            retry_results = [''] * len(failed_texts)
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=retry_workers) as executor:
                futures = {
                    executor.submit(self._translate_single, text, idx): idx
                    for idx, text in enumerate(failed_texts)
                }
                for future in concurrent.futures.as_completed(futures):
                    if self._cancel_requested:
                        break
                    try:
                        idx, translated = future.result()
                        retry_results[idx] = translated
                    except Exception:
                        pass
            
            # ── Apply improved translations ──
            # Accept any result with less Chinese than what we currently have.
            # Partially translated text (e.g. an English sentence keeping a
            # Chinese name) counts as progress instead of being discarded,
            # which previously caused endless retry loops.
            improved = 0
            for j, i in enumerate(failed_indices):
                translated = retry_results[j]
                if not translated:
                    continue
                if count_chinese_fn(translated) < count_chinese_fn(results[i]):
                    results[i] = translated
                    improved += 1
            
            # ── Restore original settings ──
            self.request_interval = old_interval
            self.max_retries = old_max_retries
            
            print(f"  ✓ Pass {retry_pass} done: {improved}/{len(failed_indices)} newly translated")
        
        # Final summary
        final_failed = sum(
            1 for i, r in enumerate(results)
            if r and is_chinese_fn(r) and count_chinese_fn(r) > 5
        )
        if final_failed == 0:
            print(f"\n  ✅ All {len(texts)} segments translated successfully "
                  f"({retry_pass} retry pass{'es' if retry_pass != 1 else ''})")
        elif self._cancel_requested:
            print(f"\n  ⚠ Translation cancelled with {final_failed} segments remaining")
        else:
            print(f"\n  ⚠ Proceeding with {final_failed} segment(s) still containing Chinese "
                  f"after {retry_pass} retry pass{'es' if retry_pass != 1 else ''}")
        
        return results
    
    def translate_text(self, text: str) -> str:
        """Translate a single text (convenience method)."""
        results = self.translate_texts([text])
        return results[0] if results else text
    
    @staticmethod
    def _contains_chinese(text: str) -> bool:
        """Check if text contains Chinese characters."""
        if not text:
            return False
        return bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))
    
    @staticmethod
    def _count_chinese(text: str) -> int:
        """Count Chinese characters in text."""
        if not text:
            return 0
        return len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))
    
    @staticmethod
    def is_chinese(text: str) -> bool:
        """Check if text contains significant Chinese characters."""
        if not text:
            return False
        chinese_count = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))
        return chinese_count > len(text) * 0.1  # More than 10% Chinese
    
    def get_stats(self) -> Dict:
        """Get translation statistics."""
        return self.stats.copy()
    
    def reset_stats(self):
        """Reset statistics."""
        self.stats = {
            'requests': 0,
            'paragraphs_translated': 0,
            'characters_translated': 0,
            'cache_hits': 0,
            'errors': 0,
            'retries': 0,
            'retry_passes': 0,
        }
        self.failed_texts.clear()
    
    def clear_cache(self):
        """Clear the translation cache."""
        with self.cache_lock:
            self.cache.clear()
