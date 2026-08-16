# Author: joelsnl and Anthropic Claude
"""
Generic fallback parser for unsupported novel sites (experimental).

Heuristics instead of site-specific selectors:
- Novel info from OpenGraph / standard meta tags with h1/title fallbacks
- Chapter list: the container element holding the largest cluster of
  chapter-looking links (第N章 / Chapter N / numbered links)
- Chapter content: the element with the highest density of non-link text

Registered LAST (see parsers/__init__.py) so it only handles URLs that
no sites.json config claims.
"""

import re
from typing import List, Optional
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from core.parser import BaseParser, Chapter, NovelInfo

# Link text that looks like a chapter
CHAPTER_TEXT_RE = re.compile(
    r'(第\s*[0-9零一二三四五六七八九十百千两]+\s*[章节話话回]'
    r'|chapter\s*\d+'
    r'|^\s*\d+\s*[.::、]'
    r')',
    re.IGNORECASE
)

# Tags whose contents are never chapter content
NOISE_TAGS = ['script', 'style', 'nav', 'header', 'footer', 'aside', 'form',
              'iframe', 'ins', 'button', 'select', 'noscript']

# Common novel CMS content blocks (WordPress / Madara / chapter wrappers).
# Tried before the density heuristic so known layouts win on unknown hosts.
CMS_CONTENT_SELECTORS = [
    '.reading-content .text-left',
    'div.reading-content',
    'div#chapter-content',
    'div.chapter-content',
    'article.chapter-content',
    'div.entry-content',
    'div.post-content',
]
CMS_CHAPTER_LIST_SELECTORS = [
    'li.wp-manga-chapter a',
    'ul.list-chapter a',
    'div.eplister a',
    'div.chapter-list a',
]


