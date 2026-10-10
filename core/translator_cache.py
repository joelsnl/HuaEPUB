# Author: joelsnl and Anthropic Claude
"""What GoogleTranslator may keep in its caches, and how it reads and writes them.

``is_usable_translation`` decides whether a result counts as a translation at all: an echoed
Chinese chapter must never become a cache hit.
"""

from __future__ import annotations

import re
from typing import List, Optional

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


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
    src_cjk = len(CJK_RE.findall(src))
    if src_cjk <= 5:
        return True
    if out == src:
        return False
    out_cjk = len(CJK_RE.findall(out))
    if out_cjk <= 5:
        return True
    if out_cjk >= max(src_cjk * 0.5, 20):
        return False
    return True


class TranslationCacheMixin:
    """In-memory and persistent cache helpers of GoogleTranslator."""

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

    def _flush_persistent_cache(self) -> None:
        cache = self.persistent_cache
        if cache is not None and hasattr(cache, "flush"):
            try:
                cache.flush()
            except Exception:
                pass
