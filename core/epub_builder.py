# Author: joelsnl and Anthropic Claude
"""
EPUB Builder - Create EPUB files from chapters
Uses ebooklib for EPUB creation.

New features ported from fixTranslate.py:
- Translation verification: counts remaining Chinese chars after translation,
  warns about chapters with significant untranslated content
- Uses multi-pass retry translation (translate_texts_with_retry)
- Reports files_with_remaining_chinese in stats
"""

import os
import io
import re
import time
import hashlib
from typing import List, Optional, Callable, Tuple, Dict
from pathlib import Path

from ebooklib import epub

from core.parser import Chapter, NovelInfo, create_http_session
from core.cleaner import ContentCleaner, is_chinese, count_chinese_chars
from core.utils import format_eta

# Shared session for image downloads (curl_cffi impersonation when available)
_http_session = create_http_session()

# Volume prefix detection for TOC grouping (Chinese and translated forms)
def write_epub_atomic(output_path: str, book) -> None:
    """Write an EPUB to a sibling .tmp file, then replace the destination."""
    dest = Path(output_path)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        if tmp.exists():
            tmp.unlink()
        epub.write_epub(str(tmp), book, {})
        tmp.replace(dest)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


VOLUME_PREFIX_RE = re.compile(
    r'^\s*('
    r'第\s*[0-9零一二三四五六七八九十百千两]+\s*[卷部集]'
    r'|Volume\s*\d+'
    r'|Vol\.?\s*\d+'
    r'|Book\s+\d+'
    r')',
    re.IGNORECASE
)


