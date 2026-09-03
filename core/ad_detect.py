# Author: joelsnl and Anthropic Claude
"""
Learn repeating site watermarks/ads from a few chapters.

Independent of Polish English / llama.cpp. Does not start a model, does not
download a GGUF, and does not wait on GPU copy-edit. When Clean is on, the
first chapters of a book are compared and repeating promotional lines are
added as extra ContentCleaner literals.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Callable, Iterable, List, Optional

SAMPLE_CHAPTERS = 5
_MIN_SAMPLES = 2
_MIN_LEN = 6
_MAX_LEN = 120

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_CHAPTER_HEAD = re.compile(
    r"^第[零一二三四五六七八九十百千万0-9]+[章节回卷].{0,40}$"
)
_JUNK_HINT = re.compile(
    r"(收藏|首发|首發|阅读|閱讀|无弹窗|無彈窗|最新章|手机阅读|手機閱讀|"
    r"下載|下载|公众号|公眾號|请到|請到|记住本|記住本|本站|域名|"
    r"最快更新|点击下载|點擊下載|扫码|掃碼|www\.|https?://|"
    r"\.com|\.net|\.cc|小说网|小說網|笔趣|頂点|顶点|"
    r"本章完|未完|下一页|下一頁|广告|廣告|txtad|纯文字|無錯|无错|"
    r"天才一秒|访问下载|訪問下載|欢迎广大|歡迎廣大|请收藏|請收藏|书吧|書吧)",
    re.IGNORECASE,
)

StatusFn = Callable[[str], None]


def _chapter_html(chapter) -> str:
    return (
        getattr(chapter, "content", None)
        or getattr(chapter, "html", None)
        or ""
    )


def sample_chapter_html(chapters: Iterable, n: int = SAMPLE_CHAPTERS) -> List[str]:
    out: List[str] = []
    for chapter in chapters or []:
        html = str(_chapter_html(chapter)).strip()
        if html:
            out.append(html)
        if len(out) >= n:
            break
    return out


def paragraphs(html: str) -> List[str]:
    text = _TAGS.sub("\n", html or "")
    lines: List[str] = []
    for chunk in re.split(r"[\n\r]+", text):
        line = _WS.sub(" ", chunk).strip()
        if line:
            lines.append(line)
    return lines


def _edge_lines(paras: List[str], n: int = 4) -> List[str]:
    if len(paras) <= n * 2:
        return list(paras)
    return paras[:n] + paras[-n:]


def repeating_junk_lines(htmls: List[str]) -> List[str]:
    """Lines that recur at chapter start/end and look like site promo, not plot."""
    samples = [paragraphs(h) for h in htmls if (h or "").strip()]
    n = len(samples)
    if n < _MIN_SAMPLES:
        return []
    need = max(_MIN_SAMPLES, (n + 1) // 2)  # 2 of 2–3, 3 of 4–5
    counts: dict[str, set[int]] = defaultdict(set)
    for idx, paras in enumerate(samples):
        for line in _edge_lines(paras):
            if _MIN_LEN <= len(line) <= _MAX_LEN:
                counts[line].add(idx)
    found: List[str] = []
    for line, seen in counts.items():
        if len(seen) < need:
            continue
        if _CHAPTER_HEAD.match(line):
            continue
        if _JUNK_HINT.search(line) and line not in found:
            found.append(line)
    return found


def learn_site_junk(
    cleaner,
    chapters,
    *,
    set_status: Optional[StatusFn] = None,
) -> List[str]:
    """
    Compare the first chapters and add repeating ads to *cleaner*.

    No-op if Cleaner already learned this run, or if fewer than two chapters
    have HTML. Never starts Polish / llama.cpp.
    """
    if cleaner is None:
        return []
    if getattr(cleaner, "_site_junk_learned", False):
        return list(getattr(cleaner, "_learned_literals", []) or [])
    htmls = sample_chapter_html(chapters, SAMPLE_CHAPTERS)
    if len(htmls) < _MIN_SAMPLES:
        return []
    if set_status:
        set_status(f"Learning site ads from {len(htmls)} chapters…")
    literals = repeating_junk_lines(htmls)
    added = 0
    add = getattr(cleaner, "add_literals", None)
    if literals and callable(add):
        added = int(add(literals) or 0)
    cleaner._site_junk_learned = True
    if added:
        print(f"  Learned {added} repeating watermark/ad line(s) from {len(htmls)} chapters")
    return literals
