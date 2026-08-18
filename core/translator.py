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
connections are reused. On Windows and macOS, translate sessions prefer IPv4 —
broken AAAA routes otherwise add multi-second stalls per call. The hardcoded
Google endpoint skips per-request DNS in the SSRF layer (redirect hops still
checked).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import requests
import time
import threading
import concurrent.futures
from typing import List, Tuple, Dict, Optional, Callable, Any

# Windows and macOS often have broken/slow IPv6; urllib3 tries AAAA first and
# burns the connect timeout per address. Prefer IPv4 for requests fallback.
if sys.platform in ("win32", "darwin"):
    try:
        import urllib3.util.connection as _urllib3_conn
        _urllib3_conn.HAS_IPV6 = False
    except Exception:
        pass

# libcurl CURL_IPRESOLVE_V4 — used when curl_cffi is available.
_CURL_IPRESOLVE_V4 = 1


_gpu_lock = threading.Lock()
_gpu_cached: Optional[bool] = None
_gpu_logged = False


def _nvidia_smi_cmd() -> Optional[str]:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if sys.platform == "win32":
        for path in (
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ):
            if os.path.isfile(path):
                return path
    return None


def ollama_gpu_available() -> bool:
    """
    True if a local GPU Ollama can use is present (NVIDIA, ROCm, or Apple Metal).
    Cached. HUAEPUB_OLLAMA_GPU=0|1 overrides.
    """
    global _gpu_cached
    env = (os.environ.get("HUAEPUB_OLLAMA_GPU") or "").strip().lower()
    if env in ("0", "false", "cpu", "no"):
        return False
    if env in ("1", "true", "gpu", "yes"):
        return True
    with _gpu_lock:
        if _gpu_cached is not None:
            return _gpu_cached
        _gpu_cached = _detect_ollama_gpu()
        return _gpu_cached