class EPUBBuilder:
    """Build EPUB files from novel chapters."""
    
    def __init__(self, cleaner: Optional[ContentCleaner] = None, image_cache=None):
        self.cleaner = cleaner or ContentCleaner()
        # Optional NovelCache — cover bytes stay local-only
        self.image_cache = image_cache
    
    def build(
        self,
        novel_info: NovelInfo,
        chapters: List[Chapter],
        output_path: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> str:
        """
        Build an EPUB file from chapters.
        
        Args:
            novel_info: Novel metadata
            chapters: List of chapters with content loaded
            output_path: Where to save the EPUB
            progress_callback: Optional callback(current, total, status)
            
        Returns:
            Path to the created EPUB file
        """
        # Validate we have chapters with content
        valid_chapters = [ch for ch in chapters if ch.content and len(ch.content.strip()) > 0]
        if not valid_chapters:
            raise ValueError("No chapters with content to build EPUB")
        
        print(f"Building EPUB with {len(valid_chapters)} chapters (from {len(chapters)} total)")
        print(f"  Title: {novel_info.title}")
        print(f"  Author: {novel_info.author}")
        
        book = epub.EpubBook()
        
        # Set metadata (md5 keeps the identifier stable across runs,
        # unlike hash() which is randomized per process)
        id_source = novel_info.source_url or novel_info.title
        book.set_identifier(f"novel-{hashlib.md5(id_source.encode('utf-8')).hexdigest()[:16]}")
        book.set_title(novel_info.title)
        book.set_language('en')  # Set to English since we're translating
        book.add_author(novel_info.author)
        
        if novel_info.description:
            book.add_metadata('DC', 'description', novel_info.description)
        
        if novel_info.source_url:
            book.add_metadata('DC', 'source', novel_info.source_url)
        
        for tag in novel_info.tags:
            book.add_metadata('DC', 'subject', tag)
        
        # Add cover image if available
        if novel_info.cover_url:
            try:
                print(f"  Downloading cover from: {novel_info.cover_url}")
                cover_data = self._download_image(novel_info.cover_url)
                if cover_data:
                    print(f"  Cover downloaded: {len(cover_data)} bytes")
                    # Determine image type
                    ext = 'jpg'
                    if novel_info.cover_url.lower().endswith('.png'):
                        ext = 'png'
                    elif novel_info.cover_url.lower().endswith('.gif'):
                        ext = 'gif'
                    
                    book.set_cover(f"cover.{ext}", cover_data)
                    print(f"  Cover added to EPUB as cover.{ext}")
                else:
                    print("  Warning: Cover download returned no data")
            except Exception as e:
                print(f"  Warning: Could not download cover image: {e}")
        
        # Create chapter items
        epub_chapters = []
        spine = ['nav']
        
        total = len(valid_chapters)
        for idx, chapter in enumerate(valid_chapters):
            if progress_callback:
                progress_callback(idx + 1, total, f"Adding chapter: {chapter.title[:30]}...")
            
            # Clean content
            content = chapter.content
            if self.cleaner:
                content = self.cleaner.clean_html(content)
            
            # Validate content isn't empty after cleaning
            if not content or len(content.strip()) < 10:
                print(f"Warning: Chapter {idx} '{chapter.title}' has empty content, using placeholder")
                content = f"<p>Chapter content not available.</p>"
            
            # Create EPUB chapter
            chapter_filename = f"chapter_{idx:04d}.xhtml"
            epub_chapter = epub.EpubHtml(
                title=chapter.title,
                file_name=chapter_filename,
                lang='en'  # Set to English
            )
            
            # Wrap content in proper XHTML
            xhtml_content = self._wrap_xhtml(chapter.title, content)
            epub_chapter.content = xhtml_content.encode('utf-8')
            
            book.add_item(epub_chapter)
            epub_chapters.append(epub_chapter)
            spine.append(epub_chapter)
        
        # Validate we have chapters
        if not epub_chapters:
            raise ValueError("No valid chapters to include in EPUB")
        
        # Add navigation (grouped by volume when titles carry volume prefixes)
        book.toc = self._build_toc(epub_chapters)
        book.spine = spine
        
        # Add required NCX and Nav
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        
        # Add CSS
        css = self._get_default_css()
        nav_css = epub.EpubItem(
            uid="style_nav",
            file_name="style/nav.css",
            media_type="text/css",
            content=css.encode('utf-8')
        )
        book.add_item(nav_css)
        
        # Write EPUB
        if progress_callback:
            progress_callback(total, total, "Writing EPUB file...")
        
        print(f"Writing EPUB to: {output_path}")
        try:
            write_epub_atomic(output_path, book)
            file_size = os.path.getsize(output_path)
            print(f"EPUB written successfully: {file_size} bytes ({file_size/1024:.1f} KB)")
        except Exception as e:
            print(f"Error writing EPUB: {e}")
            raise
        
        return output_path
    
    def _build_toc(self, epub_chapters):
        """
        Build the TOC, grouping chapters under volume sections when most
        chapter titles carry a volume prefix (e.g. 第一卷 / Volume 2).
        Falls back to the plain flat list otherwise.
        """
        labels = []
        for ch in epub_chapters:
            m = VOLUME_PREFIX_RE.match(ch.title or '')
            labels.append(m.group(1).strip() if m else None)
        
        distinct = {label for label in labels if label}
        labeled_count = sum(1 for label in labels if label)
        if len(distinct) < 2 or labeled_count < len(epub_chapters) * 0.6:
            return epub_chapters  # flat TOC
        
        print(f"  Grouping TOC into {len(distinct)} volumes")
        toc = []
        current_label = None
        current_children = []
        
        def flush():
            nonlocal current_children
            if current_children:
                if current_label:
                    toc.append((epub.Section(current_label), current_children))
                else:
                    toc.extend(current_children)
            current_children = []
        
        for ch, label in zip(epub_chapters, labels):
            # Unlabeled chapters stay in the current volume
            effective = label or current_label
            if effective != current_label:
                flush()
                current_label = effective
            current_children.append(ch)
        flush()
        
        return toc
    
    def _download_image(self, url: str) -> Optional[bytes]:
        """Download an image and return bytes using curl_cffi."""
        from core.security import UnsafeURLError, fetch_cover_bytes
        if self.image_cache is not None:
            cached = self.image_cache.get_cover(cover_url=url)
            if cached:
                return cached
        try:
            data = fetch_cover_bytes(_http_session, url, timeout=30)
            if data and self.image_cache is not None:
                self.image_cache.put_cover(data, cover_url=url)
            return data
        except UnsafeURLError as e:
            print(f"  Blocked cover URL: {e}")
            return None
        except Exception as e:
            print(f"  Image download error: {e}")
            return None
    
    def _wrap_xhtml(self, title: str, content: str) -> str:
        """Wrap content in proper XHTML structure."""
        # Escape title for XML
        title = title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head>
    <meta charset="UTF-8"/>
    <title>{title}</title>
    <link rel="stylesheet" type="text/css" href="style/nav.css"/>
</head>
<body>
{content}
</body>
</html>'''
    
    def _get_default_css(self) -> str:
        """Get default CSS for the EPUB."""
        return '''
body {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 1em;
    line-height: 1.6;
    margin: 1em;
    padding: 0;
}

h1 {
    font-size: 1.5em;
    margin-bottom: 1em;
    text-align: center;
}

h2 {
    font-size: 1.3em;
    margin-top: 1.5em;
    margin-bottom: 0.5em;
}

p {
    margin: 0.5em 0;
    text-indent: 2em;
}

.chapter-title {
    text-align: center;
    font-weight: bold;
    margin-bottom: 1em;
}
'''


class TranslatedEPUBBuilder(EPUBBuilder):
    """
    EPUB Builder with translation support.
    Translates title, author, chapter titles, and all content.
    
    New features from fixTranslate.py:
    - Uses multi-pass retry translation for better reliability
    - Translation verification: counts remaining Chinese characters
      after translation and warns about chapters with significant
      untranslated content
    """
    
    def __init__(
        self, 
        cleaner: Optional[ContentCleaner] = None,
        translator=None,
        verify_translation: bool = True,
        image_cache=None,
        polish: bool = False,
    ):
        super().__init__(cleaner, image_cache=image_cache)
        self.translator = translator
        self.verify_translation = verify_translation
        self.polish = bool(polish)
        
        # Track chapters with remaining Chinese after translation
        self.chapters_with_chinese: List[Tuple[str, int]] = []
        self.polish_cancelled = False
    
    def build_with_translation(
        self,
        novel_info: NovelInfo,
        chapters: List[Chapter],
        output_path: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> str:
        """
        Build EPUB with translation.
        Translates: title, author, chapter titles (for TOC), and all content.
        Uses multi-pass retry for better translation reliability.
        """
        if not self.translator:
            # No translator, just build normally
            return self.build(novel_info, chapters, output_path, progress_callback)
        
        self.chapters_with_chinese = []
        self.polish_cancelled = False
        total_steps = len(chapters) * 2  # Clean + Translate phases
        current_step = 0
        
        # Phase 1: Clean all chapters and collect ALL Chinese text for translation
        if progress_callback:
            progress_callback(0, total_steps, "Preparing for translation...")
        
        # Structure: list of (text_type, index, original_text)
        # text_type: 'title', 'author', 'chapter_title', 'content'
        all_texts = []
        
        # Collect novel title for translation
        if is_chinese(novel_info.title):
            all_texts.append(('title', 0, novel_info.title))
            print(f"Will translate title: {novel_info.title}")
        
        # Collect author for translation  
        if is_chinese(novel_info.author):
            all_texts.append(('author', 0, novel_info.author))
            print(f"Will translate author: {novel_info.author}")
        
        # Collect description for translation (ends up in EPUB metadata)
        if novel_info.description and is_chinese(novel_info.description):
            all_texts.append(('description', 0, novel_info.description))
            print("Will translate description")
        
        # Collect all chapter titles for translation (these become the TOC)
        for idx, chapter in enumerate(chapters):
            if is_chinese(chapter.title):
                all_texts.append(('chapter_title', idx, chapter.title))
        
        print(f"Will translate {sum(1 for t in all_texts if t[0] == 'chapter_title')} chapter titles")
        
        # Clean chapters and collect content text
        for idx, chapter in enumerate(chapters):
            current_step += 1
            if progress_callback:
                progress_callback(current_step, total_steps, f"Cleaning: {chapter.title[:30]}...")
            
            # Clean content
            if self.cleaner:
                chapter.content = self.cleaner.clean_html(chapter.content)
            
            # Extract Chinese text segments for translation
            texts = self._extract_text_segments(chapter.content)
            for text in texts:
                if is_chinese(text) and len(text.strip()) > 0:
                    all_texts.append(('content', idx, text))
        
        # Phase 2: Translate all texts in one batch (with multi-pass retry)
        if progress_callback:
            progress_callback(current_step, total_steps, f"Translating {len(all_texts)} segments...")
        
        print(f"Total segments to translate: {len(all_texts)}")
        
        if all_texts:
            texts_to_translate = [t[2] for t in all_texts]
            
            # ETA: clock starts on the first *network* translation this pass,
            # not on cache-hit bursts (library updates reuse most segments).
            net_clock: Optional[float] = None
            requests_at_clock = 0
            retry_pass_num = 0

            def _network_requests() -> int:
                stats = getattr(self.translator, "stats", None) or {}
                try:
                    return int(stats.get("requests", 0) or 0)
                except Exception:
                    return 0

            def translate_progress(completed, total):
                nonlocal current_step, net_clock, requests_at_clock
                if not progress_callback or total <= 0:
                    return

                eta = ""
                requests = _network_requests()
                if requests > 0:
                    if net_clock is None:
                        net_clock = time.monotonic()
                        requests_at_clock = max(0, requests - 1)
                    elapsed = time.monotonic() - net_clock
                    net_done = max(0, requests - requests_at_clock)
                    min_samples = min(10, max(2, total // 20))
                    remaining = total - completed
                    if net_done >= min_samples and remaining > 0 and elapsed > 0:
                        eta = f"  (ETA {format_eta(remaining * (elapsed / net_done))})"

                hits = 0
                stats = getattr(self.translator, "stats", None) or {}
                try:
                    hits = int(stats.get("cache_hits", 0) or 0)
                except Exception:
                    pass
                cache_note = ""
                if hits and completed:
                    cache_note = f" · {min(hits, completed)} cached"

                pct = (completed / total) * len(chapters)
                if retry_pass_num > 0:
                    status = (
                        f"Retry pass {retry_pass_num}: {completed}/{total}"
                        f"{cache_note}{eta}"
                    )
                else:
                    status = f"Translating: {completed}/{total}{cache_note}{eta}"
                progress_callback(int(len(chapters) + pct), total_steps, status)

            def on_retry_pass(pass_number, remaining, total_segments, cooldown):
                nonlocal net_clock, retry_pass_num, requests_at_clock
                retry_pass_num = pass_number
                net_clock = None
                requests_at_clock = _network_requests()
                if not progress_callback:
                    return
                if cooldown > 0:
                    progress_callback(
                        current_step,
                        total_steps,
                        f"Retry pass {pass_number}: cooling down {int(cooldown)}s "
                        f"({remaining} left)...",
                    )
                else:
                    progress_callback(
                        current_step,
                        total_steps,
                        f"Retry pass {pass_number}: retrying {remaining} segments...",
                    )
            
            # Use multi-pass retry if available, fall back to single-pass
            if hasattr(self.translator, 'translate_texts_with_retry'):
                translated = self.translator.translate_texts_with_retry(
                    texts_to_translate,
                    translate_progress,
                    is_chinese_fn=lambda t: is_chinese(t),
                    count_chinese_fn=lambda t: count_chinese_chars(t),
                    pass_callback=on_retry_pass,
                )
            else:
                translated = self.translator.translate_texts(texts_to_translate, translate_progress)

            # Cancel during Chinese→English: do not write a half-translated EPUB.
            if getattr(self.translator, "_cancel_requested", False):
                from core.download_runner import DownloadCancelled
                raise DownloadCancelled()

            if self.polish and hasattr(self.translator, 'polish_texts'):
                polish_start = time.monotonic()

                def polish_progress(completed, total):
                    if not progress_callback or total <= 0:
                        return
                    eta = ""
                    if completed > 0 and completed < total:
                        elapsed = time.monotonic() - polish_start
                        if elapsed > 0:
                            eta = (
                                "  (ETA "
                                f"{format_eta((total - completed) * (elapsed / completed))})"
                            )
                    progress_callback(
                        int(len(chapters) * 1.5),
                        total_steps,
                        f"Polishing English: {completed}/{total}{eta}",
                    )

                print(f"Polishing {len(translated)} segments (KEEP/REPLACE, local LLM)...")
                translated = self.translator.polish_texts(
                    translated, polish_progress
                )
                if getattr(self.translator, "_cancel_requested", False):
                    self.polish_cancelled = True
                    print(
                        "Polish cancelled — packaging EPUB with machine translation "
                        "(already-polished spans kept)."
                    )
            
            # Apply translations back. Content translations are grouped per
            # chapter and applied at the text-node level (not raw string
            # replacement), so HTML entities in the source can't cause silent
            # mismatches and translated text gets properly escaped.
            content_pairs: Dict[int, List[Tuple[str, str]]] = {}
            for i, (text_type, idx, original) in enumerate(all_texts):
                translated_text = translated[i] if i < len(translated) else ''
                if text_type == 'content':
                    content_pairs.setdefault(idx, []).append((original, translated_text))
                    continue
                if translated_text and translated_text != original:
                    if text_type == 'title':
                        print(f"Translated title: {novel_info.title} -> {translated_text}")
                        novel_info.title = translated_text
                    elif text_type == 'author':
                        print(f"Translated author: {novel_info.author} -> {translated_text}")
                        novel_info.author = translated_text
                    elif text_type == 'description':
                        novel_info.description = translated_text
                    elif text_type == 'chapter_title':
                        # This is crucial - translating chapter titles fixes the TOC!
                        chapters[idx].title = translated_text
            
            for idx, pairs in content_pairs.items():
                chapters[idx].content = self._apply_content_translations(
                    chapters[idx].content, pairs
                )
        
        # Phase 2.5: Translation verification (from fixTranslate.py)
        if self.verify_translation:
            self._verify_translations(chapters)
        
        # Validate chapters have content
        for idx, chapter in enumerate(chapters):
            if not chapter.content or len(chapter.content.strip()) < 10:
                print(f"Warning: Chapter {idx} '{chapter.title}' has empty/minimal content")
                if not chapter.content:
                    chapter.content = "<p>Chapter content not available.</p>"
        
        # Phase 3: Build EPUB with translated metadata and chapters
        print(f"Building EPUB with translated content...")
        print(f"  Final title: {novel_info.title}")
        print(f"  Final author: {novel_info.author}")
        return self.build(novel_info, chapters, output_path, progress_callback)
    
    def _verify_translations(self, chapters: List[Chapter]):
        """
        Verify translation quality by checking for remaining Chinese content.
        From fixTranslate.py - warns about chapters with significant untranslated text.
        """
        self.chapters_with_chinese = []
        
        for idx, chapter in enumerate(chapters):
            if not chapter.content:
                continue
            
            remaining = count_chinese_chars(chapter.content)
            if remaining > 50:  # More than 50 Chinese chars = significant
                self.chapters_with_chinese.append((chapter.title, remaining))
        
        if self.chapters_with_chinese:
            print(f"\n  ⚠ Warning: {len(self.chapters_with_chinese)} chapters still have significant Chinese content:")
            for title, count in self.chapters_with_chinese[:10]:
                display_title = title[:40] + '...' if len(title) > 40 else title
                print(f"    - {display_title}: {count} Chinese chars")
            if len(self.chapters_with_chinese) > 10:
                print(f"    ... and {len(self.chapters_with_chinese) - 10} more")
            print("  These may need manual re-translation or the API failed silently.")
    
    @staticmethod
    def _find_translatable_nodes(soup) -> list:
        """
        Find text nodes eligible for translation, in document order.
        Used by both extraction and application so the two always align.
        """
        nodes = []
        for element in soup.find_all(string=True):
            text = str(element).strip()
            if text and len(text) > 1 and re.search(r'[\u4e00-\u9fff]', text):
                nodes.append(element)
        return nodes
    
    def _extract_text_segments(self, html: str) -> List[str]:
        """Extract text segments from HTML for translation."""
        from bs4 import BeautifulSoup
        
        soup = BeautifulSoup(html, 'lxml')
        return [str(node).strip() for node in self._find_translatable_nodes(soup)]
    
    def _apply_content_translations(
        self, html: str, pairs: List[Tuple[str, str]]
    ) -> str:
        """
        Replace translatable text nodes with their translations.
        
        `pairs` is a list of (original, translated) in the same document order
        that _extract_text_segments produced. Replacement happens on parsed
        text nodes; BeautifulSoup escapes special characters on serialization,
        so translations containing <, > or & can't break the XHTML.
        """
        from bs4 import BeautifulSoup
        
        soup = BeautifulSoup(html, 'lxml')
        nodes = self._find_translatable_nodes(soup)
        
        j = 0
        for node in nodes:
            if j >= len(pairs):
                break
            original, translated = pairs[j]
            if str(node).strip() != original:
                # Safety net - shouldn't happen since the same HTML is parsed
                # by both extraction and application
                continue
            j += 1
            if translated and translated != original:
                node.replace_with(translated)
        
        # Return only the body contents so the result can be re-wrapped
        # by _wrap_xhtml without nested <html>/<body> tags
        if soup.body is not None:
            return ''.join(str(child) for child in soup.body.children)
        return str(soup)
    
    def get_translation_warnings(self) -> List[Tuple[str, int]]:
        """
        Get list of chapters that still have significant Chinese content.
        Returns list of (chapter_title, chinese_char_count) tuples.
        """
        return self.chapters_with_chinese.copy()
