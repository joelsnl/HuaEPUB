# Author: joelsnl and Anthropic Claude
"""
Learn repeating site watermarks/ads from a few chapters.

Independent of Polish English / llama.cpp. Does not start a model, does not
download a GGUF, and does not wait on GPU copy-edit. When Clean is on, many
chapters of a book are compared and repeating promotional lines are added as
extra ContentCleaner literals.

Two chapters is not enough: prologues often lack the site footer, and a line
that appears twice can still be plot. Live learning waits for SAMPLE_CHAPTERS
before locking; a final pass can spread-sample the whole book.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Callable, Iterable, List, Optional

SAMPLE_CHAPTERS = 12
FINAL_SAMPLE_CHAPTERS = 40
_MIN_SAMPLES = 4
_MIN_LEN = 6
_MAX_LEN = 200
_EDGE = 6

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_CHAPTER_HEAD = re.compile(
    r"^第[零一二三四五六七八九十百千万0-9]+[章节回卷].{0,40}$"
)
_JUNK_HINT = re.compile(
    r"(收藏|首发|首發|阅读|閱讀|无弹窗|無彈窗|最新章|手机阅读|手機閱讀|"
    r"下載|下载|公众号|公眾號|请到|請到|记住本|記住本|本站|域名|"
    r"最快更新|点击下载|點擊下載|扫码|掃碼|www\.|https?://|"
    r"\.com|\.net|\.cc|\.info|\.xyz|\.top|小说网|小說網|笔趣|頂点|顶点|"
    r"本章完|未完|下一页|下一頁|广告|廣告|txtad|纯文字|無錯|无错|"
    r"天才一秒|访问下载|訪問下載|欢迎广大|歡迎廣大|请收藏|請收藏|书吧|書吧|"
    r"书架|書架|推薦票|推荐票|月票|求收藏|求推荐|求推薦|"
    r"一秒记住|一秒記住|备用网址|備用網址|最新网址|最新網址|"
    r"免费阅读|免費閱讀|完整版|全本|章节目录|章節目錄|"
    r"加入书架|加入書架|打开浏览器|打開瀏覽器|"
    r"笔下文学|筆下文學|新笔趣|飄天|飘天|69书吧|69書吧|"
    r"纯文字在线|純文字在線|更新最快|看最新章)",
    re.IGNORECASE,
)

StatusFn = Callable[[str], None]


def _chapter_html(chapter) -> str:
    return (
        getattr(chapter, "content", None)
        or getattr(chapter, "html", None)
        or ""
    )


def sample_chapter_html(
    chapters: Iterable,
    n: int = SAMPLE_CHAPTERS,
    *,
    spread: bool = False,
) -> List[str]:
    htmls: List[str] = []
    for chapter in chapters or []:
        html = str(_chapter_html(chapter)).strip()
        if html:
            htmls.append(html)
    if not htmls:
        return []
    if not spread or len(htmls) <= n:
        return htmls[:n]
    last = len(htmls) - 1
    idxs = sorted({round(i * last / (n - 1)) for i in range(n)})
    return [htmls[i] for i in idxs]


def paragraphs(html: str) -> List[str]:
    text = _TAGS.sub("\n", html or "")
    lines: List[str] = []
    for chunk in re.split(r"[\n\r]+", text):
        line = _WS.sub(" ", chunk).strip()
        if line:
            lines.append(line)
    return lines


def _edge_lines(paras: List[str], n: int = _EDGE) -> List[str]:
    if len(paras) <= n * 2:
        return list(paras)
    head, tail = paras[:n], paras[-n:]
    mid = [
        p for p in paras[n:-n]
        if _JUNK_HINT.search(p) and _MIN_LEN <= len(p) <= _MAX_LEN
    ]
    return head + mid + tail


def _need_count(n: int) -> int:
    """Half of samples, at least 3. Never commit from a pair of chapters."""
    return max(3, (n + 1) // 2)


def repeating_junk_lines(htmls: List[str]) -> List[str]:
    """Lines that recur at chapter start/end and look like site promo, not plot."""
    samples = [paragraphs(h) for h in htmls if (h or "").strip()]
    n = len(samples)
    if n < _MIN_SAMPLES:
        return []
    need = _need_count(n)
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
    finalize: bool = False,
) -> List[str]:
    """
    Compare chapters and add repeating ads to *cleaner*.

    During fetch, wait until SAMPLE_CHAPTERS have HTML before locking.
    ``finalize=True`` (end of fetch / EPUB build) spread-samples the book
    and may add more literals. Never starts Polish / llama.cpp.
    """
    if cleaner is None:
        return []
    already = bool(getattr(cleaner, "_site_junk_learned", False))
    existing = list(getattr(cleaner, "_learned_literals", []) or [])
    if already and not finalize:
        return existing
    cap = FINAL_SAMPLE_CHAPTERS if finalize else SAMPLE_CHAPTERS
    htmls = sample_chapter_html(chapters, cap, spread=finalize)
    if len(htmls) < _MIN_SAMPLES:
        return existing
    if not finalize and len(htmls) < SAMPLE_CHAPTERS:
        return existing
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
    return list(getattr(cleaner, "_learned_literals", []) or []) or literals