def _detect_ollama_gpu() -> bool:
    smi = _nvidia_smi_cmd()
    if smi:
        try:
            kwargs: Dict[str, Any] = {
                "capture_output": True,
                "timeout": 2,
                "text": True,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run([smi, "-L"], **kwargs)
            out = (result.stdout or "") + (result.stderr or "")
            if result.returncode == 0 and "GPU" in out.upper():
                return True
        except Exception:
            pass
    if shutil.which("rocm-smi"):
        try:
            result = subprocess.run(
                ["rocm-smi"], capture_output=True, timeout=2, text=True,
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass
    if sys.platform == "darwin":
        return True
    return False


def ollama_infer_options() -> Dict[str, Any]:
    """num_gpu / num_thread: all layers on GPU when present, else CPU."""
    global _gpu_logged
    threads = max(2, min(16, int(os.cpu_count() or 4)))
    if ollama_gpu_available():
        opts: Dict[str, Any] = {"num_gpu": 99, "num_thread": min(8, threads)}
        device = "GPU"
    else:
        opts = {"num_gpu": 0, "num_thread": threads}
        device = "CPU"
    if not _gpu_logged:
        _gpu_logged = True
        print(f"Ollama inference: {device}")
    return opts


class GoogleTranslator:
    """
    Concurrent translator with retry logic and multi-pass retry.
    
    Backends:
    - 'google' (default): the free Google Translate endpoint
    - 'libretranslate': a LibreTranslate server (public instance or self-hosted),
      configured via libretranslate_url
    - 'ollama': a local Ollama instance (loopback only), configured via
      ollama_url + ollama_model. Workers are capped; timeouts are longer.

    
    Optionally uses a persistent cache (core.cache.NovelCache) so repeated
    runs and recurring phrases across novels cost zero API requests.
    """
    
    ENDPOINT = 'https://translate.googleapis.com/translate_a/single'
    USER_AGENT = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    )
    _VALID_BACKENDS = ('google', 'libretranslate', 'ollama')
    _OLLAMA_SYSTEM = (
        "You are a literary translator. Translate the user's Chinese web-novel "
        "text into fluent natural English. Keep names and terms consistent. "
        "Do not add notes, titles, or commentary. Output only the translation."
    )
    _OLLAMA_POLISH_SYSTEM = (
        "You copy-edit machine-translated English from a web novel. "
        "Fix grammar and awkward phrasing only. Keep meaning, names, numbers. "
        "Only output segments that need changes, each starting with <<<N>>>. "
        "Omit fluent segments. If nothing needs changing, output NONE. "
        "Do not add titles, notes, or commentary."
    )
    # Awkward-only batches stay small so the 4070 spends time generating
    # corrections, not rewriting the whole book.
    POLISH_BATCH_CHARS = 8000
    POLISH_MIN_CHARS = 20
    DEFAULT_OLLAMA_URL = 'http://127.0.0.1:11434'
    # Qwen2.5 3B: Apache-2.0, strong zh→en, ~2 GB, fine on CPU.
    # Untagged "qwen2.5" often resolves to 7B+; never auto-pull.
    DEFAULT_OLLAMA_MODEL = 'qwen2.5:3b'
    
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
        ollama_url: str = 'http://127.0.0.1:11434',
        ollama_model: str = 'qwen2.5:3b',
        persistent_cache=None,
    ):
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.max_workers = max_workers
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.request_interval = request_interval
        self.backend = backend if backend in self._VALID_BACKENDS else 'google'
        self.persistent_cache = persistent_cache
        self.ollama_model = (ollama_model or '').strip() or self.DEFAULT_OLLAMA_MODEL
        if self.backend == 'ollama':
            # Local GPU/CPU inference does not benefit from 200 workers
            self.max_workers = max(1, min(int(max_workers or 1), 8))
            if request_timeout <= 15:
                request_timeout = 180
            self.request_timeout = request_timeout
        # (connect, read) — short connect avoids long IPv6 black-hole waits
        self._timeout = (min(5, request_timeout), request_timeout)
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
        if self.backend == 'ollama':
            from core.security import UnsafeURLError, validate_ollama_url
            try:
                self.ollama_url = validate_ollama_url(
                    ollama_url or self.DEFAULT_OLLAMA_URL
                )
            except UnsafeURLError as e:
                raise ValueError(f"Invalid Ollama URL: {e}") from e
        else:
            self.ollama_url = (ollama_url or self.DEFAULT_OLLAMA_URL).rstrip('/')
        
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
            curl_kw: Dict[str, Any] = {"impersonate": "chrome120"}
            if sys.platform in ("win32", "darwin"):
                try:
                    from curl_cffi import CurlOpt
                    curl_kw["curl_options"] = {
                        CurlOpt.IPRESOLVE: _CURL_IPRESOLVE_V4
                    }
                except Exception:
                    pass
            try:
                sess = CurlSession(**curl_kw)
            except TypeError:
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
        from core.security import safe_http_request
        session = self._get_http_session()
        headers = {'User-Agent': self.USER_AGENT}

        # Use GET for short texts, POST for long texts (redirect-safe)
        if len(text) <= 1800:
            response = safe_http_request(
                session, "GET", self.ENDPOINT,
                allow_http=False, resolve_dns=False,
                params=params, headers=headers,
                timeout=self._timeout,
            )
        else:
            response = safe_http_request(
                session, "POST", self.ENDPOINT,
                allow_http=False, resolve_dns=False,
                data=params, headers=headers,
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
        from core.security import UnsafeURLError, safe_http_request
        # LibreTranslate uses plain ISO codes ('zh', not 'zh-CN')
        source = self.source_lang.split('-')[0]
        session = self._get_http_session()
        try:
            response = safe_http_request(
                session,
                "POST",
                f'{self.libretranslate_url}/translate',
                allow_http=True,
                timeout=self._timeout,
                json={
                    'q': text,
                    'source': source,
                    'target': self.target_lang,
                    'format': 'text',
                },
                headers={'User-Agent': self.USER_AGENT},
            )
        except UnsafeURLError as e:
            raise ValueError(f"Blocked LibreTranslate URL: {e}") from e
        response.raise_for_status()
        return response.json().get('translatedText', '')

    def _cache_backend(self) -> str:
        """Persistent-cache key. Ollama is namespaced by model."""
        if self.backend == 'ollama':
            return f'ollama:{self.ollama_model}'
        return self.backend

    def _polish_cache_backend(self) -> str:
        return 'span-polish:v2'

    def _request_ollama(
        self,
        text: str,
        *,
        system: Optional[str] = None,
        timeout: Optional[Tuple[int, int]] = None,
        temperature: float = 0.2,
        extra_options: Optional[Dict[str, Any]] = None,
        allow_empty: bool = False,
        think: Optional[bool] = None,
    ) -> str:
        """Chat with a local Ollama instance (loopback only)."""
        from core.security import UnsafeURLError, safe_http_request, validate_ollama_url
        if not self.ollama_model:
            raise ValueError(
                "Ollama model is empty. Set a model (e.g. qwen2.5:3b) and run: "
                "ollama pull qwen2.5:3b"
            )
        try:
            base = validate_ollama_url(self.ollama_url or self.DEFAULT_OLLAMA_URL)
        except UnsafeURLError as e:
            raise ValueError(f"Blocked Ollama URL: {e}") from e
        session = self._get_http_session()
        options: Dict[str, Any] = {'temperature': temperature}
        options.update(ollama_infer_options())
        if extra_options:
            options.update(extra_options)
        payload: Dict[str, Any] = {
            'model': self.ollama_model,
            'stream': False,
            'keep_alive': '10m',
            'messages': [
                {'role': 'system', 'content': system or self._OLLAMA_SYSTEM},
                {'role': 'user', 'content': text},
            ],
            'options': options,
        }
        if think is not None:
            payload['think'] = think
        try:
            response = safe_http_request(
                session,
                "POST",
                f'{base}/api/chat',
                allow_http=True,
                allow_loopback=True,
                timeout=timeout or self._timeout,
                json=payload,
                headers={
                    'User-Agent': self.USER_AGENT,
                    'Content-Type': 'application/json',
                },
            )
        except UnsafeURLError as e:
            raise ValueError(f"Blocked Ollama URL: {e}") from e
        except Exception as e:
            err = str(e).lower()
            if any(s in err for s in ('connection', 'refused', '10061', 'timed out', 'timeout')):
                raise ValueError(
                    "Ollama is not running. Install from https://ollama.com "
                    f"then run: ollama pull {self.ollama_model}"
                ) from e
            raise
        if getattr(response, 'status_code', 0) == 404:
            installed = list_ollama_models(base, timeout=1.5)
            hint = (
                f"Installed: {', '.join(installed)}. Pick one in Translator, or run: "
                f"ollama pull {self.ollama_model}"
                if installed
                else f"Run: ollama pull {self.ollama_model}"
            )
            raise ValueError(
                f"Ollama model '{self.ollama_model}' is not installed. {hint}"
            )
        response.raise_for_status()
        data = response.json() if hasattr(response, 'json') else {}
        if isinstance(data, dict) and data.get('error'):
            err = str(data['error'])
            if 'not found' in err.lower():
                installed = list_ollama_models(base, timeout=1.5)
                hint = (
                    f"Installed: {', '.join(installed)}. Pick one in Translator, or run: "
                    f"ollama pull {self.ollama_model}"
                    if installed
                    else f"Run: ollama pull {self.ollama_model}"
                )
                raise ValueError(
                    f"Ollama model '{self.ollama_model}' is not installed. {hint}"
                )
            raise ValueError(f"Ollama: {err}")
        msg = ((data.get('message') or {}).get('content') or '').strip()
        if not msg:
            if allow_empty:
                return ""
            raise ValueError("Ollama returned an empty translation")
        return msg
    
    def _request_translation(self, text: str) -> str:
        if self.backend == 'libretranslate':
            return self._request_libretranslate(text)
        if self.backend == 'ollama':
            return self._request_ollama(text)
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
            cached = self.persistent_cache.get_translation(cache_key, self._cache_backend())
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
                        self.persistent_cache.put_translation(cache_key, translated, self._cache_backend())
                    
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
                    self.persistent_cache.delete_translation(texts[i].strip(), self._cache_backend())
            
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
            print(f"\n  All {len(texts)} segments translated successfully "
                  f"({retry_pass} retry pass{'es' if retry_pass != 1 else ''})")
        elif self._cancel_requested:
            print(f"\n  Translation cancelled with {final_failed} segments remaining")
        else:
            print(f"\n  Proceeding with {final_failed} segment(s) still containing Chinese "
                  f"after {retry_pass} retry pass{'es' if retry_pass != 1 else ''}")
        
        return results
    
    def translate_text(self, text: str) -> str:
        """Translate a single text (convenience method)."""
        results = self.translate_texts([text])
        return results[0] if results else text

    def polish_texts(
        self,
        texts: List[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
        max_chars: int = POLISH_BATCH_CHARS,
    ) -> List[str]:
        """
        Copy-edit already-English machine translation with the local
        KEEP/REPLACE polisher (llama.cpp / vLLM / Ollama). On failure,
        keeps the original English.
        """
        del max_chars
        if not texts:
            return []
        from core.local_polish import polish_paragraphs, wants_polish

        results = list(texts)
        cache_backend = self._polish_cache_backend()
        pending: List[int] = []
        for i, text in enumerate(texts):
            if self._cancel_requested:
                return results
            raw = (text or '').strip()
            if not raw or self.is_chinese(raw):
                continue
            if not wants_polish(raw):
                continue
            with self.cache_lock:
                cached = self.cache.get(f'{cache_backend}\0{raw}')
            if cached:
                results[i] = cached
                with self.stats_lock:
                    self.stats['cache_hits'] += 1
                continue
            if self.persistent_cache is not None:
                cached = self.persistent_cache.get_translation(raw, cache_backend)
                if cached:
                    with self.cache_lock:
                        self.cache[f'{cache_backend}\0{raw}'] = cached
                    results[i] = cached
                    with self.stats_lock:
                        self.stats['cache_hits'] += 1
                    continue
            pending.append(i)

        skipped = len(texts) - len(pending)
        print(
            f"  Local polish: {len(pending)} segments to copy-edit, "
            f"{skipped} skipped (short/title/fluent/Chinese/cached)"
        )
        if not pending:
            if progress_callback:
                progress_callback(1, 1)
            return results

        originals = [texts[i] for i in pending]

        def on_progress(completed: int, total: int) -> None:
            if progress_callback:
                progress_callback(completed, total)

        try:
            polished, model_id = polish_paragraphs(
                originals,
                progress=on_progress,
                cancelled=lambda: self._cancel_requested,
                log=print,
            )
        except Exception as e:
            print(f"  Local polish failed ({e}); keeping Google English.")
            if progress_callback:
                progress_callback(1, 1)
            return results

        if model_id:
            print(f"  Local polish model: {model_id}")
        edited = 0
        for i, out in zip(pending, polished):
            src = (texts[i] or '').strip()
            results[i] = out
            if out != texts[i]:
                edited += 1
                with self.cache_lock:
                    self.cache[f'{cache_backend}\0{src}'] = out
                if self.persistent_cache is not None:
                    self.persistent_cache.put_translation(src, out, cache_backend)
            with self.stats_lock:
                self.stats['requests'] += 1
        print(f"  Local polish: {edited} edited, {len(pending) - edited} kept original")
        return results
    
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


_SEGMENT_MARK = re.compile(r'<<<(\d+)>>>\s*')
_POLISH_SKIP_TOKENS = frozenset({
    'skip', 'unchanged', 'ok', 'same', '[skip]', 'skip.', 'none',
})
_POLISH_SV = re.compile(
    r"\b(?:he|she|it)\s+(?:go|have|do|don't|want|say|get|come|make|take|give)\b"
    r"|\b(?:they|we|you)\s+(?:goes|has|was)\b"
    r"|\b(?:he|she|it)\s+don't\b",
    re.IGNORECASE,
)
_POLISH_DUP = re.compile(r'\b(\w{3,})\s+\1\b', re.IGNORECASE)
_POLISH_CJK = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')
_POLISH_END = re.compile(r'[.!?…]"?\s*$')


def should_polish_english(text: str, *, min_chars: int = 20) -> bool:
    """
    True if this English looks like it still needs a copy-edit.
    Fluent, punctuated Google output is skipped so the GPU is not asked
    to rewrite the whole book.
    """
    raw = (text or '').strip()
    if not raw:
        return False
    if ' ' not in raw and len(raw) < 80:
        return False
    if len(raw) < max(1, int(min_chars)):
        return False
    if _POLISH_CJK.search(raw):
        return True
    if _POLISH_DUP.search(raw):
        return True
    if _POLISH_SV.search(raw):
        return True
    if len(raw) >= 60 and not _POLISH_END.search(raw):
        return True
    periods = raw.count('.') + raw.count('!') + raw.count('?')
    if len(raw) >= 100 and raw.count(',') >= 3 and periods == 0:
        return True
    if len(raw) >= 40 and raw[0].islower():
        return True
    return False


def is_polish_skip(text: str) -> bool:
    token = (text or '').strip().strip('"').strip("'").lower()
    return token in _POLISH_SKIP_TOKENS


def pack_numbered_segments(texts: List[str]) -> str:
    """Join segments with <<<N>>> markers for one Ollama polish request."""
    parts = [f"<<<{i}>>>\n{(t or '').strip()}" for i, t in enumerate(texts, 1)]
    return "\n".join(parts)


def unpack_numbered_segments(blob: str, expected: int) -> Optional[List[str]]:
    """
    Parse <<<N>>> segments back into a list. None if the model dropped,
    reordered, or skipped markers.
    """
    sparse = unpack_sparse_segments(blob, expected)
    if sparse is None or len(sparse) != expected:
        return None
    return [sparse[i] for i in range(1, expected + 1)]


def unpack_sparse_segments(blob: str, expected: int) -> Optional[Dict[int, str]]:
    """
    Map 1-based segment index → text. Missing numbers are omitted (keep
    Google wording). Empty / NONE → {}. Garbage with no markers → None.
    """
    raw = (blob or '').strip()
    if not raw or raw.upper() in ('NONE', 'SKIP', 'OK'):
        return {}
    if expected <= 0:
        return None
    matches = list(_SEGMENT_MARK.finditer(blob))
    if not matches:
        return None
    out: Dict[int, str] = {}
    for i, m in enumerate(matches):
        num = int(m.group(1))
        if num < 1 or num > expected:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(blob)
        piece = blob[start:end].strip()
        if piece:
            out[num] = piece
    return out


def probe_ollama(
    ollama_url: str = GoogleTranslator.DEFAULT_OLLAMA_URL,
    timeout: float = 1.5,
) -> Optional[List[str]]:
    """
    Models already pulled. None if Ollama is not reachable; [] if it is
    running but has no models. Never pulls.
    """
    from core.security import UnsafeURLError, safe_http_request, validate_ollama_url
    try:
        base = validate_ollama_url(ollama_url or GoogleTranslator.DEFAULT_OLLAMA_URL)
    except UnsafeURLError:
        return None
    session = requests.Session()
    try:
        response = safe_http_request(
            session,
            "GET",
            f"{base}/api/tags",
            allow_http=True,
            allow_loopback=True,
            timeout=timeout,
            headers={"User-Agent": GoogleTranslator.USER_AGENT},
        )
        response.raise_for_status()
        data = response.json() if hasattr(response, "json") else {}
    except Exception:
        return None
    names: List[str] = []
    for item in (data.get("models") or []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or item.get("model") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _is_windows() -> bool:
    """Isolated so tests can fake Windows without patching os.name (Path breaks)."""
    import os
    return os.name == "nt"


def ollama_is_installed() -> bool:
    """True if the Ollama app/CLI looks present (not whether it is running)."""
    import os
    import shutil
    from pathlib import Path

    if shutil.which("ollama"):
        return True
    candidates: List[Path] = []
    if _is_windows():
        local = os.environ.get("LOCALAPPDATA") or ""
        pf = os.environ.get("PROGRAMFILES") or r"C:\Program Files"
        pf86 = os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)"
        if local:
            candidates.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
        candidates.append(Path(pf) / "Ollama" / "ollama.exe")
        candidates.append(Path(pf86) / "Ollama" / "ollama.exe")
    else:
        home = Path.home()
        candidates.extend([
            Path("/usr/local/bin/ollama"),
            Path("/usr/bin/ollama"),
            Path("/opt/homebrew/bin/ollama"),
            home / ".local" / "bin" / "ollama",
            Path("/Applications/Ollama.app"),
            home / "Applications" / "Ollama.app",
        ])
    return any(p.exists() for p in candidates)


def list_ollama_models(
    ollama_url: str = GoogleTranslator.DEFAULT_OLLAMA_URL,
    timeout: float = 1.5,
) -> List[str]:
    """Like probe_ollama, but [] when Ollama is down (never None)."""
    found = probe_ollama(ollama_url, timeout=timeout)
    return [] if found is None else found


def ollama_model_installed(name: str, installed: List[str]) -> bool:
    """True if name is pulled. qwen2.5:3b does not match qwen2.5:7b."""
    want = (name or "").strip()
    if not want or not installed:
        return False
    for have in installed:
        if have == want or have.startswith(want + ":"):
            return True
    return False


def pull_ollama_model(
    model: str,
    ollama_url: str = GoogleTranslator.DEFAULT_OLLAMA_URL,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> None:
    """
    Stream-pull a model into local Ollama. Loopback only. Raises on failure
    or cancel. progress_callback(percent_or_-1, status). Uses safe_http_request
    so redirect hops are re-validated (still loopback-only).
    """
    from core.security import UnsafeURLError, safe_http_request, validate_ollama_url

    name = (model or "").strip()
    if not name:
        raise ValueError("No Ollama model name to download")
    try:
        base = validate_ollama_url(ollama_url or GoogleTranslator.DEFAULT_OLLAMA_URL)
    except UnsafeURLError as e:
        raise ValueError(f"Invalid Ollama URL: {e}") from e

    session = requests.Session()
    try:
        response = safe_http_request(
            session,
            "POST",
            f"{base}/api/pull",
            allow_http=True,
            allow_loopback=True,
            timeout=(10, 3600),
            json={"model": name, "name": name, "stream": True},
            stream=True,
            headers={
                "User-Agent": GoogleTranslator.USER_AGENT,
                "Content-Type": "application/json",
            },
        )
    except UnsafeURLError as e:
        raise ValueError(f"Blocked Ollama URL: {e}") from e
    except Exception as e:
        err = str(e).lower()
        if any(s in err for s in ("connection", "refused", "10061")):
            raise ValueError(
                "Ollama is not running. Start Ollama, then try again."
            ) from e
        raise
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code == 404:
        raise ValueError(f"Ollama does not know how to pull '{name}'")
    response.raise_for_status()

    for raw in response.iter_lines():
        if cancel_check and cancel_check():
            try:
                response.close()
            except Exception:
                pass
            raise ValueError("Download cancelled")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("error"):
            raise ValueError(str(data["error"]))
        status = str(data.get("status") or "")
        total = int(data.get("total") or 0)
        completed = int(data.get("completed") or 0)
        if progress_callback:
            if total > 0:
                pct = min(100, int(completed * 100 / total))
            elif status == "success":
                pct = 100
            else:
                pct = -1
            progress_callback(pct, status)
        if status == "success":
            return
    if progress_callback:
        progress_callback(100, "success")


def resolve_ollama_model(preferred: str, installed: List[str]) -> str:
    """
    Pick a model that is actually installed.
    Exact match, then same family (qwen2.5:3b → qwen2.5:7b), else first installed.
    If nothing is installed, keep the preferred name so the user can still type it.
    """
    pref = (preferred or "").strip()
    if not installed:
        return pref
    if pref in installed:
        return pref
    pref_base = pref.split(":")[0] if pref else ""
    if pref_base:
        for name in installed:
            if name.split(":")[0] == pref_base:
                return name
    return installed[0]
