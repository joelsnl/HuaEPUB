# Author: joelsnl and Anthropic Claude
"""
Translation backends: Google (free), LibreTranslate, local Ollama,
and optional CTranslate2 (via NovelTranslator). Glossary protect/restore
lives on NovelTranslator so names and ranks are not translated literally.

Persistent retry (Google / LibreTranslate):
- Bounded multi-pass retry — hard cap on passes (default 8); never loops forever
- Partial improvements are kept so the EPUB can always be built
- Unofficial gtx is per-IP. 200 parallel GETs 429 until nothing translates,
  then retry shows ~15k still Chinese. Google uses a throttle: start at 8
  in flight (UI 200 is the ceiling), cool all new requests on 429, climb
  +1 per success. Do not go back to “that worker sleeps; the other 199 keep
  hammering.”
- Stall detection: if no progress for 3+ passes, later passes use a longer cooldown
- Failed cache entries are dropped before each retry so fresh requests are made
- Persistent cache writes are batched (commit every 200 / end of pass)
- Cancellable via cancel() — abort during translation writes no EPUB

HTTP: each worker thread keeps its own Session (curl_cffi when available) so
TLS is reused without sharing a Session across threads. On Windows and macOS,
translate sessions prefer IPv4 — broken AAAA routes otherwise add multi-second
stalls per call. The hardcoded Google endpoint skips per-request DNS in the
SSRF layer (redirect hops still checked). Google is one paragraph per
request. Identical strings share one GET; nodes with no CJK are not sent.
LibreTranslate may still pack. Failed or echoed Chinese is never stored
as a cache hit. Google prefetch does not hit gtx.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import time
import threading
import concurrent.futures
from html import unescape
from typing import List, Tuple, Dict, Optional, Callable, Any

from core.gtx_throttle import (
    GtxThrottle,
    RateLimitedError,
    jittered_backoff_seconds,
    parse_retry_after,
)
from core.parser import CHROME_UA

DEFAULT_GOOGLE_WORKERS = 200
MAX_PACKED_WORKERS = 200
MAX_SESSION_POOL = 16
GOOGLE_FAMILY_BACKENDS = frozenset({"google", "google_html", "google_gtx"})
THROTTLED_BACKENDS = frozenset({"google", "google_html", "google_gtx", "microsoft"})
# Widget keys shipped in Google's Translate Element / the Calibre plugin.
# Not a user Cloud Translation secret.
_GOOGLE_PA_KEY = "AIzaSyDLEeFI5OtFBwYBIoK_jj5m32rZK5CkCXA"
_GOOGLE_HTML_KEY = "AIzaSyATBXajvzQLTDHEQbcpq0Ihe0vWDHmO520"
_GTX_LOG_LOCK = threading.Lock()
_GTX_LOG_AT = 0.0
_GTX_HIDDEN = 0


_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def is_usable_translation(source: str, translated: str) -> bool:
    """
    True when dest looks like a real MT result, not an echo of the source.

    Leftover names (a few CJK chars) still count as usable. A whole chapter
    that came back as Chinese must not be cached or treated as done.
    """
    src = (source or "").strip()
    out = (translated or "").strip()
    if not out:
        return False
    src_cjk = len(_CJK_RE.findall(src))
    if src_cjk <= 5:
        return True
    if out == src:
        return False
    out_cjk = len(_CJK_RE.findall(out))
    if out_cjk <= 5:
        return True
    if out_cjk >= max(src_cjk * 0.5, 20):
        return False
    return True


def needs_gtx_request(text: str) -> bool:
    """True when unofficial gtx should see this string (has CJK ideographs)."""
    return bool((text or "").strip() and _CJK_RE.search(text))


class _GtxCancelled(Exception):
    """Worker gave up waiting for a gtx slot because cancel was requested."""


from core.ollama_setup import (
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_URL,
    list_ollama_models,
    ollama_gpu_available,
    ollama_infer_options,
    ollama_is_installed,
    ollama_model_installed,
    probe_ollama,
    pull_ollama_model,
    resolve_ollama_model,
)

# Windows and macOS often have broken/slow IPv6; urllib3 tries AAAA first and
# burns the connect timeout per address. Prefer IPv4 for requests fallback.
if sys.platform in ("win32", "darwin"):
    try:
        import urllib3.util.connection as _urllib3_conn
        _urllib3_conn.HAS_IPV6 = False
    except Exception:
        pass


class GoogleTranslator:
    """
    Concurrent translator with retry logic and multi-pass retry.
    
    Backends:
    - 'google' (default): Google (Free) New — translate-pa v1/translate
    - 'google_html': Google (Free) HTML — translate-pa v1/translateHtml
    - 'google_gtx': Google (Free) Old — translate_a/single?client=gtx
    - 'microsoft': Microsoft Edge (Free)
    - 'libretranslate': a LibreTranslate server
    - 'ollama': local Ollama (loopback only)

    
    Optionally uses a persistent cache (core.cache.NovelCache) so repeated
    runs and recurring phrases across novels cost zero API requests.
    """
    
    ENDPOINT = 'https://translate.googleapis.com/translate_a/single'
    ENDPOINT_PA = 'https://translate-pa.googleapis.com/v1/translate'
    ENDPOINT_HTML = 'https://translate-pa.googleapis.com/v1/translateHtml'
    ENDPOINT_EDGE_AUTH = 'https://edge.microsoft.com/translate/auth'
    ENDPOINT_EDGE = 'https://api-edge.cognitive.microsofttranslator.com/translate'
    USER_AGENT = CHROME_UA
    _VALID_BACKENDS = (
        'google', 'google_html', 'google_gtx', 'microsoft',
        'libretranslate', 'ollama', 'ctranslate2',
    )
    _OLLAMA_SYSTEM = (
        "You are a literary translator. Translate the user's Chinese web-novel "
        "text into fluent natural English. Keep names and terms consistent. "
        "Do not add notes, titles, or commentary. Output only the translation."
    )
    # Awkward-only batches stay small so the 4070 spends time generating
    # corrections, not rewriting the whole book.
    POLISH_BATCH_CHARS = 8000
    DEFAULT_OLLAMA_URL = DEFAULT_OLLAMA_URL
    # Qwen2.5 3B: Apache-2.0, strong zh→en, ~2 GB, fine on CPU.
    # Untagged "qwen2.5" often resolves to 7B+; never auto-pull.
    DEFAULT_OLLAMA_MODEL = DEFAULT_OLLAMA_MODEL
    
    def __init__(
        self,
        source_lang: str = 'zh-CN',
        target_lang: str = 'en',
        max_workers: int = DEFAULT_GOOGLE_WORKERS,
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
            # Local GPU/CPU inference does not benefit from many workers
            self.max_workers = max(1, min(int(max_workers or 1), 8))
            if request_timeout <= 15:
                request_timeout = 180
            self.request_timeout = request_timeout
        elif self.backend == 'ctranslate2':
            self.max_workers = max(1, min(int(max_workers or 1), 4))
        elif self.backend in ('google', 'google_html', 'google_gtx', 'microsoft', 'libretranslate'):
            self.max_workers = max(
                1, min(int(max_workers or DEFAULT_GOOGLE_WORKERS), MAX_PACKED_WORKERS)
            )
        self._configured_workers = self.max_workers
        self._adapter_pool_size = max(8, min(int(self.max_workers or 1), MAX_SESSION_POOL))
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
            'throttles': 0,
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
        self._in_flight = 0
        self._unique_requests = 0
        self._progress_emit_at = 0.0
        self._progress_source_index = -1
        self._active_source_indices: set[int] = set()
        self.progress_callback: Optional[Callable[[int, int], None]] = None
        
        # Failed texts for reporting
        self.failed_texts: List[Tuple[int, str]] = []
        self.failed_lock = threading.Lock()
        
        # Control flag. DownloadControl.bind is optional — Cancel/Pause on
        # the GUI set flags on that object from the UI thread.
        self._cancel_requested = False
        self._control = None
        self._gtx: Optional[GtxThrottle] = None
        if self.backend in THROTTLED_BACKENDS:
            # Title fetch used max_workers=1, which made the cap 1 (ceiling 1).
            # Unofficial Google/Microsoft always start at 8 toward the UI ceiling.
            raw = int(self._configured_workers or DEFAULT_GOOGLE_WORKERS)
            ceiling = max(GtxThrottle.START_LIMIT, raw)
            self._gtx = GtxThrottle(
                ceiling,
                on_change=self._on_gtx_change,
            )
        self._edge_token = ""
        self._edge_token_exp = 0.0

    def bind_control(self, control: Any) -> None:
        """See Cancel/Pause on a DownloadControl (UI thread mutates it)."""
        self._control = control
        if control is not None:
            control.translator = self

    def cancel(self) -> None:
        """Request cancellation of ongoing translation."""
        self._cancel_requested = True
        gate = getattr(self, "_gtx", None)
        if gate is not None:
            gate.wake()

    def _on_gtx_change(self, current: int, _limit: int) -> None:
        self._in_flight = int(current)
        self._emit_progress(force=(int(current) <= 1))

    def _should_cancel(self) -> bool:
        if self._cancel_requested:
            return True
        ctrl = self._control
        if ctrl is not None and getattr(ctrl, "cancel_requested", False):
            self._cancel_requested = True
            return True
        return False

    def _wait_if_paused(self) -> None:
        """Block this worker while the download is paused. Cancel wins."""
        ctrl = self._control
        if ctrl is None:
            return
        while getattr(ctrl, "is_paused", False) and not self._should_cancel():
            time.sleep(0.2)
        if ctrl is not None and getattr(ctrl, "cancel_requested", False):
            self._cancel_requested = True
    
    def _make_http_session(self) -> Any:
        """One curl_cffi or requests session (keep-alive)."""
        from core.parser import create_http_session

        ipv4 = sys.platform in ("win32", "darwin")
        pool = max(8, int(self._adapter_pool_size or 8))
        return create_http_session(ipv4=ipv4, pool_size=pool)

    def _get_http_session(self) -> Any:
        """Thread-local Session. Sharing one Session across workers corrupts TLS."""
        sess = getattr(self._thread_local, "session", None)
        if sess is not None:
            return sess
        sess = self._make_http_session()
        self._thread_local.session = sess
        return sess

    def _note_throttle(self, _retry_after: float) -> None:
        """Log a 429. Google also cools new GETs via GtxThrottle."""
        global _GTX_LOG_AT, _GTX_HIDDEN
        log_line = ""
        with self.stats_lock:
            self.stats["throttles"] = int(self.stats.get("throttles", 0) or 0) + 1
            if getattr(self, "_in_prefetch", False):
                self._skip_prefetch_translate = True
        cap = ""
        gate = getattr(self, "_gtx", None)
        if gate is not None:
            cap = f"in-flight cap {gate.limit}, ceiling {gate.max_limit}"
        else:
            cap = f"workers={self.max_workers}"
        with _GTX_LOG_LOCK:
            hidden = int(_GTX_HIDDEN or 0) + 1
            now = time.monotonic()
            if now - float(_GTX_LOG_AT or 0.0) >= 15.0:
                extra = f" ×{hidden}" if hidden > 1 else ""
                log_line = (
                    f"  {self._throttle_label()} HTTP 429 (rate limited){extra} — "
                    f"pausing new requests ({cap})"
                )
                _GTX_LOG_AT = now
                _GTX_HIDDEN = 0
            else:
                _GTX_HIDDEN = hidden
        if log_line:
            print(log_line)

    def _throttle_label(self) -> str:
        if self.backend == "microsoft":
            return "Microsoft Edge"
        if self.backend == "google_html":
            return "Google HTML"
        if self.backend == "google_gtx":
            return "Google Old"
        return "Google"

    def _interruptible_sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            if self._should_cancel():
                return
            self._wait_if_paused()
            left = end - time.monotonic()
            if left <= 0:
                return
            before = time.monotonic()
            time.sleep(min(0.2, left))
            after = time.monotonic()
            if after < before + 0.01:
                # time.sleep was patched (tests); do not busy-spin.
                return

    def _raise_if_rate_limited(self, response: Any) -> None:
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 429:
            wait = parse_retry_after(response)
            raise RateLimitedError(wait)

    def _request_http(
        self,
        method: str,
        url: str,
        *,
        allow_http: bool = False,
        resolve_dns: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Session + UA + SSRF wrapper + 429/status. Caller parses the body."""
        from core.security import safe_http_request

        session = self._get_http_session()
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("User-Agent", self.USER_AGENT)
        kwargs.setdefault("timeout", self._timeout)
        response = safe_http_request(
            session,
            method,
            url,
            allow_http=allow_http,
            resolve_dns=resolve_dns,
            headers=headers,
            **kwargs,
        )
        self._raise_if_rate_limited(response)
        response.raise_for_status()
        return response

    # ------------------------------------------------------------------
    # Backend requests
    # ------------------------------------------------------------------

    def _request_google(self, text: str) -> str:
        """Translate via the selected free Google engine."""
        if self.backend == "google_html":
            return self._request_google_html(text)
        if self.backend == "google_gtx":
            return self._request_google_gtx(text)
        return self._request_google_pa(text)

    def _request_google_gtx(self, text: str) -> str:
        """Legacy translate_a/single?client=gtx (walled for many IPs since 2026)."""
        params = {
            'client': 'gtx',
            'sl': self.source_lang,
            'tl': self.target_lang,
            'dt': 't',
            'dj': '1',
            'q': text
        }
        if len(text) <= 1800:
            response = self._request_http(
                "GET", self.ENDPOINT, params=params,
            )
        else:
            response = self._request_http(
                "POST", self.ENDPOINT, data=params,
            )
        data = response.json()
        return ''.join(
            s.get('trans', '')
            for s in data.get('sentences', [])
            if 'trans' in s
        )

    def _request_google_pa(self, text: str) -> str:
        """Google (Free) New — same as Calibre Ebook Translator v2.4+."""
        params = {
            "params.client": "gtx",
            "query.source_language": self.source_lang or "zh-CN",
            "query.target_language": self.target_lang or "en",
            "query.display_language": "en-US",
            "data_types": "TRANSLATION",
            "key": _GOOGLE_PA_KEY,
            "query.text": text,
        }
        response = self._request_http(
            "GET", self.ENDPOINT_PA, params=params,
        )
        data = response.json() if hasattr(response, "json") else {}
        out = unescape(str((data or {}).get("translation") or "")).strip()
        if not out:
            raise ValueError("Empty Google (New) translation")
        return out

    def _request_google_html(self, text: str) -> str:
        """Google (Free) HTML — translateHtml widget endpoint."""
        escaped = (
            (text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        body = json.dumps(
            [[[escaped], self.source_lang or "zh-CN", self.target_lang or "en"], "wt_lib"],
            ensure_ascii=False,
        )
        response = self._request_http(
            "POST", self.ENDPOINT_HTML,
            data=body.encode("utf-8"),
            headers={
                "Content-Type": "application/json+protobuf",
                "X-Goog-Api-Key": _GOOGLE_HTML_KEY,
            },
        )
        data = response.json() if hasattr(response, "json") else None
        chunk = data[0][0] if isinstance(data, list) and data and data[0] else ""
        if isinstance(chunk, list):
            chunk = "".join(str(p) for p in chunk)
        out = unescape(str(chunk or "")).strip()
        if not out:
            raise ValueError("Empty Google (HTML) translation")
        return out

    def _microsoft_lang(self, code: str) -> str:
        raw = (code or "en").replace("_", "-")
        mapped = {
            "zh-cn": "zh-Hans",
            "zh": "zh-Hans",
            "zh-tw": "zh-Hant",
            "zh-hk": "zh-Hant",
        }
        return mapped.get(raw.lower(), raw)

    def _edge_auth_token(self) -> str:
        now = time.time()
        if self._edge_token and now < self._edge_token_exp - 60:
            return self._edge_token
        response = self._request_http("GET", self.ENDPOINT_EDGE_AUTH)
        token = (getattr(response, "text", None) or "").strip().strip('"')
        if not token or token.count(".") < 2:
            raise ValueError("Microsoft Edge auth did not return a token")
        exp = now + 300
        try:
            payload = token.split(".")[1]
            pad = "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload + pad).decode("utf-8"))
            exp = float(data.get("exp") or exp)
        except Exception:
            pass
        self._edge_token = token
        self._edge_token_exp = exp
        return token

    def _request_microsoft(self, text: str) -> str:
        """Microsoft Edge (Free) — same unofficial path as the Calibre plugin."""
        from urllib.parse import urlencode

        token = self._edge_auth_token()
        query = {
            "to": self._microsoft_lang(self.target_lang),
            "api-version": "3.0",
            "includeSentenceLength": True,
            "from": self._microsoft_lang(self.source_lang),
        }
        url = f"{self.ENDPOINT_EDGE}?{urlencode(query)}"
        response = self._request_http(
            "POST", url,
            data=json.dumps([{"text": text}], ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        data = response.json() if hasattr(response, "json") else []
        try:
            out = unescape(str(data[0]["translations"][0]["text"])).strip()
        except (IndexError, KeyError, TypeError):
            out = ""
        if not out:
            raise ValueError("Empty Microsoft Edge translation")
        return out
    
    def _request_libretranslate(self, text: str) -> str:
        """Translate via a LibreTranslate server."""
        from core.security import UnsafeURLError
        # LibreTranslate uses plain ISO codes ('zh', not 'zh-CN')
        source = self.source_lang.split('-')[0]
        try:
            response = self._request_http(
                "POST",
                f'{self.libretranslate_url}/translate',
                allow_http=True,
                resolve_dns=True,
                json={
                    'q': text,
                    'source': source,
                    'target': self.target_lang,
                    'format': 'text',
                },
            )
        except UnsafeURLError as e:
            raise ValueError(f"Blocked LibreTranslate URL: {e}") from e
        return response.json().get('translatedText', '')

    def _cache_backend(self) -> str:
        """Persistent-cache key. Ollama is namespaced by model."""
        if self.backend == 'ollama':
            return f'ollama:{self.ollama_model}'
        if self.backend == 'ctranslate2':
            return 'ctranslate2:opus-mt-zh-en'
        return self.backend

    def _legacy_cache_backends(self) -> List[str]:
        """Older cache namespaces still worth reading (empty by default)."""
        return []

    def _dedupe_key(self, source: str) -> str:
        """Group equivalent paragraphs onto one unofficial gtx GET."""
        keys = self._cache_keys_for(source)
        return keys[-1] if keys else (source or "").strip()

    def _cache_keys_for(self, source: str) -> List[str]:
        keys = []
        stripped = (source or "").strip()
        if stripped:
            keys.append(stripped)
        try:
            from core.cache import normalize_cache_source
            norm = normalize_cache_source(source)
            if norm and norm not in keys:
                keys.append(norm)
        except Exception:
            pass
        return keys

    def _forget_cached(self, source: str) -> None:
        """Drop a poisoned or failed row from memory and SQLite."""
        keys = self._cache_keys_for(source)
        if not keys:
            return
        with self.cache_lock:
            for key in keys:
                self.cache.pop(key, None)
        cache = self.persistent_cache
        if cache is None:
            return
        backends = [self._cache_backend()]
        for alias in self._legacy_cache_backends() or []:
            if alias and alias not in backends:
                backends.append(alias)
        pairs = [(key, backend) for key in keys for backend in backends]
        deleter = getattr(cache, "delete_translations", None)
        try:
            if callable(deleter):
                deleter(pairs)
            else:
                for key, backend in pairs:
                    cache.delete_translation(key, backend)
        except Exception:
            pass

    def _accept_cached(self, source: str, translated: Optional[str]) -> Optional[str]:
        if not translated:
            return None
        if is_usable_translation(source, translated):
            return translated
        self._forget_cached(source)
        return None

    def _store_usable(self, source: str, translated: str) -> None:
        if not is_usable_translation(source, translated):
            return
        cache_key = (source or "").strip()
        if not cache_key:
            return
        with self.cache_lock:
            self.cache[cache_key] = translated
        if self.persistent_cache is not None:
            self.persistent_cache.put_translation(
                cache_key, translated, self._cache_backend(), commit=False
            )

    def _get_cached_translation(self, source: str) -> Optional[str]:
        """Look up a segment, including legacy backend names."""
        cache = self.persistent_cache
        if cache is None or not source:
            return None
        primary = self._cache_backend()
        hit = cache.get_translation(source, primary)
        if hit:
            return self._accept_cached(source, hit)
        for alias in self._legacy_cache_backends():
            if not alias or alias == primary:
                continue
            hit = cache.get_translation(source, alias)
            if hit:
                return self._accept_cached(source, hit)
        return None

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
        if self.backend == 'microsoft':
            return self._request_microsoft(text)
        return self._request_google(text)

    def _google_gated(self, text: str) -> str:
        """One unofficial gtx call under the per-IP throttle."""
        gate = self._gtx
        if gate is None:
            return self._request_translation(text)
        if not gate.acquire(
            self._should_cancel, self._wait_if_paused, self._interruptible_sleep
        ):
            raise _GtxCancelled()
        try:
            out = self._request_translation(text)
            climbed = gate.on_success()
            if climbed is not None:
                print(
                    f"  {self._throttle_label()} in-flight cap {climbed} "
                    f"(recovering; ceiling {gate.max_limit})"
                )
            return out
        except RateLimitedError as exc:
            new_cap, wait = gate.on_429(exc.retry_after)
            if new_cap is not None:
                print(
                    f"  {self._throttle_label()} in-flight cap {new_cap}; "
                    f"cooling {wait:.0f}s so this IP can recover "
                    f"(ceiling {gate.max_limit})"
                )
            raise
        finally:
            gate.release()
    
    def _translate_single(self, text: str, index: int) -> Tuple[int, str]:
        """Translate a single text with exponential backoff retry."""
        self._mark_source_progress(index, inflight=True)
        try:
            return self._translate_single_inner(text, index)
        finally:
            self._mark_source_progress(index, inflight=False)

    def _translate_single_inner(self, text: str, index: int) -> Tuple[int, str]:
        if self._should_cancel():
            return (index, text)
            
        if not needs_gtx_request(text):
            self._update_progress()
            return (index, text)
        
        cache_key = text.strip()
        
        # Check in-memory cache
        with self.cache_lock:
            memory_hit = self.cache.get(cache_key)
        accepted = self._accept_cached(text, memory_hit)
        if accepted:
            with self.stats_lock:
                self.stats['cache_hits'] += 1
            self._update_progress()
            return (index, accepted)
        
        # Check persistent cache (and any legacy backend aliases)
        if self.persistent_cache is not None:
            cached = self._get_cached_translation(cache_key)
            if cached:
                with self.cache_lock:
                    self.cache[cache_key] = cached
                with self.stats_lock:
                    self.stats['cache_hits'] += 1
                self._update_progress()
                return (index, cached)
        
        self._wait_if_paused()
        if self._should_cancel():
            self._update_progress()
            return (index, text)

        google = self.backend in THROTTLED_BACKENDS and self._gtx is not None
        if not google:
            self._begin_request()
        try:
            for attempt in range(self.max_retries):
                self._wait_if_paused()
                if self._should_cancel():
                    return (index, text)
                try:
                    if google:
                        translated = self._google_gated(text)
                    else:
                        translated = self._request_translation(text)
                    if translated and translated.strip():
                        self._store_usable(text, translated)
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
                    raise ValueError("Empty translation response")
                except _GtxCancelled:
                    self._update_progress()
                    return (index, text)
                except RateLimitedError as e:
                    self._note_throttle(e.retry_after)
                    # Next attempt waits on the global cool inside acquire().
                except Exception:
                    if attempt < self.max_retries - 1:
                        self._interruptible_sleep(jittered_backoff_seconds(attempt))

            with self.failed_lock:
                preview = text[:50] + '...' if len(text) > 50 else text
                self.failed_texts.append((index, preview))
            with self.stats_lock:
                self.stats['errors'] += 1
            self._update_progress()
            return (index, text)
        finally:
            if not google:
                self._end_request()

    def _mark_source_progress(self, index: int, *, inflight: bool) -> None:
        """Remember which input slot is in flight so the UI can name the chapter."""
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return
        with self.progress_lock:
            active = self._active_source_indices
            if inflight:
                active.add(idx)
            else:
                active.discard(idx)
            self._progress_source_index = min(active) if active else idx

    def _begin_request(self) -> None:
        with self.stats_lock:
            self._in_flight = int(self._in_flight or 0) + 1
            n = self._in_flight
        self._emit_progress(force=(n == 1))

    def _end_request(self) -> None:
        with self.stats_lock:
            self._in_flight = max(0, int(self._in_flight or 0) - 1)
            n = self._in_flight
        self._emit_progress(force=(n == 0))

    def _emit_progress(self, *, force: bool = False) -> None:
        """Paint ~15 times/s so 200 completions in one wave do not skip the UI."""
        cb = self.progress_callback
        if not cb or self.total <= 0:
            return
        now = time.monotonic()
        with self.progress_lock:
            done = self.completed
            if (
                not force
                and 0 < done < self.total
                and (now - float(self._progress_emit_at or 0.0)) < 0.07
            ):
                return
            self._progress_emit_at = now
        cb(done, self.total)

    def _update_progress(self, n: int = 1) -> None:
        """Count finished segment slot(s) and refresh the status line."""
        n = max(0, int(n))
        if n <= 0:
            return
        with self.progress_lock:
            self.completed += n
            done = self.completed
            total = self.total
        self._emit_progress(force=(done >= total or done <= 1))

    def _flush_persistent_cache(self) -> None:
        cache = self.persistent_cache
        if cache is not None and hasattr(cache, "flush"):
            try:
                cache.flush()
            except Exception:
                pass

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

        # Keep a latched cancel so DownloadControl → cancel() is not lost
        # at the start of a pass (previously this reset mid-run cancel).
        if self._should_cancel():
            return list(texts)

        self.total = len(texts)
        self.completed = 0
        self._in_flight = 0
        self._unique_requests = 0
        self._progress_emit_at = 0.0
        self._progress_source_index = -1
        self._active_source_indices.clear()
        self.failed_texts = []
        self.progress_callback = progress_callback
        # Footer must leave "Starting download…" as soon as we know N,
        # before unique-GET grouping or the first unofficial GET.
        if self._gtx is not None:
            self._in_flight = int(self._gtx.limit or GtxThrottle.START_LIMIT)
        self._emit_progress(force=True)
        time.sleep(0)

        results = list(texts)
        groups: Dict[str, List[int]] = {}
        skipped = 0
        for i, text in enumerate(texts):
            if not needs_gtx_request(text):
                skipped += 1
                continue
            key = self._dedupe_key(text)
            groups.setdefault(key, []).append(i)
            if progress_callback and i > 0 and i % 8000 == 0:
                self._emit_progress(force=True)
                time.sleep(0)

        unique_n = len(groups)
        self._unique_requests = unique_n
        saved = len(texts) - skipped - unique_n
        label = "Google" if self.backend == "google" else self.backend
        bits = [f"{label}: {len(texts)} node(s) → {unique_n} unique GET(s)"]
        if skipped:
            bits.append(f"skipped {skipped} without CJK")
        if saved:
            bits.append(f"{saved} duplicate slot(s)")
        if skipped or saved or unique_n:
            print("  " + ", ".join(bits) + ".")

        if skipped:
            self._update_progress(skipped)
        if unique_n == 0:
            self._flush_persistent_cache()
            return results

        self._wait_if_paused()
        if self._should_cancel():
            self._flush_persistent_cache()
            return results

        if self.backend in THROTTLED_BACKENDS and self._gtx is not None:
            print(
                f"  {self._throttle_label()} in-flight cap {self._gtx.limit} "
                f"(ceiling {self._gtx.max_limit}; cools on 429, climbs on success)"
            )
            # Pool size is the UI ceiling. GtxThrottle (start 8) is the
            # in-flight cap — do not freeze the executor at start_cap*4 (32)
            # or the climb to 200 never adds workers.
            workers = min(self.max_workers, unique_n)
            self._in_flight = min(int(self._gtx.limit or 0), unique_n)
        else:
            workers = min(self.max_workers, unique_n)

        self._emit_progress(force=True)
        time.sleep(0)

        def _run_group(idxs: List[int]) -> Tuple[List[int], str]:
            if self._should_cancel():
                return idxs, texts[idxs[0]]
            index, translated = self._translate_single(texts[idxs[0]], idxs[0])
            extra = len(idxs) - 1
            if extra:
                self._update_progress(extra)
            return idxs, translated

        # Bounded backlog so the download thread can emit footer copy every
        # 70ms (in-flight) even before the first GET returns. Submitting all
        # unique jobs up front froze Dummy-N in a 47k-Future dict.
        backlog = min(max(workers * 4, 32), unique_n)
        pending = iter(groups.values())
        inflight: Dict[Any, List[int]] = {}

        def _fill() -> None:
            while len(inflight) < backlog:
                try:
                    idxs = next(pending)
                except StopIteration:
                    return
                inflight[executor.submit(_run_group, idxs)] = idxs

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
                    idxs = inflight.pop(future)
                    try:
                        group, translated = future.result()
                        for i in group:
                            results[i] = translated
                    except Exception:
                        for i in idxs:
                            results[i] = texts[i]
                _fill()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        self._flush_persistent_cache()
        return results
    
    def translate_texts_with_retry(
        self,
        texts: List[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
        is_chinese_fn: Optional[Callable[[str], bool]] = None,
        count_chinese_fn: Optional[Callable[[str], int]] = None,
        pass_callback: Optional[Callable[[int, int, int, float], None]] = None,
        max_retry_passes: int = 8,
    ) -> List[str]:
        """
        Translate texts and retry remaining failures, then stop.

        Uses a short cooldown between Google retry passes. Worker count stays
        at the configured pool (200). Ollama still scales down.
        
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
        
        # Google: executor size is not the in-flight cap — GtxThrottle is.
        # Longer cool between retry passes so a 429'd IP can recover.
        # Ollama still scales the pool down.
        if self.backend in THROTTLED_BACKENDS:
            WORKER_STEPS = [0, 0, 0, 0, 0, 0]
            INTERVAL_STEPS = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            COOLDOWN_STEPS = [0, 15, 30, 45, 60, 60]
            EXTRA_RETRIES = [0, 0, 1, 1, 2, 2]
        elif self.backend == "libretranslate":
            WORKER_STEPS = [0, 0, 0, 0, 0, 0]
            INTERVAL_STEPS = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            COOLDOWN_STEPS = [0, 1, 2, 3, 5, 8]
            EXTRA_RETRIES = [0, 0, 1, 1, 2, 2]
        else:
            WORKER_STEPS = [0, 50, 30, 20, 10, 5]
            INTERVAL_STEPS = [0.0, 0.3, 0.5, 1.0, 1.5, 2.0]
            COOLDOWN_STEPS = [0, 5, 10, 20, 30, 60]
            EXTRA_RETRIES = [0, 0, 1, 1, 2, 3]
        
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
        
        while not self._should_cancel():
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
            cap_note = ""
            if self.backend in THROTTLED_BACKENDS and self._gtx is not None:
                cap_note = f", in-flight cap {self._gtx.limit}"
            print(
                f"\n  Retry pass {retry_pass}: {len(failed_indices)}/{len(texts)} "
                f"still Chinese after the first pass "
                f"(usually HTTP 429 — not a second copy of the book) "
                f"(workers={retry_workers}{cap_note}, interval={interval:.1f}s, "
                f"cooldown={cooldown}s, retries={self.max_retries + extra_retry})"
            )
            
            if pass_callback:
                pass_callback(retry_pass, len(failed_indices), len(texts), cooldown)
            
            # ── Cooldown between passes ──
            if cooldown > 0:
                print(f"  ⏳ Cooling down for {cooldown}s before retry...")
                self._interruptible_sleep(float(cooldown))
            
            if self._should_cancel():
                break
            
            # ── Clear caches for failed texts (memory + persistent) ──
            for i in failed_indices:
                self._forget_cached(texts[i])
            
            # ── Apply retry settings ──
            old_interval = self.request_interval
            old_max_retries = self.max_retries
            old_workers = self.max_workers
            self.request_interval = max(interval, old_interval)
            self.max_retries = old_max_retries + extra_retry
            self.max_workers = retry_workers
            
            failed_texts = [texts[i] for i in failed_indices]
            retry_results = self.translate_texts(failed_texts, progress_callback)
            
            # ── Apply improved translations ──
            # Accept any result with less Chinese than what we currently have.
            # Partially translated text (e.g. an English sentence keeping a
            # Chinese name) counts as progress instead of being discarded,
            # which previously caused endless retry loops.
            improved = 0
            for j, i in enumerate(failed_indices):
                translated = retry_results[j] if j < len(retry_results) else ""
                if not translated:
                    continue
                if count_chinese_fn(translated) < count_chinese_fn(results[i]):
                    results[i] = translated
                    improved += 1
            
            # ── Restore original settings ──
            self.request_interval = old_interval
            self.max_retries = old_max_retries
            self.max_workers = old_workers
            
            print(f"  ✓ Pass {retry_pass} done: {improved}/{len(failed_indices)} newly translated")
            self._flush_persistent_cache()
        
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
                    self.persistent_cache.put_translation(
                        src, out, cache_backend, commit=False
                    )
            with self.stats_lock:
                self.stats['requests'] += 1
        print(f"  Local polish: {edited} edited, {len(pending) - edited} kept original")
        self._flush_persistent_cache()
        return results
    
    @staticmethod
    def _contains_chinese(text: str) -> bool:
        """Check if text contains Chinese characters."""
        if not text:
            return False
        return bool(_CJK_RE.search(text))

    @staticmethod
    def _count_chinese(text: str) -> int:
        """Count Chinese characters in text."""
        if not text:
            return 0
        return len(_CJK_RE.findall(text))

    @staticmethod
    def is_chinese(text: str) -> bool:
        """Check if text contains significant Chinese characters."""
        if not text:
            return False
        chinese_count = len(_CJK_RE.findall(text))
        return chinese_count > len(text) * 0.1  # More than 10% Chinese

    def get_stats(self) -> Dict[str, Any]:
        """Get translation statistics."""
        return self.stats.copy()

    def reset_stats(self) -> None:
        """Reset statistics."""
        self.stats = {
            'requests': 0,
            'paragraphs_translated': 0,
            'characters_translated': 0,
            'cache_hits': 0,
            'errors': 0,
            'retries': 0,
            'retry_passes': 0,
            'throttles': 0,
        }
        self.failed_texts.clear()
    
    def clear_cache(self) -> None:
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


# Re-export Ollama helpers so existing `from core.translator import …` keeps working.
__all__ = [
    "DEFAULT_GOOGLE_WORKERS",
    "GOOGLE_FAMILY_BACKENDS",
    "GoogleTranslator",
    "GtxThrottle",
    "MAX_PACKED_WORKERS",
    "THROTTLED_BACKENDS",
    "RateLimitedError",
    "is_polish_skip",
    "jittered_backoff_seconds",
    "needs_gtx_request",
    "parse_retry_after",
    "list_ollama_models",
    "ollama_gpu_available",
    "ollama_infer_options",
    "ollama_is_installed",
    "ollama_model_installed",
    "pack_numbered_segments",
    "probe_ollama",
    "pull_ollama_model",
    "resolve_ollama_model",
    "should_polish_english",
    "unpack_numbered_segments",
    "unpack_sparse_segments",
]
