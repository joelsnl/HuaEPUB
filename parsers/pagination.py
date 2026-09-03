# Author: joelsnl and Anthropic Claude
"""Follow TOC / chapter-body next links without looping forever."""

from __future__ import annotations

import re
from typing import Callable, List
from urllib.parse import urljoin, urldefrag

from bs4 import BeautifulSoup

from core.parser import Chapter

MAX_LIST_PAGES = 80
MAX_CONTENT_PAGES = 30

_PAGE_NEXT_RE = re.compile(
    r"(下一页|下[一]?页|next\s*page|>\s*$)",
    re.IGNORECASE,
)
_CHAPTER_NEXT_RE = re.compile(
    r"(下一章|下一回|next\s*chapter)",
    re.IGNORECASE,
)


def canonicalize_page_url(url: str) -> str:
    return urldefrag((url or "").strip())[0]


def href_from_element(el, base_url: str) -> str:
    if el is None:
        return ""
    href = (el.get("href") or "").strip()
    if not href or href.startswith(("javascript:", "#", "mailto:")):
        return ""
    return canonicalize_page_url(urljoin(base_url, href))


def links_to_chapters(links, base_url: str, *, href_contains: str = "") -> List[Chapter]:
    """Turn <a> nodes into Chapter rows (same skip list as href_from_element)."""
    chapters: List[Chapter] = []
    seen: set[str] = set()
    must = (href_contains or "").strip()
    for link in links:
        href = (link.get("href") or "").strip()
        title = link.get_text(strip=True)
        if not href or not title or href.startswith(("javascript:", "#", "mailto:")):
            continue
        if must and must not in href:
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        chapters.append(Chapter(title=title, url=absolute, index=len(chapters)))
    return chapters


def next_from_selector(soup: BeautifulSoup, selector: str, base_url: str) -> str:
    if not selector:
        return ""
    try:
        el = soup.select_one(selector)
    except Exception:
        return ""
    return href_from_element(el, base_url)


def next_from_rel(soup: BeautifulSoup, base_url: str) -> str:
    try:
        nodes = soup.select("a[rel~=next], link[rel~=next]")
    except Exception:
        nodes = []
    for el in nodes:
        href = href_from_element(el, base_url)
        if href:
            return href
    return ""


def next_content_page_url(soup: BeautifulSoup, base_url: str) -> str:
    """
    Next *page of this chapter*, not the next chapter.

    rel=next is used only when the link text is not 下一章 / next chapter.
    Otherwise look for 下一页 / next page.
    """
    try:
        rel_nodes = soup.select("a[rel~=next]")
    except Exception:
        rel_nodes = []
    for el in rel_nodes:
        text = el.get_text(strip=True)
        if _CHAPTER_NEXT_RE.search(text or ""):
            continue
        href = href_from_element(el, base_url)
        if href:
            return href
    for el in soup.find_all("a", href=True):
        text = el.get_text(strip=True)
        if _PAGE_NEXT_RE.search(text or "") and not _CHAPTER_NEXT_RE.search(text or ""):
            href = href_from_element(el, base_url)
            if href:
                return href
    return ""


def merge_chapter_pages(first_html: str, extra_html: List[str]) -> str:
    if not extra_html:
        return first_html
    return first_html + "\n" + "\n".join(extra_html)


def walk_list_pages(
    *,
    first_soup: BeautifulSoup,
    first_url: str,
    parse_chapters: Callable[[BeautifulSoup, str], List[Chapter]],
    next_url: Callable[[BeautifulSoup, str], str],
    fetch_page: Callable[[str], BeautifulSoup],
    delay: Callable[[], None],
    max_pages: int = MAX_LIST_PAGES,
) -> List[Chapter]:
    chapters: List[Chapter] = []
    seen_pages = set()
    seen_ch = set()
    soup = first_soup
    url = first_url
    for _ in range(max(1, max_pages)):
        page = canonicalize_page_url(url)
        if not page or page in seen_pages:
            break
        seen_pages.add(page)
        for ch in parse_chapters(soup, url) or []:
            key = canonicalize_page_url(ch.url)
            if not key or key in seen_ch:
                continue
            seen_ch.add(key)
            ch.index = len(chapters)
            chapters.append(ch)
        nxt = canonicalize_page_url(next_url(soup, url) or "")
        if not nxt or nxt in seen_pages:
            break
        delay()
        soup = fetch_page(nxt)
        url = nxt
    return chapters


def walk_content_pages(
    *,
    first_html: str,
    first_soup: BeautifulSoup,
    first_url: str,
    next_url: Callable[[BeautifulSoup, str], str],
    fetch_html_and_soup: Callable[[str], tuple[str, BeautifulSoup]],
    delay: Callable[[], None],
    max_pages: int = MAX_CONTENT_PAGES,
) -> str:
    parts = [first_html]
    seen = {canonicalize_page_url(first_url)}
    soup = first_soup
    url = first_url
    for _ in range(max(0, max_pages - 1)):
        nxt = canonicalize_page_url(next_url(soup, url) or "")
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        delay()
        html, soup = fetch_html_and_soup(nxt)
        if html:
            parts.append(html)
        url = nxt
    return merge_chapter_pages(parts[0], parts[1:])
