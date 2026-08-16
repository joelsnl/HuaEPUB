"""Offline parser tests using HTML fixtures (no network)."""

import pytest
from bs4 import BeautifulSoup

import parsers  # noqa: F401 — register SiteConfigParser + GenericParser
from core.parser import get_parser_for_url, _parser_registry
from parsers.config import SiteConfigParser
from parsers.generic import GenericParser

from conftest import load_fixture


def soup_of(fixture_name: str) -> BeautifulSoup:
    return BeautifulSoup(load_fixture(fixture_name), 'lxml')


def parser_for(url: str) -> SiteConfigParser:
    return SiteConfigParser.for_url(url)


class TestRegistry:
    def test_configured_sites_win_over_generic(self):
        twkan = get_parser_for_url('https://twkan.com/book/76222.html')
        shuba = get_parser_for_url('https://69shuba.com/book/123.htm')
        uu = get_parser_for_url('https://uukanshu.cc/book/22432/')
        assert isinstance(twkan, SiteConfigParser)
        assert isinstance(shuba, SiteConfigParser)
        assert isinstance(uu, SiteConfigParser)
        assert twkan.SITE_NAME == 'twkan.com'
        assert shuba.SITE_NAME == '69shuba.com'
        assert uu.SITE_NAME == 'uukanshu.cc'

    def test_generic_is_fallback_for_unknown_sites(self):
        parser = get_parser_for_url('https://some-random-novel-site.example/book/1')
        assert type(parser) is GenericParser

    def test_generic_is_last_in_registry(self):
        assert _parser_registry[-1] is GenericParser
        assert _parser_registry[0] is SiteConfigParser

    def test_json_site_wins_over_generic(self):
        parser = get_parser_for_url('https://101kks.com/book/1')
        assert isinstance(parser, SiteConfigParser)
        assert type(parser) is not GenericParser
        assert parser.SITE_NAME == '101kks.com'

    def test_non_http_not_handled(self):
        assert get_parser_for_url('ftp://example.com/x') is None

    def test_www_subdomain_matches_site_config(self):
        parser = get_parser_for_url('https://www.twkan.com/book/1.html')
        assert isinstance(parser, SiteConfigParser)
        assert parser.SITE_NAME == 'twkan.com'

    def test_query_string_does_not_steal_site_config(self):
        parser = get_parser_for_url('https://evil.example/?next=https://twkan.com/book/1')
        assert type(parser) is GenericParser

    def test_suffix_lookalike_host_does_not_match(self):
        parser = get_parser_for_url('https://nottwkan.com/book/1')
        assert type(parser) is GenericParser


class TestTwkan:
    def test_parse_novel_info(self):
        parser = parser_for('https://twkan.com/book/76222.html')
        info = parser.parse_novel_info(
            soup_of('twkan_book.html'), 'https://twkan.com/book/76222.html'
        )
        assert info.title == '測試小說'
        assert info.author == '測試作者'
        assert '測試小說' in info.description or info.description
        assert info.cover_url.endswith('76222s.jpg')
        assert '玄幻' in info.tags

    def test_parse_chapter_list(self):
        parser = parser_for('https://twkan.com/book/76222.html')
        chapters = parser.parse_chapter_list(
            BeautifulSoup(load_fixture('twkan_chapterlist.html'), 'lxml'),
            'https://twkan.com/book/76222.html',
        )
        assert len(chapters) == 3  # the /book/ link is filtered out
        assert chapters[0].title == '第1章 開始'
        assert chapters[0].url == 'https://twkan.com/txt/76222/1000001'
        assert chapters[2].url == 'https://twkan.com/txt/76222/1000003'

    def test_book_id_from_url(self):
        parser = parser_for('https://twkan.com/book/76222.html')
        assert parser._book_id('https://twkan.com/book/76222.html') == '76222'
        assert parser._book_id('https://twkan.com/txt/76222/1000001') == '76222'


