# Author: joelsnl and Anthropic Claude
"""
Base Parser class and Chapter data structure
All site-specific parsers inherit from BaseParser
"""

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from bs4 import BeautifulSoup

# Try curl_cffi first (best TLS fingerprinting, lightweight)
HTTP_CLIENT = None

try:
    from curl_cffi.requests import Session as CurlSession
    HTTP_CLIENT = "curl_cffi"
    print("Using curl_cffi (Chrome TLS fingerprinting)")
except ImportError:
    pass

# Fallback to requests
if not HTTP_CLIENT:
    import requests
    HTTP_CLIENT = "requests"
    print("Warning: curl_cffi not installed. Run: pip install curl_cffi")


def create_http_session():
    """
    Create an HTTP session with Chrome TLS impersonation when curl_cffi
    is available, falling back to a plain requests session.
    Shared by parsers, the app (cover preview), and the EPUB builder.
    """
    try:
        from core.utils import sanitize_runtime_env
        sanitize_runtime_env()
    except Exception:
        pass
    if HTTP_CLIENT == "curl_cffi":
        return CurlSession(impersonate="chrome120")
    import requests
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    })
    return session


@dataclass
class Chapter:
    """Represents a single chapter."""
    title: str
    url: str
    content: str = ""
    index: int = 0
    # True when sites.json content selectors missed and GenericParser guessed.
    used_heuristic: bool = False
    
    def __str__(self):
        return f"Chapter {self.index}: {self.title}"


@dataclass 
class NovelInfo:
    """Metadata about a novel."""
    title: str
    author: str = "Unknown"
    description: str = ""
    cover_url: Optional[str] = None
    language: str = "zh"
    tags: List[str] = field(default_factory=list)
    source_url: str = ""


class BaseParser(ABC):
    """
    Base class for site parsers.

    Configured hosts use SiteConfigParser (parsers/sites.json). Unknown
    hosts fall through to GenericParser.
    """
    
    # Subclasses should set these
    SITE_NAME = "Unknown"
    SITE_DOMAINS = []  # e.g., ["twkan.com", "www.twkan.com"]
    
    def __init__(self):
        self.session = create_http_session()
        
        # Rate limiting — default 2 seconds between requests
        self.request_delay = 2.0
        # 429 retry delays in seconds
        self.rate_limit_delays = [15, 30, 60, 120]
        self._encoding: Optional[str] = None
        self._referer: Optional[str] = None
    
    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Check if this parser can handle the given URL."""
        for domain in cls.SITE_DOMAINS:
            if domain in url.lower():
                return True
        return False
    
    @abstractmethod
    def get_novel_info(self, url: str) -> NovelInfo:
        pass
    
    @abstractmethod
    def get_chapter_list(self, url: str) -> List[Chapter]:
        pass
    
    @abstractmethod
    def get_chapter_content(self, chapter: Chapter) -> str:
        pass
    
    def _apply_referer(self, referer: Optional[str] = None):
        ref = referer or self._referer
        if not ref:
            return
        try:
            self.session.headers["Referer"] = ref
        except Exception:
            pass

    def _html_from_response(self, response) -> str:
        if self._encoding:
            return response.content.decode(self._encoding, errors="replace")
        return response.text

    def fetch_page(self, url: str, retries: int = 3) -> BeautifulSoup:
        """
        Fetch a page and return BeautifulSoup object.
        Handles 429 errors with longer waits.
        """
        from core.security import UnsafeURLError, safe_http_request

        last_error = None
        rate_limit_retry = 0  # Track 429 retries separately
        
        for attempt in range(retries):
            try:
                self._apply_referer()
                response = safe_http_request(
                    self.session, "GET", url, allow_http=True, timeout=30
                )
                
                # Check for 429 specifically
                if response.status_code == 429:
                    if rate_limit_retry < len(self.rate_limit_delays):
                        wait = self.rate_limit_delays[rate_limit_retry]
                        print(f"  Rate limited (429). Waiting {wait}s before retry...")
                        time.sleep(wait)
                        rate_limit_retry += 1
                        continue  # Don't count as a regular retry
                    else:
                        response.raise_for_status()
                
                response.raise_for_status()
                self._referer = url
                return BeautifulSoup(self._html_from_response(response), 'lxml')

            except UnsafeURLError as e:
                raise ValueError(f"Blocked URL: {e}") from e
            except Exception as e:
                last_error = e
                error_str = str(e)
                
                # Check if it's a 429 error from exception
                if '429' in error_str:
                    if rate_limit_retry < len(self.rate_limit_delays):
                        wait = self.rate_limit_delays[rate_limit_retry]
                        print(f"  Rate limited (429). Waiting {wait}s before retry...")
                        time.sleep(wait)
                        rate_limit_retry += 1
                        continue
                
                print(f"  Attempt {attempt + 1}/{retries} failed: {e}")
                if attempt < retries - 1:
                    wait = 2 ** (attempt + 1)
                    print(f"  Retrying in {wait}s...")
                    time.sleep(wait)
        
        raise last_error
    
    def fetch_html(self, url: str, retries: int = 3) -> str:
        """Fetch page and return raw HTML string with 429 handling."""
        from core.security import UnsafeURLError, safe_http_request

        last_error = None
        rate_limit_retry = 0
        
        for attempt in range(retries):
            try:
                self._apply_referer()
                response = safe_http_request(
                    self.session, "GET", url, allow_http=True, timeout=30
                )
                
                # Check for 429 specifically
                if response.status_code == 429:
                    if rate_limit_retry < len(self.rate_limit_delays):
                        wait = self.rate_limit_delays[rate_limit_retry]
                        print(f"  Rate limited (429). Waiting {wait}s before retry...")
                        time.sleep(wait)
                        rate_limit_retry += 1
                        continue
                
                response.raise_for_status()
                self._referer = url
                return self._html_from_response(response)

            except UnsafeURLError as e:
                raise ValueError(f"Blocked URL: {e}") from e
            except Exception as e:
                last_error = e
                error_str = str(e)
                
                if '429' in error_str:
                    if rate_limit_retry < len(self.rate_limit_delays):
                        wait = self.rate_limit_delays[rate_limit_retry]
                        print(f"  Rate limited (429). Waiting {wait}s before retry...")
                        time.sleep(wait)
                        rate_limit_retry += 1
                        continue
                
                print(f"  Attempt {attempt + 1}/{retries} failed: {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** (attempt + 1))
        
        raise last_error


# Registry of all parsers
_parser_registry: List[type] = []


def register_parser(parser_class: type):
    """Decorator to register a parser class."""
    _parser_registry.append(parser_class)
    return parser_class


def get_parser_for_url(url: str) -> Optional[BaseParser]:
    """Find and instantiate the appropriate parser for a URL."""
    for parser_class in _parser_registry:
        if parser_class.can_handle(url):
            factory = getattr(parser_class, "for_url", None)
            if callable(factory):
                return factory(url)
            return parser_class()
    return None


def get_supported_sites() -> List[str]:
    """Get list of all supported site names."""
    names: List[str] = []
    for p in _parser_registry:
        extra = getattr(p, "configured_site_names", None)
        if callable(extra):
            names.extend(extra())
        else:
            names.append(p.SITE_NAME)
    return names


def cleanup_browser():
    """Placeholder for compatibility."""
    pass
