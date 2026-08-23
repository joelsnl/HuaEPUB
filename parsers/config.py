# Author: joelsnl and Anthropic Claude
"""
Single site parser driven by parsers/sites.json.

Each JSON object is a hostname plus CSS selectors (and optional encoding,
delay, AJAX chapter-list URL, etc.). Unknown hosts fall through to GenericParser.

If content selectors miss, GenericParser's density heuristic is used and
Chapter.used_heuristic is set (completion dialog warns).
"""

from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core.parser import Chapter, NovelInfo
from parsers.generic import GenericParser
from parsers.pagination import next_from_selector, walk_content_pages, walk_list_pages

_SITES: Optional[List[dict]] = None
_LOCK = threading.Lock()
_GENERIC_CHAPTER_LIST = frozenset({
    "a", "div", "span", "li", "p", "ul", "ol", "body", "html",
})


def _sites_json_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "parsers" / "sites.json"
    return Path(__file__).resolve().parent / "sites.json"


def load_sites() -> List[dict]:
    global _SITES
    if _SITES is None:
        with _LOCK:
            if _SITES is None:
                raw = _sites_json_path().read_text(encoding="utf-8")
                data = json.loads(raw)
                if not isinstance(data, list):
                    raise ValueError("parsers/sites.json must be a JSON array")
                _SITES = data
    return _SITES


def spec_for_url(url: str) -> Optional[dict]:
    host = _hostname(url)
    if not host:
        return None
    for spec in load_sites():
        for domain in spec.get("domains") or []:
            if _domain_matches_host(host, domain):
                return spec
    return None


def _hostname(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").lower().rstrip(".")


def _domain_matches_host(host: str, domain: str) -> bool:
    d = (domain or "").strip().lower().rstrip(".")
    if not host or not d:
        return False
    if "/" in d or ":" in d:
        d = (urlparse(f"https://{d}").hostname or "").lower().rstrip(".")
        if not d:
            return False
    return host == d or host.endswith("." + d)


def _as_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)]


def _el_text(el) -> str:
    if el is None:
        return ""
    if getattr(el, "name", "") == "meta":
        return (el.get("content") or "").strip()
    return el.get_text(strip=True)


def _el_src(el) -> str:
    if el is None:
        return ""
    if getattr(el, "name", "") == "meta":
        return (el.get("content") or "").strip()
    if getattr(el, "name", "") == "img":
        return (el.get("src") or el.get("data-src") or "").strip()
    img = el.find("img") if hasattr(el, "find") else None
    if img:
        return (img.get("src") or img.get("data-src") or "").strip()
    return (el.get("src") or el.get("href") or "").strip()


