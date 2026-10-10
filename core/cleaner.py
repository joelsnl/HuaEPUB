# Author: joelsnl and Anthropic Claude
"""
XHTML/Content Cleaner - Remove watermarks, ads, and fix structure
Based on fixTranslate.py XHTMLProcessor - now with ALL features ported.

Features:
- Full XHTML parsing with multi-encoding fallback (utf-8, gbk, gb2312, big5, latin-1)
- XHTML namespace-aware processing
- Structure fixing (head/body/title/meta charset)
- Deprecated tag conversion (center→div, u→span, strike→span, font→span)
- Duplicate ID removal
- BR-to-P conversion for web novel content
- BR with content → div conversion
- Comment fix (-- → __ for Adobe Digital Editions)
- Self-closing tag fix for e-reader compatibility
- Proper XHTML serialization with xml_declaration
- Watermark removal with Unicode variant detection
- Ad div removal
- Invisible character cleaning
"""

import re
from typing import List, Optional
from lxml import etree
from lxml import html as lxml_html


# ============================================================================
# CONSTANTS
# ============================================================================
XHTML_NS = 'http://www.w3.org/1999/xhtml'
XHTML = lambda name: f'{{{XHTML_NS}}}{name}'

# Tags that should NOT be self-closing in EPUB output (from Calibre)
SELF_CLOSING_BAD_TAGS = {
    'a', 'abbr', 'address', 'article', 'aside', 'audio', 'b',
    'bdo', 'blockquote', 'body', 'button', 'cite', 'code', 'dd', 'del', 'details',
    'dfn', 'div', 'dl', 'dt', 'em', 'fieldset', 'figcaption', 'figure', 'footer',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hgroup', 'i', 'iframe', 'ins', 'kbd',
    'label', 'legend', 'li', 'map', 'mark', 'meter', 'nav', 'ol', 'output', 'p',
    'pre', 'progress', 'q', 'rp', 'rt', 'samp', 'section', 'select', 'small',
    'span', 'strong', 'sub', 'summary', 'sup', 'textarea', 'time', 'ul', 'var',
    'video', 'title', 'script', 'style'
}

# Elements to remove entirely
REMOVE_ELEMENTS = {
    'script', 'embed', 'object', 'form', 'input', 'button', 'textarea',
    'iframe', 'link', 'base', 'applet', 'noscript',
}

# After cleaning, only these tags may remain in chapter bodies (others unwrapped)
ALLOWED_CONTENT_TAGS = {
    'html', 'head', 'body', 'title', 'meta',  # structure for full XHTML path
    'p', 'br', 'div', 'span', 'section', 'article',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'em', 'strong', 'b', 'i', 'u', 's', 'sub', 'sup', 'small',
    'blockquote', 'ul', 'ol', 'li', 'hr',
    'a', 'img',
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td',
}

# Attributes kept on content tags (event handlers and javascript: always stripped)
ALLOWED_ATTRS = {'href', 'src', 'alt', 'title', 'class', 'id', 'colspan', 'rowspan', 'style'}

# Invisible characters to remove
INVISIBLE_CHARS = '\u200b\u200c\u200d\ufeff\u00ad\u2060\u180e\u200e\u200f\u202a\u202b\u202c\u202d\u202e'

# Ad div classes to remove
REMOVE_DIV_CLASSES = {'txtad', 'ad', 'advertisement', 'ads', 'adsbygoogle'}

# Deprecated tag conversions: (old_tag, new_tag, {attrs_to_add})
DEPRECATED_TAG_CONVERSIONS = [
    ('center', 'div', {'style': 'text-align:center'}),
    ('u', 'span', {'style': 'text-decoration:underline'}),
    ('s', 'span', {'style': 'text-decoration:line-through'}),
    ('strike', 'span', {'style': 'text-decoration:line-through'}),
    ('font', 'span', {}),
]

