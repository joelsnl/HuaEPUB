# Author: joelsnl and Anthropic Claude
"""
Parser for uukanshu.cc (UU看書繁體免費小說閱讀網)
Traditional Chinese novel site with direct chapter listing on the main page.
"""

import re
from typing import List, Optional, Tuple
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from core.parser import BaseParser, Chapter, NovelInfo, register_parser


@register_parser
class UUKanshuParser(BaseParser):
    """Parser for uukanshu.cc Traditional Chinese novel site."""

    SITE_NAME = "uukanshu.cc"
    SITE_DOMAINS = ["uukanshu.cc"]

    BASE_URL = "https://uukanshu.cc"

    def __init__(self):
        super().__init__()
        self.request_delay = 2.0

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_book_id(url: str) -> Optional[str]:
        """Extract book ID from URL like /book/22432/ or /book/22432/13417006.html"""
        match = re.search(r'/book/(\d+)', url)
        return match.group(1) if match else None

    @staticmethod
    def _book_index_url(book_id: str) -> str:
        return f"https://uukanshu.cc/book/{book_id}/"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_all_parallel(self, url: str) -> Tuple[NovelInfo, List[Chapter]]:
        """
        Fetch novel info and chapter list from the single TOC page.
        uukanshu.cc embeds all chapters directly on the book index page,
        so no AJAX or second request is needed.
        """
        book_id = self._extract_book_id(url)
        if not book_id:
            raise ValueError(f"Could not extract book ID from URL: {url}")

        index_url = self._book_index_url(book_id)
        print(f"  Fetching book page: {index_url}")
        soup = self.fetch_page(index_url)

        novel_info = self._parse_novel_info(soup, index_url)
        chapters = self._parse_chapter_list(soup, book_id)

        return novel_info, chapters

    def get_novel_info(self, url: str) -> NovelInfo:
        """Extract novel metadata from the book index page."""
        soup = self.fetch_page(url)
        return self._parse_novel_info(soup, url)

    def get_chapter_list(self, url: str) -> List[Chapter]:
        """Get the full chapter list from the book index page."""
        book_id = self._extract_book_id(url)
        if not book_id:
            raise ValueError(f"Could not extract book ID from URL: {url}")

        soup = self.fetch_page(url)
        return self._parse_chapter_list(soup, book_id)

    def get_chapter_content(self, chapter: Chapter) -> str:
        """Fetch and extract content for a single chapter.
        
        Note: the download loop in the app already sleeps request_delay
        between chapters, so no extra delay here.
        """
        soup = self.fetch_page(chapter.url)

        # Content lives inside div.readcotent (note: site typo, not "readcontent")
        content_el = soup.select_one("div.readcotent")
        if not content_el:
            # Fallback selectors
            content_el = soup.select_one("div.readcontent, div.content, #bookContent")
        if not content_el:
            raise ValueError(f"Could not find chapter content at {chapter.url}")

        # Remove scripts, ads, and other junk
        for selector in ['script', 'ins.adsbygoogle', '.ads', '.ad',
                         'iframe', 'div[style*="text-align:center"]']:
            for el in content_el.select(selector):
                el.decompose()

        # Chapter title from the page <h1>
        title_el = soup.select_one("div.read h1, h1.pt10, h1")
        chapter_title = title_el.get_text(strip=True) if title_el else chapter.title

        html = f"<h1>{chapter_title}</h1>\n"
        html += str(content_el)
        return html

    # ------------------------------------------------------------------
    # Internal parsing helpers
    # ------------------------------------------------------------------

    def _parse_novel_info(self, soup: BeautifulSoup, url: str) -> NovelInfo:
        """Parse novel metadata from the book index page."""
        title = ""
        author = "Unknown"
        description = ""
        cover_url = None
        tags = []

        # --- Title ---
        meta_title = soup.select_one("meta[property='og:novel:book_name']")
        if meta_title:
            title = meta_title.get('content', '')
        if not title:
            meta_title = soup.select_one("meta[property='og:title']")
            if meta_title:
                title = meta_title.get('content', '')
        if not title:
            h1 = soup.select_one("h1.booktitle, .bookinfo h1, h1")
            if h1:
                title = h1.get_text(strip=True)

        # --- Author ---
        meta_author = soup.select_one("meta[property='og:novel:author']")
        if meta_author:
            author = meta_author.get('content', '')
        if not author or author == "Unknown":
            author_el = soup.select_one(".booktag a.red[href*='author']")
            if author_el:
                author = author_el.get_text(strip=True)

        # --- Description ---
        meta_desc = soup.select_one("meta[property='og:description']")
        if meta_desc:
            description = meta_desc.get('content', '')
        if not description:
            desc_el = soup.select_one("p.bookintro")
            if desc_el:
                description = desc_el.get_text(strip=True)

        # --- Cover ---
        meta_image = soup.select_one("meta[property='og:image']")
        if meta_image:
            cover_url = meta_image.get('content', '')
        if not cover_url:
            cover_el = soup.select_one(".bookcover img.thumbnail, .bookinfo img.thumbnail")
            if cover_el:
                cover_url = cover_el.get('src', '')

        # --- Tags / Category ---
        meta_cat = soup.select_one("meta[property='og:novel:category']")
        if meta_cat:
            cat = meta_cat.get('content', '')
            if cat:
                tags.append(cat)

        return NovelInfo(
            title=title,
            author=author,
            description=description,
            cover_url=cover_url,
            language="zh-Hant",  # Traditional Chinese
            tags=tags,
            source_url=url,
        )

    def _parse_chapter_list(self, soup: BeautifulSoup, book_id: str) -> List[Chapter]:
        """
        Parse the chapter list embedded in the book index page.
        Chapters are in <dl class="book chapterlist"> → <dd> → <a>.
        """
        chapters = []

        # All chapter links sit inside dd elements under the chapterlist
        chapter_links = soup.select("dl.chapterlist dd a")
        if not chapter_links:
            # Broader fallback
            chapter_links = soup.select("#list-chapterAll dd a")

        for idx, link in enumerate(chapter_links):
            href = link.get('href', '')
            title = link.get_text(strip=True)
            if not href or not title:
                continue

            # Make URL absolute
            if not href.startswith('http'):
                href = urljoin(self.BASE_URL, href)

            chapters.append(Chapter(
                title=title,
                url=href,
                index=idx,
            ))

        print(f"  Found {len(chapters)} chapters")
        return chapters


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------
if __name__ == "__main__":
    parser = UUKanshuParser()

    test_url = "https://uukanshu.cc/book/22432/"

    print("Fetching novel info...")
    info = parser.get_novel_info(test_url)
    print(f"Title: {info.title}")
    print(f"Author: {info.author}")
    print(f"Description: {info.description[:100]}...")
    print(f"Cover: {info.cover_url}")

    print("\nFetching chapter list...")
    chapters = parser.get_chapter_list(test_url)
    print(f"Found {len(chapters)} chapters")

    if chapters:
        print(f"\nFirst chapter: {chapters[0]}")
        print(f"Last chapter: {chapters[-1]}")

        print("\nFetching first chapter content...")
        content = parser.get_chapter_content(chapters[0])
        print(f"Content length: {len(content)} chars")
        print(f"Preview: {content[:200]}...")