class GenericParser(BaseParser):
    """Best-effort parser for sites without a dedicated implementation."""

    SITE_NAME = "Generic (experimental)"
    SITE_DOMAINS = []  # matches any http(s) URL via can_handle

    def __init__(self):
        super().__init__()
        self.request_delay = 2.0

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return url.lower().startswith(('http://', 'https://'))

    # ------------------------------------------------------------------
    # Novel info
    # ------------------------------------------------------------------

    def get_novel_info(self, url: str) -> NovelInfo:
        soup = self.fetch_page(url)
        return self._parse_novel_info(soup, url)

    def _parse_novel_info(self, soup: BeautifulSoup, url: str) -> NovelInfo:
        def meta(*selectors: str) -> str:
            for sel in selectors:
                el = soup.select_one(sel)
                if el and el.get('content'):
                    return el['content'].strip()
            return ''

        title = meta("meta[property='og:novel:book_name']", "meta[property='og:title']")
        if not title:
            h1 = soup.select_one('h1')
            if h1:
                title = h1.get_text(strip=True)
        if not title and soup.title:
            title = soup.title.get_text(strip=True)

        author = meta("meta[property='og:novel:author']", "meta[name='author']") or "Unknown"
        description = meta("meta[property='og:description']", "meta[name='description']")
        cover_url = meta("meta[property='og:image']") or None
        if cover_url and not cover_url.startswith('http'):
            cover_url = urljoin(url, cover_url)

        return NovelInfo(
            title=title or "Unknown",
            author=author,
            description=description,
            cover_url=cover_url,
            language="zh",
            tags=[],
            source_url=url,
        )

    # ------------------------------------------------------------------
    # Chapter list
    # ------------------------------------------------------------------

    def get_chapter_list(self, url: str) -> List[Chapter]:
        soup = self.fetch_page(url)
        chapters = self._parse_chapter_list(soup, url)
        if not chapters:
            raise ValueError(
                "Generic parser could not find a chapter list on this page. "
                "Make sure the URL is the novel's table-of-contents page."
            )
        return chapters

    def _parse_chapter_list(self, soup: BeautifulSoup, base_url: str) -> List[Chapter]:
        for selector in CMS_CHAPTER_LIST_SELECTORS:
            try:
                links = soup.select(selector)
            except Exception:
                links = []
            if len(links) >= 5:
                chapters = self._links_to_chapters(links, base_url)
                if len(chapters) >= 5:
                    return chapters

        best_container = self._find_chapter_container(soup)
        if best_container is None:
            return []

        return self._links_to_chapters(
            best_container.find_all('a', href=True), base_url
        )

    @staticmethod
    def _links_to_chapters(links, base_url: str) -> List[Chapter]:
        chapters = []
        seen = set()
        for link in links:
            href = (link.get('href') or '').strip()
            title = link.get_text(strip=True)
            if not href or not title or href.startswith(('javascript:', '#', 'mailto:')):
                continue
            absolute = urljoin(base_url, href)
            if absolute in seen:
                continue
            seen.add(absolute)
            chapters.append(Chapter(title=title, url=absolute, index=len(chapters)))
        return chapters

    @staticmethod
    def _find_chapter_container(soup: BeautifulSoup):
        """Find the element containing the largest cluster of chapter-like links."""
        best = None
        best_score = 0

        for container in soup.find_all(['ul', 'ol', 'dl', 'div', 'table']):
            links = container.find_all('a', href=True, recursive=True)
            if len(links) < 5:
                continue
            chapterish = sum(
                1 for a in links if CHAPTER_TEXT_RE.search(a.get_text(strip=True))
            )
            if chapterish < 5:
                continue
            # Prefer the most specific container: penalize wrappers whose
            # chapter links mostly live in a smaller child container
            score = chapterish - 0.1 * (len(links) - chapterish)
            direct_children_with_links = sum(
                1 for child in container.find_all(['ul', 'ol', 'dl', 'div', 'table'], recursive=False)
                if len(child.find_all('a', href=True)) >= 5
            )
            if direct_children_with_links:
                score -= 1  # nudge toward the inner container
            # >= so ties go to the later (deeper, pre-order) element
            if score >= best_score:
                best_score = score
                best = container

        return best

    # ------------------------------------------------------------------
    # Chapter content
    # ------------------------------------------------------------------

    def get_chapter_content(self, chapter: Chapter) -> str:
        soup = self.fetch_page(chapter.url)

        for tag in NOISE_TAGS:
            for el in soup.find_all(tag):
                el.decompose()

        content_el = None
        for selector in CMS_CONTENT_SELECTORS:
            try:
                el = soup.select_one(selector)
            except Exception:
                el = None
            if el and len(el.get_text(strip=True)) >= 200:
                content_el = el
                break
        if content_el is None:
            content_el = self._find_content_element(soup)
        if content_el is None:
            raise ValueError(f"Could not find chapter content at {chapter.url}")

        title_el = soup.select_one('h1')
        chapter_title = title_el.get_text(strip=True) if title_el else chapter.title

        html = f"<h1>{chapter_title}</h1>\n"
        html += str(content_el)
        return html

    @staticmethod
    def _find_content_element(soup: BeautifulSoup):
        """
        Find the element with the highest density of non-link text.
        Web novel chapter text is a big block of text with few links.
        
        Among elements scoring close to the maximum, the deepest one wins
        so we return the actual content block instead of a page wrapper
        that merely contains it.
        """
        candidates = []  # (score, element) in document order
        for el in soup.find_all(['div', 'article', 'section', 'td']):
            text_len = len(el.get_text(strip=True))
            if text_len < 200:
                continue
            link_len = sum(len(a.get_text(strip=True)) for a in el.find_all('a'))
            score = text_len - 2 * link_len
            if score > 0:
                candidates.append((score, el))

        if not candidates:
            return None

        max_score = max(score for score, _ in candidates)
        # Document order is pre-order (ancestors first), so the last
        # near-maximum candidate is the most specific element
        best = None
        for score, el in candidates:
            if score >= max_score * 0.8:
                best = el
        return best