class SiteConfigParser(GenericParser):
    """One class for every host listed in sites.json."""

    SITE_NAME = "sites.json"
    SITE_DOMAINS: List[str] = []

    def __init__(self, spec: Optional[dict] = None):
        super().__init__()
        self.spec: dict = spec or {}
        self._bind_spec(self.spec)

    def _bind_spec(self, spec: dict):
        self.spec = spec or {}
        if spec:
            self.SITE_NAME = spec.get("name") or (spec.get("domains") or [self.SITE_NAME])[0]
            self.SITE_DOMAINS = list(spec.get("domains") or [])
            delay = spec.get("delay")
            if delay is not None:
                self.request_delay = float(delay)
            enc = (spec.get("encoding") or "").strip()
            self._encoding = enc or None
            origin = spec.get("origin") or ""
            if origin:
                self._referer = origin
            headers = spec.get("headers")
            if isinstance(headers, dict):
                try:
                    self.session.headers.update(
                        {str(k): str(v) for k, v in headers.items()}
                    )
                except Exception:
                    pass

    @classmethod
    def for_url(cls, url: str) -> "SiteConfigParser":
        inst = cls(spec_for_url(url))
        if inst.spec.get("referer"):
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if parsed.scheme and parsed.netloc:
                inst._referer = f"{parsed.scheme}://{parsed.netloc}"
        return inst

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return spec_for_url(url) is not None

    @classmethod
    def configured_site_names(cls) -> List[str]:
        names = []
        for spec in load_sites():
            name = spec.get("name") or (spec.get("domains") or [""])[0]
            if name:
                names.append(name)
        return names

    def _ensure_spec(self, url: str) -> dict:
        if self.spec:
            return self.spec
        spec = spec_for_url(url)
        if spec:
            self._bind_spec(spec)
        return self.spec

    def _first(self, soup: BeautifulSoup, selector: str):
        if not selector:
            return None
        try:
            return soup.select_one(selector)
        except Exception:
            return None

    def _select_all(self, soup: BeautifulSoup, selector: str):
        if not selector:
            return []
        try:
            return list(soup.select(selector))
        except Exception:
            return []

    def _text(self, soup: BeautifulSoup, key: str) -> str:
        spec = self.spec
        idx = spec.get(f"{key}_index")
        for sel in _as_list(spec.get(key)):
            nodes = self._select_all(soup, sel)
            if not nodes:
                continue
            if idx is not None:
                try:
                    i = int(idx)
                except (TypeError, ValueError):
                    i = 0
                if 0 <= i < len(nodes):
                    text = _el_text(nodes[i])
                    if text:
                        return text
                continue
            text = _el_text(nodes[0])
            if text:
                return text
        return ""

    def _cover(self, soup: BeautifulSoup, base_url: str) -> str:
        for sel in _as_list(self.spec.get("cover")):
            el = self._first(soup, sel)
            src = _el_src(el)
            if src:
                return urljoin(base_url, src)
        template = self.spec.get("cover_template") or ""
        book_id = self._book_id(base_url)
        if template and book_id:
            prefix = book_id[:2] if len(book_id) >= 2 else book_id
            try:
                return template.format(book_id=book_id, prefix=prefix)
            except Exception:
                return ""
        return ""

    def _book_id(self, url: str) -> Optional[str]:
        pattern = self.spec.get("book_id") or ""
        if not pattern:
            return None
        try:
            match = re.search(pattern, url)
        except re.error:
            return None
        return match.group(1) if match else None

    def parse_novel_info(self, soup: BeautifulSoup, url: str) -> NovelInfo:
        self._ensure_spec(url)
        info = self._parse_novel_info(soup, url)
        spec = self.spec
        title = self._text(soup, "title")
        if title:
            info.title = title
        author = self._text(soup, "author")
        if author:
            info.author = author
        desc = self._text(soup, "description")
        if desc:
            info.description = desc
        cover = self._cover(soup, url)
        if cover:
            info.cover_url = cover
        lang = spec.get("language")
        if lang:
            info.language = lang
        tag = self._text(soup, "tags")
        if tag and tag not in info.tags:
            info.tags.append(tag)
        return info

    def _chapters_from_selector(
        self, soup: BeautifulSoup, base_url: str, selector: str
    ) -> List[Chapter]:
        if not selector or selector.strip().lower() in _GENERIC_CHAPTER_LIST:
            return []
        nodes = self._select_all(soup, selector)
        links = []
        for node in nodes:
            if getattr(node, "name", "") == "a" and node.get("href"):
                links.append(node)
            elif hasattr(node, "find_all"):
                links.extend(node.find_all("a", href=True))
        must = (self.spec.get("chapter_href_contains") or "").strip()
        chapters: List[Chapter] = []
        seen = set()
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
        if self.spec.get("reverse"):
            chapters.reverse()
            for i, ch in enumerate(chapters):
                ch.index = i
        return chapters

    def parse_chapter_list(self, soup: BeautifulSoup, url: str) -> List[Chapter]:
        self._ensure_spec(url)
        selector = self.spec.get("chapter_list")
        chapters: List[Chapter] = []
        if selector:
            chapters = self._chapters_from_selector(soup, url, selector)
        if not chapters:
            chapters = self._parse_chapter_list(soup, url)
        return chapters

    def fetch_all_parallel(self, url: str):
        soup = self.fetch_page(url)
        info = self.parse_novel_info(soup, url)
        chapters = self._load_chapter_list(url, soup)
        return info, chapters

    def get_novel_info(self, url: str) -> NovelInfo:
        soup = self.fetch_page(url)
        return self.parse_novel_info(soup, url)

    def get_chapter_list(self, url: str) -> List[Chapter]:
        soup = None
        if self.spec.get("visit_toc_first") or self.spec.get("toc_link") or not self.spec.get("chapter_list_url"):
            soup = self.fetch_page(url)
        return self._load_chapter_list(url, soup)

    def _load_chapter_list(self, url: str, soup: Optional[BeautifulSoup]) -> List[Chapter]:
        self._ensure_spec(url)
        spec = self.spec
        if soup is None:
            soup = self.fetch_page(url)

        toc_sel = spec.get("toc_link")
        if toc_sel:
            a = self._first(soup, toc_sel)
            href = a.get("href") if a else None
            if href:
                toc_url = urljoin(url, href)
                soup = self.fetch_page(toc_url)
                url = toc_url

        list_url = spec.get("chapter_list_url") or ""
        book_id = self._book_id(url)
        if list_url:
            if "{book_id}" in list_url:
                if not book_id:
                    raise ValueError(f"Could not extract book ID from URL: {url}")
                list_url = list_url.format(book_id=book_id)
            html = self.fetch_html(list_url)
            soup = BeautifulSoup(html, "lxml")

        if spec.get("chapter_list_next"):
            chapters = walk_list_pages(
                first_soup=soup,
                first_url=url,
                parse_chapters=self.parse_chapter_list,
                next_url=lambda s, u: next_from_selector(
                    s, spec.get("chapter_list_next") or "", u
                ),
                fetch_page=self.fetch_page,
                delay=self._page_delay,
            )
        else:
            chapters = self.parse_chapter_list(soup, url)
        if not chapters:
            raise ValueError(
                f"{self.SITE_NAME} parser could not find a chapter list. "
                "Make sure the URL is the novel's table-of-contents page."
            )
        return chapters

    def _content_element(self, soup, spec: dict):
        content_el = None
        for sel in _as_list(spec.get("content")):
            content_el = self._first(soup, sel)
            if content_el is not None:
                break
        if content_el is None:
            return None
        remove = spec.get("remove")
        if remove:
            try:
                for el in content_el.select(remove):
                    el.decompose()
            except Exception:
                pass
        return content_el

    def get_chapter_content(self, chapter: Chapter) -> str:
        self._ensure_spec(chapter.url)
        soup = self.fetch_page(chapter.url)
        spec = self.spec
        content_el = self._content_element(soup, spec)
        if content_el is None:
            chapter.used_heuristic = True
            print(
                f"Warning: {self.SITE_NAME} content selector missed at {chapter.url}; "
                "using generic density heuristic. The chapter text may be wrong."
            )
            return self._content_html_from_soup(soup, chapter)

        title_el = None
        for sel in _as_list(spec.get("chapter_title")):
            title_el = self._first(soup, sel)
            if title_el is not None:
                break
        chapter_title = (
            title_el.get_text(strip=True) if title_el else chapter.title
        )
        first = f"<h1>{chapter_title}</h1>\n{content_el}"
        if not spec.get("content_next"):
            return first

        def fetch_more(next_url: str):
            more_soup = self.fetch_page(next_url)
            extra_el = self._content_element(more_soup, spec)
            extra = str(extra_el) if extra_el is not None else ""
            return extra, more_soup

        return walk_content_pages(
            first_html=first,
            first_soup=soup,
            first_url=chapter.url,
            next_url=lambda s, u: next_from_selector(
                s, spec.get("content_next") or "", u
            ),
            fetch_html_and_soup=fetch_more,
            delay=self._page_delay,
        )
