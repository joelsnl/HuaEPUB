# Author: joelsnl
"""KEEP/REPLACE local polish after Google/LibreTranslate.

HuaEPUB's own novel-tested implementation (core.polish). Installs llama.cpp
and a Qwen GGUF under ~/.huaepub/polish if needed. Ollama is not required.
Polish runs in-memory on the same EPUB. Progress goes to huaepub.log.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import List, Optional, Tuple

CACHE_BACKEND = "span-polish:v2"


def wants_polish(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    from core.translator import GoogleTranslator, should_polish_english

    if GoogleTranslator.is_chinese(raw):
        return False
    if should_polish_english(raw):
        return True
    from core.polish.api import wants_polish as polish_wants

    return polish_wants(raw)


def polish_paragraphs(
    texts: List[str],
    *,
    progress: Optional[Callable[[int, int], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Tuple[List[str], str]:
    """Polish English paragraphs. Returns (texts, model_id)."""
    from core.polish.api import polish_paragraphs as run

    return run(
        texts,
        progress=progress,
        cancelled=cancelled,
        log=log or print,
        auto_serve=True,
    )
