# Author: joelsnl and Anthropic Claude
"""
CSS-selector parsers generated from WebToEpub site specs.

Each spec is a hostname + the CSS selectors WebToEpub uses for that site.
Complex JS (paginated AJAX TOCs, chapter walkers) is not ported — those
sites still get the content selector, and fall back to GenericParser for
the chapter list.

Registered after dedicated parsers and before GenericParser.
"""

from __future__ import annotations

from typing import List
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core.parser import Chapter, NovelInfo, register_parser
from parsers.generic import GenericParser


class SelectorParser(GenericParser):
    """GenericParser + optional CSS selectors from a WebToEpub-style spec."""

    SITE_NAME = ""
    SITE_DOMAINS: List[str] = []
    SPEC: dict = {}

    @classmethod
    def can_handle(cls, url: str) -> bool:
        u = url.lower()
        return any(d.lower() in u for d in cls.SITE_DOMAINS)

    def _first(self, soup: BeautifulSoup, selector: str):
        if not selector:
            return None
        try:
            return soup.select_one(selector)
        except Exception:
            return None

    def _text(self, soup: BeautifulSoup, selector: str) -> str:
        el = self._first(soup, selector)
        if el is None:
            return ""
        return el.get_text(strip=True)

    def get_novel_info(self, url: str) -> NovelInfo:
        soup = self.fetch_page(url)
        info = self._parse_novel_info(soup, url)
        spec = self.SPEC
        title = self._text(soup, spec.get("title", ""))
        if title:
            info.title = title
        author = self._text(soup, spec.get("author", ""))
        if author:
            info.author = author
        desc = self._text(soup, spec.get("description", ""))
        if desc:
            info.description = desc
        cover_sel = spec.get("cover") or ""
        if cover_sel:
            el = self._first(soup, cover_sel)
            if el:
                if el.name == "img":
                    src = el.get("src") or el.get("data-src") or ""
                else:
                    img = el.find("img")
                    src = (img.get("src") or img.get("data-src") or "") if img else ""
                if src:
                    info.cover_url = urljoin(url, src)
        lang = spec.get("language")
        if lang:
            info.language = lang
        return info

    def get_chapter_list(self, url: str) -> List[Chapter]:
        soup = self.fetch_page(url)
        spec = self.SPEC
        toc_link = spec.get("toc_link")
        if toc_link:
            a = self._first(soup, toc_link)
            href = a.get("href") if a else None
            if href:
                soup = self.fetch_page(urljoin(url, href))
        selector = spec.get("chapter_list")
        if selector:
            chapters = self._chapters_from_selector(soup, url, selector)
            if chapters:
                if spec.get("reverse"):
                    chapters.reverse()
                    for i, ch in enumerate(chapters):
                        ch.index = i
                return chapters
        chapters = self._parse_chapter_list(soup, url)
        if not chapters:
            raise ValueError(
                f"{self.SITE_NAME} parser could not find a chapter list. "
                "Make sure the URL is the novel's table-of-contents page."
            )
        return chapters

    def _chapters_from_selector(
        self, soup: BeautifulSoup, base_url: str, selector: str
    ) -> List[Chapter]:
        if not selector or selector.strip().lower() in {
            "a", "div", "span", "li", "p", "ul", "ol", "body", "html",
        }:
            return []
        try:
            nodes = soup.select(selector)
        except Exception:
            return []
        links = []
        for node in nodes:
            if node.name == "a" and node.get("href"):
                links.append(node)
            else:
                links.extend(node.find_all("a", href=True))
        chapters: List[Chapter] = []
        seen = set()
        for link in links:
            href = (link.get("href") or "").strip()
            title = link.get_text(strip=True)
            if not href or not title or href.startswith(("javascript:", "#", "mailto:")):
                continue
            absolute = urljoin(base_url, href)
            if absolute in seen:
                continue
            seen.add(absolute)
            chapters.append(Chapter(title=title, url=absolute, index=len(chapters)))
        return chapters

    def get_chapter_content(self, chapter: Chapter) -> str:
        soup = self.fetch_page(chapter.url)
        spec = self.SPEC
        content_el = None
        for sel in spec.get("content") or []:
            content_el = self._first(soup, sel)
            if content_el is not None:
                break
        if content_el is None:
            return super().get_chapter_content(chapter)

        remove = spec.get("remove")
        if remove:
            try:
                for el in content_el.select(remove):
                    el.decompose()
            except Exception:
                pass

        title_el = self._first(soup, spec.get("chapter_title", ""))
        chapter_title = (
            title_el.get_text(strip=True) if title_el else chapter.title
        )
        return f"<h1>{chapter_title}</h1>\n{content_el}"


def register_webtoepub_parsers() -> int:
    """Instantiate one parser class per site spec. Returns how many registered."""
    from parsers.webtoepub_sites import SITES

    count = 0
    for spec in SITES:
        domains = list(spec.get("domains") or [])
        if not domains or not spec.get("content"):
            continue
        name = spec.get("name") or domains[0]

        class _P(SelectorParser):
            SITE_NAME = name
            SITE_DOMAINS = domains
            SPEC = spec

        _P.__name__ = "Wte_" + "".join(c if c.isalnum() else "_" for c in name)[:40]
        _P.__qualname__ = _P.__name__
        register_parser(_P)
        count += 1
    return count
