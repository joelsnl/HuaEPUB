# Author: joelsnl and Anthropic Claude
"""
Parser for 69shuba.com / 69shu.com (六九书吧)
Based on WebToEpub 69shuParser.js

Note: This site uses GB18030 encoding for text.
"""

import re
import time
from typing import List, Optional, Tuple
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from core.parser import BaseParser, Chapter, NovelInfo, register_parser


@register_parser
class Shuba69Parser(BaseParser):
    """Parser for 69shuba.com and related 69shu sites."""
    
    SITE_NAME = "69shuba.com"
    SITE_DOMAINS = ["69shuba.com", "69shu.com", "69shuba.cx", "69shu.pro", "69shuba.pro"]
    
    def __init__(self):
        super().__init__()
        # Minimum delay between requests (site is sensitive to rapid requests)
        self.request_delay = 1.5
        # Store the base URL for Referer header
        self._base_url = "https://www.69shuba.com"
        self._last_page_url = None
        
        # Set up session headers for anti-bot bypass
        self._setup_session_headers()
    
    def _setup_session_headers(self):
        """Configure session with headers that work for 69shuba."""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Referer': self._base_url,
        }
        
        # Update session headers based on client type
        try:
            # For curl_cffi
            self.session.headers.update(headers)
        except AttributeError:
            # For requests
            self.session.headers.update(headers)
    
    def _set_referer(self, referer: str):
        """Update the Referer header in session."""
        try:
            self.session.headers['Referer'] = referer
        except:
            pass
    
    def _extract_book_id(self, url: str) -> Optional[str]:
        """Extract book ID from URL."""
        match = re.search(r'/(?:book|txt)/(\d+)', url)
        return match.group(1) if match else None
    
    def _fetch_with_encoding(self, url: str, referer: str = None, retries: int = 3) -> BeautifulSoup:
        """
        Fetch page with GB18030 encoding and proper headers.
        """
        last_error = None
        
        # Set referer before request
        if referer:
            self._set_referer(referer)
        elif self._last_page_url:
            self._set_referer(self._last_page_url)
        else:
            self._set_referer(self._base_url)
        
        for attempt in range(retries):
            try:
                # Add small delay between requests
                if attempt > 0:
                    time.sleep(self.request_delay)
                
                response = self.session.get(url, timeout=30)
                
                if response.status_code == 429:
                    wait = self.rate_limit_delays[min(attempt, len(self.rate_limit_delays)-1)]
                    print(f"  Rate limited (429). Waiting {wait}s...")
                    time.sleep(wait)
                    continue
                
                if response.status_code == 403:
                    print(f"  Got 403 on attempt {attempt + 1}, trying different referer...")
                    # Try with the book index page as referer
                    book_id = self._extract_book_id(url)
                    if book_id:
                        self._set_referer(f"{self._base_url}/book/{book_id}/")
                    time.sleep(2)
                    continue
                
                response.raise_for_status()
                
                # Store this URL as referer for next request
                self._last_page_url = url
                
                # Decode with GB18030 encoding
                content = response.content.decode('gb18030', errors='replace')
                return BeautifulSoup(content, 'lxml')
                
            except Exception as e:
                last_error = e
                print(f"  Attempt {attempt + 1}/{retries} failed: {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** (attempt + 1))
        
        raise last_error
    
    def fetch_all_parallel(self, url: str) -> Tuple[NovelInfo, List[Chapter]]:
        """
        Fetch novel info and chapter list.
        """
        # Extract base URL for referer
        if '69shuba' in url or '69shu' in url:
            self._base_url = '/'.join(url.split('/')[:3])
            self._setup_session_headers()
        
        # First get the main page to find TOC URL
        print(f"  Fetching main page...")
        main_soup = self._fetch_with_encoding(url, referer=self._base_url)
        
        # Find TOC URL (the "more" button)
        toc_link = main_soup.select_one("a.more-btn")
        if not toc_link:
            raise ValueError("Could not find chapter list link (a.more-btn)")
        
        toc_url = toc_link.get('href', '')
        if not toc_url.startswith('http'):
            toc_url = urljoin(url, toc_url)
        
        print(f"  Fetching TOC from: {toc_url}")
        toc_soup = self._fetch_with_encoding(toc_url, referer=url)
        
        # Store TOC URL - this will be used as referer for chapter downloads
        self._last_page_url = toc_url
        
        # Parse novel info from main page
        novel_info = self._parse_novel_info(main_soup, url)
        
        # Parse chapter list from TOC page
        chapters = self._parse_chapter_list(toc_soup, toc_url)
        
        return novel_info, chapters
    
    def _parse_novel_info(self, soup: BeautifulSoup, url: str) -> NovelInfo:
        """Parse novel info from main page."""
        title = ""
        author = "Unknown"
        description = ""
        cover_url = None
        tags = []
        
        # Title: div.booknav2 h1
        title_el = soup.select_one("div.booknav2 h1")
        if title_el:
            title = title_el.get_text(strip=True)
        
        # Author: second link in .booknav2
        author_links = soup.select(".booknav2 a")
        if len(author_links) >= 2:
            author = author_links[1].get_text(strip=True)
        
        # Cover image: first img in div.bookbox
        cover_el = soup.select_one("div.bookbox img")
        if cover_el:
            cover_url = cover_el.get('src', '')
            if cover_url and not cover_url.startswith('http'):
                cover_url = urljoin(url, cover_url)
        
        # Description: div.navtxt or similar
        desc_el = soup.select_one(".navtxt p, .bookintro")
        if desc_el:
            description = desc_el.get_text(strip=True)
        
        # Try to get category/tags
        category_el = soup.select_one(".booknav2 a[href*='sort']")
        if category_el:
            tags.append(category_el.get_text(strip=True))
        
        return NovelInfo(
            title=title,
            author=author,
            description=description,
            cover_url=cover_url,
            language="zh",
            tags=tags,
            source_url=url
        )
    
    def _parse_chapter_list(self, soup: BeautifulSoup, base_url: str) -> List[Chapter]:
        """Parse chapter list from TOC page."""
        chapters = []
        
        # Chapter links are in #catalog ul
        menu = soup.select_one("#catalog ul")
        if not menu:
            menu = soup.select_one(".catalog ul, .mulu ul, #list ul")
        
        if not menu:
            print("  Warning: Could not find chapter list container")
            return chapters
        
        for idx, link in enumerate(menu.select("a")):
            href = link.get('href', '')
            if not href:
                continue
            
            if not href.startswith('http'):
                href = urljoin(base_url, href)
            
            title = link.get_text(strip=True)
            if not title:
                continue
            
            chapters.append(Chapter(
                title=title,
                url=href,
                index=idx
            ))
        
        # 69shu lists chapters in reverse order (newest first), so reverse them
        chapters.reverse()
        
        # Re-index after reversing
        for idx, chapter in enumerate(chapters):
            chapter.index = idx
        
        print(f"  Found {len(chapters)} chapters")
        return chapters
    
    def get_novel_info(self, url: str) -> NovelInfo:
        """Extract novel metadata from main page."""
        self._base_url = '/'.join(url.split('/')[:3])
        self._setup_session_headers()
        soup = self._fetch_with_encoding(url)
        return self._parse_novel_info(soup, url)
    
    def get_chapter_list(self, url: str) -> List[Chapter]:
        """Get full chapter list."""
        self._base_url = '/'.join(url.split('/')[:3])
        self._setup_session_headers()
        
        print(f"  Fetching main page...")
        main_soup = self._fetch_with_encoding(url)
        
        toc_link = main_soup.select_one("a.more-btn")
        if not toc_link:
            raise ValueError("Could not find chapter list link")
        
        toc_url = toc_link.get('href', '')
        if not toc_url.startswith('http'):
            toc_url = urljoin(url, toc_url)
        
        print(f"  Fetching TOC from: {toc_url}")
        toc_soup = self._fetch_with_encoding(toc_url, referer=url)
        self._last_page_url = toc_url
        
        return self._parse_chapter_list(toc_soup, toc_url)
    
    def get_chapter_content(self, chapter: Chapter) -> str:
        """Fetch and extract content for a single chapter."""
        # Use stored referer (TOC page or previous chapter)
        referer = self._last_page_url
        if not referer:
            # Fallback: construct TOC URL from chapter URL
            book_id = self._extract_book_id(chapter.url)
            if book_id:
                referer = f"{self._base_url}/book/{book_id}/"
            else:
                referer = self._base_url
        
        # Note: the download loop in the app already sleeps request_delay
        # between chapters, so no extra delay here.
        soup = self._fetch_with_encoding(chapter.url, referer=referer)
        
        # Content is in div.txtnav
        content_el = soup.select_one("div.txtnav")
        if not content_el:
            raise ValueError(f"Could not find chapter content at {chapter.url}")
        
        # Remove unwanted elements
        for selector in ['.txtinfo', '#txtright', '.bottom-ad', 'script', 
                         '.ads', '.ad', 'ins.adsbygoogle']:
            for el in content_el.select(selector):
                el.decompose()
        
        # Get chapter title from page
        title_el = soup.select_one("h1, .txtnav h1")
        chapter_title = title_el.get_text(strip=True) if title_el else chapter.title
        
        # Build HTML content
        html = f"<h1>{chapter_title}</h1>\n"
        html += str(content_el)
        
        return html