class TestShuba69:
    def test_parse_novel_info(self):
        parser = parser_for('https://69shuba.com/book/123.htm')
        info = parser.parse_novel_info(
            soup_of('shuba69_book.html'), 'https://69shuba.com/book/123.htm'
        )
        assert info.title == '测试书'
        assert info.author == '作者名'
        assert info.description == '这是一本测试书的简介。'
        assert info.cover_url.startswith('http')
        assert '玄幻小说' in info.tags
        assert parser._encoding == 'gb18030'
        assert parser.request_delay == 1.5

    def test_parse_chapter_list_reverses_order(self):
        parser = parser_for('https://69shuba.com/book/123.htm')
        chapters = parser.parse_chapter_list(
            soup_of('shuba69_toc.html'), 'https://69shuba.com/book/123/'
        )
        assert [c.title for c in chapters] == ['第1章 开头', '第2章 中间', '第3章 结尾']


class TestUUKanshu:
    def test_parse_novel_info(self):
        parser = parser_for('https://uukanshu.cc/book/22432/')
        info = parser.parse_novel_info(
            soup_of('uukanshu_book.html'), 'https://uukanshu.cc/book/22432/'
        )
        assert info.title == '繁體測試書'
        assert info.author == '繁體作者'
        assert info.language == 'zh-Hant'

    def test_parse_chapter_list(self):
        parser = parser_for('https://uukanshu.cc/book/22432/')
        chapters = parser.parse_chapter_list(
            soup_of('uukanshu_book.html'), 'https://uukanshu.cc/book/22432/'
        )
        assert len(chapters) == 3
        assert chapters[0].title == '第一章 起點'
        assert chapters[0].url.startswith('http')


class TestGeneric:
    def test_parse_novel_info_from_meta(self):
        parser = GenericParser()
        info = parser._parse_novel_info(
            soup_of('generic_toc.html'), 'https://unknown.example/novel/1/'
        )
        assert info.title == '未知站點小說'
        assert info.author == '無名氏'
        assert info.cover_url == 'https://unknown.example/cover.jpg'  # made absolute

    def test_chapter_list_ignores_nav_links(self):
        parser = GenericParser()
        chapters = parser._parse_chapter_list(
            soup_of('generic_toc.html'), 'https://unknown.example/novel/1/'
        )
        assert len(chapters) == 6
        assert chapters[0].title == '第1章 陌生的开始'
        assert chapters[0].url == 'https://unknown.example/novel/1/ch1.html'
        assert all('login' not in c.url for c in chapters)

    def test_content_extraction_finds_main_text(self):
        soup = soup_of('generic_chapter.html')
        for tag in ('nav', 'footer'):
            for el in soup.find_all(tag):
                el.decompose()
        el = GenericParser._find_content_element(soup)
        assert el is not None
        text = el.get_text()
        assert '正文的第一段' in text
        assert '关于我们' not in text

    def test_content_too_short_returns_none(self):
        soup = BeautifulSoup('<div>short</div>', 'lxml')
        assert GenericParser._find_content_element(soup) is None


class TestSelectorParser:
    def _parser(self):
        return SiteConfigParser(spec={
            "name": "demo.test",
            "domains": ["demo.test"],
            "content": ["div.chapter-body"],
            "chapter_list": "ul.toc a",
            "title": "h1.book",
            "author": "span.author",
            "chapter_title": "h2.ch",
            "remove": ".ad",
        })

    def test_chapter_list_from_selector(self):
        html = """
        <ul class="toc">
          <li><a href="/c/1">Ch 1</a></li>
          <li><a href="/c/2">Ch 2</a></li>
        </ul>
        """
        chapters = self._parser()._chapters_from_selector(
            BeautifulSoup(html, 'lxml'), 'https://demo.test/book', 'ul.toc a'
        )
        assert [c.title for c in chapters] == ['Ch 1', 'Ch 2']
        assert chapters[0].url == 'https://demo.test/c/1'

    def test_container_selector_collects_all_links(self):
        html = """
        <div id="chapterlist">
          <a href="/c/1">One</a>
          <a href="/c/2">Two</a>
        </div>
        """
        chapters = self._parser()._chapters_from_selector(
            BeautifulSoup(html, 'lxml'), 'https://demo.test/book', '#chapterlist'
        )
        assert len(chapters) == 2

    def test_generic_selector_a_is_ignored(self):
        html = '<div><a href="/c/1">One</a></div>'
        chapters = self._parser()._chapters_from_selector(
            BeautifulSoup(html, 'lxml'), 'https://demo.test/book', 'a'
        )
        assert chapters == []
