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
"""

import re
import requests
import time
import threading
import concurrent.futures
from typing import List, Tuple, Dict, Optional, Callable


class GoogleTranslator:
    """Google Translate Free API with concurrent requests, retry logic, and multi-pass retry."""
    
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
    ):
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.max_workers = max_workers
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.request_interval = request_interval
        
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
    
    def _translate_single(self, text: str, index: int) -> Tuple[int, str]:
        """Translate a single text with exponential backoff retry."""
        if self._cancel_requested:
            return (index, text)
            
        if not text or not text.strip():
            return (index, text)
        
        cache_key = text.strip()
        
        # Check cache
        with self.cache_lock:
            if cache_key in self.cache:
                with self.stats_lock:
                    self.stats['cache_hits'] += 1
                self._update_progress()
                return (index, self.cache[cache_key])
        
        params = {
            'client': 'gtx',
            'sl': self.source_lang,
            'tl': self.target_lang,
            'dt': 't',
            'dj': '1',
            'q': text
        }
        
        last_error = None
        for attempt in range(self.max_retries):
            if self._cancel_requested:
                return (index, text)
                
            try:
                # Use GET for short texts, POST for long texts
                if len(text) <= 1800:
                    response = requests.get(
                        self.ENDPOINT,
                        params=params,
                        headers={'User-Agent': self.USER_AGENT},
                        timeout=self.request_timeout
                    )
                else:
                    response = requests.post(
                        self.ENDPOINT,
                        data=params,
                        headers={'User-Agent': self.USER_AGENT},
                        timeout=self.request_timeout
                    )
                
                response.raise_for_status()
                data = response.json()
                
                # Extract translated text
                translated = ''.join(
                    s.get('trans', '') 
                    for s in data.get('sentences', []) 
                    if 'trans' in s
                )
                
                if translated and translated.strip():
                    # Cache the result
                    with self.cache_lock:
                        self.cache[cache_key] = translated
                    
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
        
        Keeps looping until zero failures remain, cancelled, or max_retry_passes
        is reached with 3 or fewer segments still failing (to avoid getting stuck
        on a handful of stubborn segments that the API consistently rejects).
        
        Args:
            texts: List of texts to translate
            progress_callback: Optional callback(completed, total) for per-text progress
            is_chinese_fn: Function to check if text contains Chinese
            count_chinese_fn: Function to count Chinese chars
            pass_callback: Optional callback(pass_number, remaining, total, cooldown)
                           called at the start of each retry pass
            max_retry_passes: Give up if stuck on ≤3 segments after this many passes
            
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

            if len(failed_indices) <= 3 and retry_pass >= max_retry_passes:
                print(f"\n  ⚠ Stuck on {len(failed_indices)} segment(s) after {max_retry_passes} passes. "
                      f"Giving up and proceeding.")
                break  # Don't block forever on a handful of stubborn segments
            
            retry_pass += 1
            
            with self.stats_lock:
                self.stats['retry_passes'] += 1
            
            # ── Smart delay: pick settings for this pass ──
            workers_cap  = _get_step(WORKER_STEPS, retry_pass)
            interval     = _get_step(INTERVAL_STEPS, retry_pass)
            cooldown     = _get_step(COOLDOWN_STEPS, retry_pass)
            extra_retry  = _get_step(EXTRA_RETRIES, retry_pass)
            
            # Stall detection: if no progress for 3+ passes, go even slower
            if prev_failed_count is not None and len(failed_indices) >= prev_failed_count:
                stall_count += 1
                if stall_count >= 3:
                    # Force maximum backoff
                    cooldown = max(cooldown, 90)
                    interval = max(interval, 2.5)
                    workers_cap = min(workers_cap or 3, 3)
            else:
                stall_count = 0
            prev_failed_count = len(failed_indices)
            
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
            
            # ── Clear cache for failed texts ──
            with self.cache_lock:
                for i in failed_indices:
                    cache_key = texts[i].strip()
                    self.cache.pop(cache_key, None)
            
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
            
            # ── Apply only improved translations ──
            improved = 0
            for j, i in enumerate(failed_indices):
                translated = retry_results[j]
                if translated and not is_chinese_fn(translated):
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