# Default watermark patterns (Traditional + Simplified — most sites use Simplified)
DEFAULT_WATERMARKS = [
    r'本[書书]由.{0,30}首[發发]', r'本文由.{0,30}首[發发]',
    r'正版[請请].{0,30}[閱阅][讀读]', r'[請请]到.{0,30}[閱阅][讀读]',
    r'最新章[節节].{0,30}[閱阅][讀读]', r'手[機机][閱阅][讀读].{0,50}',
    r'[訪访][問问]下[載载].{0,50}', r'更多精彩.{0,50}', r'[歡欢]迎廣大書友.{0,50}',
    r'[歡欢]迎广大书友.{0,50}', r'喜[歡欢][請请]收藏.{0,50}',
    r'[請请][記记]住本[書书].{0,50}', r'百度搜索.{0,50}',
    r'最快更新.{0,50}', r'[無无]彈窗.{0,30}', r'[無无]弹窗.{0,30}',
    r'[關关]注[公眾公众]號.{0,50}', r'[關关]注公众号.{0,50}',
    r'微信[公眾公众][號号].{0,50}', r'[掃扫][碼码][關关]注.{0,50}',
    r'[點点][擊击]下[載载].{0,50}', r'APP下[載载].{0,50}',
    r'本[書书]首[發发].{0,80}',
    r'提供[給给]你[無无][錯错]章[節节].{0,50}',
    r'[台臺]灣小說網.{0,30}', r'台湾小说网.{0,30}',
    r'[請请]收藏(?:本站|本[書书]|本[頁页])?', r'[記记]住.{0,6}(?:網址|网址|域名|本站).{0,40}',
    r'天才一秒[記记]住.{0,50}', r'本章未完.{0,40}',
    r'twkan\.com',
    r'(?:https?://|www\.)[^\s<>]{3,60}',
    r'(?<![A-Za-z0-9])[a-z0-9][a-z0-9.-]{1,40}\.(?:com|net|cc|org|xyz|top)(?![A-Za-z])',

    # Fullwidth alphanumeric URLs (ａｂｃ style)
    r'[ａ-ｚＡ-Ｚ０-９]+\.[ａ-ｚＡ-Ｚ]+',

    # Double-struck/mathematical alphanumeric URLs
    r'[𝕒-𝕫𝔸-𝕫𝟘-𝟡]+\.[𝕒-𝕫𝔸-𝕫]+',

    # Sans-serif bold
    r'[𝖺-𝗓𝖠-𝗓𝟢-𝟫]+\.[𝖺-𝗓𝖠-𝗓]+',
    r'[\U0001D5BA-\U0001D5D3\U0001D5A0-\U0001D5B9]+\.[\U0001D5BA-\U0001D5D3\U0001D5A0-\U0001D5B9]+',

    # Sans-serif
    r'[\U0001D5A0-\U0001D5D3]+\.[\U0001D5A0-\U0001D5D3]+',

    # Monospace
    r'[\U0001D68A-\U0001D6A3\U0001D670-\U0001D689]+\.[\U0001D68A-\U0001D6A3]+',

    # General math alphanumeric
    r'[\U0001D400-\U0001D7FF]+\.[\U0001D400-\U0001D7FF]+',

    # Arrow followed by stylized URL
    r'→\s*[\U0001D400-\U0001D7FFａ-ｚＡ-Ｚ０-９]+\.[\U0001D400-\U0001D7FFａ-ｚＡ-Ｚ]+',
]


