"""Offline tests for core.cleaner.ContentCleaner."""

from core.cleaner import ContentCleaner, is_chinese, count_chinese_chars


def make_cleaner(**kwargs):
    return ContentCleaner(**kwargs)


class TestCleanText:
    def test_removes_watermarks(self):
        cleaner = make_cleaner()
        text = "正文內容 本書由某某網首發 更多正文"
        cleaned = cleaner.clean_text(text)
        assert "本書由" not in cleaned
        assert "正文內容" in cleaned

    def test_removes_invisible_chars(self):
        cleaner = make_cleaner()
        assert cleaner.clean_text("你\u200b好\ufeff") == "你好"

    def test_removes_fullwidth_urls(self):
        cleaner = make_cleaner()
        # Fullwidth letters with an ASCII dot, the pattern sites actually use
        cleaned = cleaner.clean_text("內容ｅｘａｍｐｌｅ.ｃｏｍ更多內容")
        assert "ｅｘａｍｐｌｅ" not in cleaned


class TestCleanHtml:
    def test_removes_scripts_and_ad_divs(self):
        cleaner = make_cleaner()
        html = '<div><script>x()</script><div class="txtad"></div><p>正文</p></div>'
        cleaned = cleaner.clean_html(html)
        assert '<script>' not in cleaned
        assert 'txtad' not in cleaned
        assert '正文' in cleaned

    def test_br_runs_become_paragraphs(self):
        cleaner = make_cleaner()
        html = '<div id="c">第一段<br/><br/>第二段<br/>第三段</div>'
        cleaned = cleaner.clean_html(html)
        assert '<p>第一段</p>' in cleaned
        assert '<p>第二段</p>' in cleaned
        assert '<p>第三段</p>' in cleaned
        assert cleaner.stats['br_converted'] == 3

    def test_br_conversion_skips_mixed_content(self):
        cleaner = make_cleaner()
        html = '<div><p>keep me</p>text<br/>more text</div>'
        cleaned = cleaner.clean_html(html)
        assert '<p>keep me</p>' in cleaned
        assert '<br' in cleaned

    def test_br_conversion_can_be_disabled(self):
        cleaner = make_cleaner(convert_br_to_p=False)
        html = '<div>第一段<br/><br/>第二段</div>'
        cleaned = cleaner.clean_html(html)
        assert '<br' in cleaned
        assert cleaner.stats['br_converted'] == 0

    def test_deprecated_tags_converted(self):
        cleaner = make_cleaner()
        cleaned = cleaner.clean_html('<div><center>居中文字</center></div>')
        assert '<center>' not in cleaned
        assert 'text-align:center' in cleaned


class TestSelfClosingTags:
    def test_fix_self_closing(self):
        cleaner = make_cleaner()
        fixed = cleaner._fix_self_closing_tags(b'<div/><p class="x"/><br/>')
        assert b'<div></div>' in fixed
        assert b'<p class="x"></p>' in fixed
        assert b'<br/>' in fixed  # br is allowed to self-close
        assert cleaner.stats['self_closing_fixed'] == 2


class TestChineseHelpers:
    def test_is_chinese(self):
        assert is_chinese("你好")
        assert not is_chinese("hello")
        assert not is_chinese("")

    def test_count_chinese(self):
        assert count_chinese_chars("你好 world") == 2
