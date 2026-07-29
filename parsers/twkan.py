# Author: joelsnl and Anthropic Claude
"""
Parser for twkan.com (台灣小說網)
Based on WebToEpub TwkanParser.js
"""

import re
import time
import concurrent.futures
from typing import List, Optional, Tuple
from bs4 import BeautifulSoup

from core.parser import BaseParser, Chapter, NovelInfo, register_parser


@register_parser
class TwkanParser(BaseParser):
    """Parser for twkan.com Chinese novel site."""
    
    SITE_NAME = "twkan.com"
    SITE_DOMAINS = ["twkan.com"]
    
    def __init__(self):
        super().__init__()
        # Higher delay to avoid 429 errors (like WebToEpub does for strict sites)
        self.request_delay = 3.0  # 3 seconds between requests
    
    def _extract_book_id(self, url: str) -> Optional[str]:
        """Extract book ID from URL."""
        # Matches: /book/76222.html, /book/76222/index.html, /txt/76222/12345
        match = re.search(r'/(?:book|txt)/(\d+)', url)
        return match.group(1) if match else None
    
    def fetch_all_parallel(self, url: str) -> Tuple[NovelInfo, List[Chapter]]:
        """
        Fetch novel info and chapter list in parallel.
        This is faster than calling get_novel_info() and get_chapter_list() separately.
        
        Returns:
            Tuple of (NovelInfo, List[Chapter])
        """
        book_id = self._extract_book_id(url)
        if not book_id:
            raise ValueError(f"Could not extract book ID from URL: {url}")
        
        ajax_url = f"https://twkan.com/ajax_novels/chapterlist/{book_id}.html"
        
        # Fetch both pages in parallel
        main_soup = None
        ajax_html = None
        
        def fetch_main():
            nonlocal main_soup
            main_soup = self.fetch_page(url)
        
        def fetch_ajax():
            nonlocal ajax_html
            ajax_html = self.fetch_html(ajax_url)
        
        print(f"  Fetching main page and chapter list in parallel...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(fetch_main),
                executor.submit(fetch_ajax)
            ]
            # Wait for both to complete
            concurrent.futures.wait(futures)
            
            # Check for exceptions
            for future in futures:
                if future.exception():
                    raise future.exception()
        
        print(f"  Both fetches complete, parsing...")
        
        # Parse novel info from main page
        novel_info = self._parse_novel_info(main_soup, url)
        
        # Parse chapter list from AJAX response
        chapters = self._parse_chapter_list(ajax_html)
        
        return novel_info, chapters
    
    def _parse_novel_info(self, soup: BeautifulSoup, url: str) -> NovelInfo:
        """Parse novel info from already-fetched soup."""
        # Try meta tags first (most reliable)
        title = ""
        author = "Unknown"
        description = ""
        cover_url = None
        tags = []
        
        # og:title
        meta_title = soup.select_one("meta[property='og:title']")
        if meta_title:
            title = meta_title.get('content', '')
        
        # Fallback: page title or h1
        if not title:
            h1 = soup.select_one(".booknav2 h1 a, .booknav2 h1, h1")
            if h1:
                title = h1.get_text(strip=True)
        
        # og:novel:author
        meta_author = soup.select_one("meta[property='og:novel:author']")
        if meta_author:
            author = meta_author.get('content', '')
        else:
            # Fallback: author link
            author_el = soup.select_one(".booknav2 p a[href*='/author/']")
            if author_el:
                author = author_el.get_text(strip=True)
        
        # og:description
        meta_desc = soup.select_one("meta[property='og:description']")
        if meta_desc:
            description = meta_desc.get('content', '')
        else:
            # Fallback: navtxt
            desc_el = soup.select_one(".navtxt p")
            if desc_el:
                description = desc_el.get_text(strip=True)
        
        # og:image (cover)
        meta_image = soup.select_one("meta[property='og:image']")
        if meta_image:
            cover_url = meta_image.get('content', '')
        else:
            # Fallback: book image
            cover_el = soup.select_one(".bookimg2 img, .bookimg img")
            if cover_el:
                cover_url = cover_el.get('src', '')
        
        # If still no cover, try to construct from book ID
        if not cover_url:
            book_id = self._extract_book_id(url)
            if book_id:
                prefix = book_id[:2] if len(book_id) >= 2 else book_id
                cover_url = f"https://twkan.com/files/article/image/{prefix}/{book_id}/{book_id}s.jpg"
        
        # Category/tags
        meta_category = soup.select_one("meta[property='og:novel:category']")
        if meta_category:
            tags.append(meta_category.get('content', ''))
        
        return NovelInfo(
            title=title,
            author=author,
            description=description,
            cover_url=cover_url,
            language="zh",
            tags=tags,
            source_url=url
        )
    
    def _parse_chapter_list(self, html: str) -> List[Chapter]:
        """Parse chapter list from AJAX HTML response."""
        soup = BeautifulSoup(html, 'lxml')
        
        chapters = []
        for idx, link in enumerate(soup.select("ul li a")):
            href = link.get('href', '')
            if '/txt/' not in href:
                continue
            
            # Make sure URL is absolute
            if not href.startswith('http'):
                href = f"https://twkan.com{href}"
            
            title = link.get_text(strip=True)
            
            chapters.append(Chapter(
                title=title,
                url=href,
                index=idx
            ))
        
        return chapters
    
    def get_novel_info(self, url: str) -> NovelInfo:
        """Extract novel metadata from main page."""
        soup = self.fetch_page(url)
        return self._parse_novel_info(soup, url)
    
    def get_chapter_list(self, url: str) -> List[Chapter]:
        """
        Get full chapter list using AJAX endpoint.
        This bypasses the "Load More" button entirely.
        """
        book_id = self._extract_book_id(url)
        if not book_id:
            raise ValueError(f"Could not extract book ID from URL: {url}")
        
        # First visit the main page (sets cookies, passes any checks)
        print(f"  Visiting main page first...")
        self.fetch_page(url)
        
        # Fetch full chapter list from AJAX endpoint
        ajax_url = f"https://twkan.com/ajax_novels/chapterlist/{book_id}.html"
        print(f"  Fetching chapters from: {ajax_url}")
        
        html = self.fetch_html(ajax_url)
        return self._parse_chapter_list(html)
    
    def get_chapter_content(self, chapter: Chapter) -> str:
        """Fetch and extract content for a single chapter."""
        soup = self.fetch_page(chapter.url)
        
        # Content selector: #txtcontent0
        content_el = soup.select_one("#txtcontent0")
        if not content_el:
            raise ValueError(f"Could not find chapter content at {chapter.url}")
        
        # Get chapter title
        title_el = soup.select_one(".txtnav h1, #container .txtnav h1, h1")
        chapter_title = title_el.get_text(strip=True) if title_el else chapter.title
        
        # Remove unwanted elements
        for selector in ['script', '.ads', '.ad', '.txtad', '.txtcenter', 
                         'ins.adsbygoogle', '.advertisement']:
            for el in content_el.select(selector):
                el.decompose()
        
        # Build HTML content
        html = f"<h1>{chapter_title}</h1>\n"
        html += str(content_el)
        
        return html


# For testing
if __name__ == "__main__":
    parser = TwkanParser()
    
    test_url = "https://twkan.com/book/76222.html"
    
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