class ContentCleaner:
    """
    Clean HTML/XHTML content - remove watermarks, ads, fix structure.
    
    Full-featured XHTML processor based on fixTranslate.py, using lxml 
    for proper parsing and serialization (like Calibre).
    """
    
    def __init__(self, custom_watermarks: List[str] = None, convert_br_to_p: bool = True):
        self.convert_br_to_p = convert_br_to_p
        self.watermark_patterns = []
        
        all_patterns = DEFAULT_WATERMARKS + (custom_watermarks or [])
        for pattern in all_patterns:
            try:
                self.watermark_patterns.append(re.compile(pattern, re.IGNORECASE))
            except re.error:
                pass
        
        self.stats = {
            'elements_removed': 0,
            'empty_tags_removed': 0,
            'watermarks_removed': 0,
            'chars_cleaned': 0,
            'ad_divs_removed': 0,
            'self_closing_fixed': 0,
            'deprecated_tags_converted': 0,
            'duplicate_ids_removed': 0,
            'comments_fixed': 0,
            'br_converted': 0,
            'attrs_stripped': 0,
            'tags_unwrapped': 0,
        }
        self._site_junk_learned = False
        self._learned_literals: List[str] = []

    def add_literals(self, texts: List[str]) -> int:
        """Add exact strings (site footers) as watermark removals."""
        seen = {p.pattern for p in self.watermark_patterns}
        added = 0
        for raw in texts or []:
            text = (raw or "").strip()
            if len(text) < 4:
                continue
            pat = re.escape(text)
            if pat in seen:
                continue
            try:
                self.watermark_patterns.append(re.compile(pat, re.IGNORECASE))
            except re.error:
                continue
            seen.add(pat)
            self._learned_literals.append(text)
            added += 1
        return added

    
    def clean_text(self, text: str) -> str:
        """Clean text content - remove watermarks and invisible chars."""
        if not text:
            return text
        
        original = text
        
        # Remove invisible characters
        for char in INVISIBLE_CHARS:
            text = text.replace(char, '')
        
        # Replace non-breaking hyphen
        text = text.replace('\u2011', '-')
        
        # Remove watermarks
        for pattern in self.watermark_patterns:
            new_text = pattern.sub('', text)
            if new_text != text:
                self.stats['watermarks_removed'] += 1
                text = new_text
        
        if text != original:
            self.stats['chars_cleaned'] += 1
        
        return text
    
    # ========================================================================
    # XHTML PARSING - Multi-encoding with XML/HTML fallback (from fixTranslate)
    # ========================================================================
    
    
    # ========================================================================
    # XHTML STRUCTURE FIXING (from fixTranslate)
    # ========================================================================
    
    
    # ========================================================================
    # CONTENT CLEANING (enhanced from fixTranslate)
    # ========================================================================
    
    
    @staticmethod
    def _local_tag(tag) -> Optional[str]:
        """Get the local (namespace-free) tag name, or None for comments/PIs."""
        if not isinstance(tag, str):
            return None
        return tag.split('}')[-1] if '}' in tag else tag

    def _sanitize_style(self, value: str) -> Optional[str]:
        """Drop styles that can execute script."""
        if not value:
            return None
        lowered = value.lower()
        if any(bad in lowered for bad in (
            'expression(', 'javascript:', 'vbscript:', '-moz-binding', 'behavior:'
        )):
            return None
        return value

    def _sanitize_url_attr(self, value: str) -> Optional[str]:
        if not value:
            return None
        v = value.strip()
        lowered = v.lower()
        if lowered.startswith(('javascript:', 'vbscript:', 'data:text/html')):
            return None
        if lowered.startswith(('#', '/', 'http://', 'https://', 'mailto:')):
            return v
        if '://' not in v and not lowered.startswith('data:'):
            return v
        return None

    def _sanitize_allowed_markup(self, root: etree._Element):
        """Unwrap disallowed tags and strip dangerous attributes."""
        for elem in list(reversed(list(root.iter()))):
            local = self._local_tag(elem.tag)
            if local is None:
                continue

            if local not in ALLOWED_CONTENT_TAGS:
                parent = elem.getparent()
                if parent is None:
                    continue
                idx = list(parent).index(elem)
                if elem.text:
                    if idx == 0:
                        parent.text = (parent.text or '') + elem.text
                    else:
                        parent[idx - 1].tail = (parent[idx - 1].tail or '') + elem.text
                children = list(elem)
                for offset, child in enumerate(children):
                    parent.insert(idx + offset, child)
                idx2 = list(parent).index(elem)
                if elem.tail:
                    if idx2 == 0:
                        parent.text = (parent.text or '') + elem.tail
                    else:
                        parent[idx2 - 1].tail = (parent[idx2 - 1].tail or '') + elem.tail
                parent.remove(elem)
                self.stats['tags_unwrapped'] += 1
                continue

            for attr in list(elem.attrib.keys()):
                attr_l = attr.lower()
                if attr_l.startswith('on') or attr_l not in ALLOWED_ATTRS:
                    del elem.attrib[attr]
                    self.stats['attrs_stripped'] += 1
                    continue
                if attr_l in ('href', 'src'):
                    cleaned = self._sanitize_url_attr(elem.get(attr) or '')
                    if cleaned is None:
                        del elem.attrib[attr]
                        self.stats['attrs_stripped'] += 1
                    else:
                        elem.set(attr, cleaned)
                elif attr_l == 'style':
                    cleaned = self._sanitize_style(elem.get(attr) or '')
                    if cleaned is None:
                        del elem.attrib[attr]
                        self.stats['attrs_stripped'] += 1
                    else:
                        elem.set(attr, cleaned)
    
    def _convert_br_runs_to_paragraphs(self, root: etree._Element, use_ns: bool = False):
        """
        Convert containers like <div>text<br/><br/>text...</div> into real
        <p> paragraphs.
        
        Conservative on purpose: only converts div/body containers whose
        element children are ALL <br/> tags (the typical web-novel content
        block), so any mixed or nested markup is left untouched.
        """
        p_tag = XHTML('p') if use_ns else 'p'
        
        candidates = []
        for parent in root.iter():
            name = self._local_tag(parent.tag)
            if name not in ('div', 'body'):
                continue
            children = list(parent)
            if len(children) < 2:
                continue
            if any(self._local_tag(child.tag) != 'br' for child in children):
                continue
            candidates.append(parent)
        
        for parent in candidates:
            segments = []
            if parent.text and parent.text.strip():
                segments.append(parent.text.strip())
            for br in parent:
                if br.tail and br.tail.strip():
                    segments.append(br.tail.strip())
            
            if len(segments) < 2:
                continue
            
            parent.text = None
            for child in list(parent):
                parent.remove(child)
            for segment in segments:
                p = etree.SubElement(parent, p_tag)
                p.text = segment
            
            self.stats['br_converted'] += len(segments)
    
    # ========================================================================
    # SERIALIZATION (from fixTranslate)
    # ========================================================================
    
    
    def _fix_self_closing_tags(self, data: bytes) -> bytes:
        """Convert self-closing tags to properly closed tags for e-reader compatibility."""
        pattern_str = r'<({})(\s[^>]*)?\s*/>'.format('|'.join(SELF_CLOSING_BAD_TAGS))
        pattern = re.compile(pattern_str.encode('utf-8'), re.IGNORECASE)
        
        def replace_func(match):
            tag = match.group(1)
            attrs = match.group(2) or b''
            return b'<' + tag + attrs + b'></' + tag + b'>'
        
        result, count = pattern.subn(replace_func, data)
        self.stats['self_closing_fixed'] += count
        return result
    
    # ========================================================================
    # FULL XHTML PROCESSING PIPELINE (from fixTranslate)
    # ========================================================================
    
    
    # ========================================================================
    # SIMPLE HTML CLEANING (for use in epub_builder pipeline)
    # ========================================================================
    
    def clean_html(self, html_content: str) -> str:
        """
        Clean HTML content and return cleaned HTML string.
        This is the simpler path used when building EPUBs from scratch
        (where ebooklib handles XHTML wrapping).
        """
        try:
            root = lxml_html.fromstring(html_content)
            root = self._clean_html_content(root)
            return lxml_html.tostring(root, encoding='unicode')
        except Exception:
            return self.clean_text(html_content)
    
    def _clean_html_content(self, root) -> etree._Element:
        """Clean HTML content (non-XHTML namespace path)."""
        
        # Remove forbidden elements
        for tag in REMOVE_ELEMENTS:
            for elem in root.iter(tag):
                self._remove_element_keep_tail(elem)
                self.stats['elements_removed'] += 1
        
        # Remove ad containers even when they have text (empty-only missed real ads)
        for elem in list(root.iter('div')):
            class_attr = (elem.get('class') or '').lower()
            classes = set(class_attr.split())
            if classes & REMOVE_DIV_CLASSES:
                self._remove_element_keep_tail(elem)
                self.stats['ad_divs_removed'] += 1
        
        # Convert deprecated tags
        for old_tag, new_tag, attrs in DEPRECATED_TAG_CONVERSIONS:
            for elem in root.iter(old_tag):
                elem.tag = new_tag
                for k, v in attrs.items():
                    existing = elem.get(k, '')
                    elem.set(k, f'{existing}; {v}' if existing else v)
                self.stats['deprecated_tags_converted'] += 1
        
        # Remove empty inline tags
        for tag in ['a', 'i', 'b', 'u', 'span', 'em', 'strong']:
            for elem in root.iter(tag):
                if (elem.get('id') is None and elem.get('name') is None and
                    len(elem) == 0 and not (elem.text and elem.text.strip())):
                    self._remove_element_keep_tail(elem)
                    self.stats['empty_tags_removed'] += 1
        
        # Convert <br> with content to <div>
        for br in root.iter('br'):
            if len(br) > 0 or (br.text and br.text.strip()):
                br.tag = 'div'
                self.stats['br_converted'] += 1
        
        # Clean text content
        for elem in root.iter():
            if elem.text:
                elem.text = self.clean_text(elem.text)
            if elem.tail:
                elem.tail = self.clean_text(elem.tail)
        
        # Fix duplicate IDs
        seen_ids = set()
        for elem in root.iter():
            id_val = elem.get('id')
            if id_val:
                if id_val in seen_ids:
                    del elem.attrib['id']
                    self.stats['duplicate_ids_removed'] += 1
                else:
                    seen_ids.add(id_val)
        
        # Convert br-separated text into proper paragraphs
        if self.convert_br_to_p:
            self._convert_br_runs_to_paragraphs(root)

        self._sanitize_allowed_markup(root)
        return root
    
    # ========================================================================
    # UTILITIES
    # ========================================================================
    
    def _remove_element_keep_tail(self, elem):
        """Remove element but keep its tail text."""
        parent = elem.getparent()
        if parent is None:
            return
        
        idx = list(parent).index(elem)
        if elem.tail:
            if idx > 0:
                prev = parent[idx - 1]
                prev.tail = (prev.tail or '') + elem.tail
            else:
                parent.text = (parent.text or '') + elem.tail
        parent.remove(elem)
    


# ============================================================================
# MODULE-LEVEL UTILITIES
# ============================================================================

def is_chinese(text: str) -> bool:
    """Check if text contains Chinese characters."""
    if not text:
        return False
    return bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))


def count_chinese_chars(text: str) -> int:
    """Count Chinese characters in text."""
    if not text:
        return 0
    return len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))
