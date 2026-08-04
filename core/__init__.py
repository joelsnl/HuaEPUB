# Author: joelsnl and Anthropic Claude
"""
Core modules for HuaEPUB
"""

from core.parser import BaseParser, Chapter, NovelInfo, get_parser_for_url, get_supported_sites, cleanup_browser
from core.cleaner import ContentCleaner, is_chinese, count_chinese_chars
from core.translator import GoogleTranslator
from core.epub_builder import EPUBBuilder, TranslatedEPUBBuilder

__all__ = [
    'BaseParser', 'Chapter', 'NovelInfo',
    'get_parser_for_url', 'get_supported_sites', 'cleanup_browser',
    'ContentCleaner', 'is_chinese', 'count_chinese_chars',
    'GoogleTranslator',
    'EPUBBuilder', 'TranslatedEPUBBuilder',
]
