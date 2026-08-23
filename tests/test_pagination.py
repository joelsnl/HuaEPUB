"""Offline tests for TOC / chapter-body pagination helpers."""

from bs4 import BeautifulSoup

from core.parser import Chapter
from parsers.pagination import (
    next_content_page_url,
    next_from_rel,
    next_from_selector,
    walk_content_pages,
    walk_list_pages,
)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


class TestNextLinks:
    def test_selector_and_rel(self):
        soup = _soup(
            '<a class="more" href="/toc?p=2">下一页</a>'
            '<a rel="next" href="/toc?p=3">next</a>'
        )
        base = "https://demo.test/toc"
        assert next_from_selector(soup, "a.more", base) == "https://demo.test/toc?p=2"
        assert next_from_rel(soup, base) == "https://demo.test/toc?p=3"

    def test_content_next_follows_page_not_chapter(self):
        page = _soup(
            '<div id="c">page 1</div>'
            '<a href="/ch1?p=2">下一页</a>'
            '<a href="/ch2">下一章</a>'
        )
        assert next_content_page_url(page, "https://demo.test/ch1") == (
            "https://demo.test/ch1?p=2"
        )

    def test_content_next_skips_rel_next_chapter(self):
        page = _soup(
            '<div id="c">page 1</div>'
            '<a rel="next" href="/ch2">下一章</a>'
        )
        assert next_content_page_url(page, "https://demo.test/ch1") == ""

    def test_content_next_allows_rel_next_page(self):
        page = _soup(
            '<div id="c">page 1</div>'
            '<a rel="next" href="/ch1?p=2">下一页</a>'
        )
        assert next_content_page_url(page, "https://demo.test/ch1") == (
            "https://demo.test/ch1?p=2"
        )


class TestWalkers:
    def test_walk_list_pages_dedupes_and_caps(self):
        pages = {
            "https://demo.test/toc": _soup(
                '<a href="/c/1">第1章</a><a class="n" href="/toc?p=2">next</a>'
            ),
            "https://demo.test/toc?p=2": _soup(
                '<a href="/c/2">第2章</a><a class="n" href="/toc">loop</a>'
            ),
        }
        fetched = []

        def parse(soup, url):
            return [
                Chapter(title=a.get_text(strip=True), url=a["href"])
                for a in soup.select("a[href^='/c/']")
            ]

        chapters = walk_list_pages(
            first_soup=pages["https://demo.test/toc"],
            first_url="https://demo.test/toc",
            parse_chapters=parse,
            next_url=lambda s, u: next_from_selector(s, "a.n", u),
            fetch_page=lambda url: fetched.append(url) or pages[url],
            delay=lambda: None,
        )
        assert [c.title for c in chapters] == ["第1章", "第2章"]
        assert fetched == ["https://demo.test/toc?p=2"]

    def test_walk_content_pages_appends(self):
        first = _soup('<div>one</div><a class="n" href="/c?p=2">下一页</a>')
        extra = _soup("<div>two</div>")

        def fetch(url):
            assert url == "https://demo.test/c?p=2"
            return "<div>two</div>", extra

        html = walk_content_pages(
            first_html="<div>one</div>",
            first_soup=first,
            first_url="https://demo.test/c",
            next_url=lambda s, u: next_from_selector(s, "a.n", u),
            fetch_html_and_soup=fetch,
            delay=lambda: None,
        )
        assert "one" in html and "two" in html
