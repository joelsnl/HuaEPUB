# Author: joelsnl
"""
Pack many short paragraphs into one Google/LibreTranslate request.

A 1000-chapter novel can be 40k–60k HTML text nodes. One HTTP call per
node is why Google runs take hours. Markers are ASCII so they usually
survive the engine; if a pack comes back garbled we split and retry
those segments one-by-one.
"""

from __future__ import annotations

import re
from typing import Optional

# ~4k source chars stays under Google's comfortable POST size after markers.
PACK_CHAR_LIMIT = 4000
_MARK = re.compile(r"\[\[#(\d+)#\]\]\s*")


def pack_mt_segments(texts: list[str]) -> str:
    parts = [f"[[#{i}#]]\n{(t or '').strip()}" for i, t in enumerate(texts, 1)]
    return "\n".join(parts)


def unpack_mt_segments(blob: str, expected: int) -> Optional[list[str]]:
    raw = blob or ""
    if expected <= 0:
        return None
    if expected == 1 and not _MARK.search(raw):
        piece = raw.strip()
        return [piece] if piece else None
    matches = list(_MARK.finditer(raw))
    if len(matches) != expected:
        return None
    out: list[str] = [""] * expected
    seen: set[int] = set()
    for i, match in enumerate(matches):
        num = int(match.group(1))
        if num < 1 or num > expected or num in seen:
            return None
        seen.add(num)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        out[num - 1] = raw[start:end].strip()
    if any(not piece for piece in out):
        return None
    return out


def group_by_char_budget(
    texts: list[str],
    max_chars: int = PACK_CHAR_LIMIT,
) -> list[list[int]]:
    """Split pending segment indexes into packs that stay near max_chars."""
    groups: list[list[int]] = []
    current: list[int] = []
    size = 0
    limit = max(200, int(max_chars))
    for i, text in enumerate(texts):
        extra = len(text or "") + 16
        if current and size + extra > limit:
            groups.append(current)
            current = []
            size = 0
        current.append(i)
        size += extra
    if current:
        groups.append(current)
    return groups
