"""Watermark/ad cleaning and per-book repeating-junk learning."""

from core.ad_detect import learn_site_junk, repeating_junk_lines
from core.cleaner import ContentCleaner


def _chapter(html: str, title: str = "第1章"):
    class Ch:
        pass

    ch = Ch()
    ch.title = title
    ch.content = html
    return ch


class TestSimplifiedWatermarks:
    def test_removes_simplified_site_promo(self):
        cleaned = ContentCleaner().clean_text("正文内容 本书由某某网首发 更多正文")
        assert "本书由" not in cleaned
        assert "正文内容" in cleaned

    def test_removes_please_bookmark(self):
        cleaned = ContentCleaner().clean_text("请收藏本站 然后继续看")
        assert "请收藏" not in cleaned
        assert "然后继续看" in cleaned

    def test_removes_ascii_site_url(self):
        cleaned = ContentCleaner().clean_text("正文 www.example.com 下文")
        assert "example.com" not in cleaned
        assert "正文" in cleaned
        assert "下文" in cleaned

    def test_traditional_still_removed(self):
        cleaned = ContentCleaner().clean_text("正文內容 本書由某某網首發 更多正文")
        assert "本書由" not in cleaned


class TestAdDivs:
    def test_removes_ad_div_with_content(self):
        html = '<div><div class="txtad">广告文字www.spam.com</div><p>正文</p></div>'
        cleaned = ContentCleaner().clean_html(html)
        assert "txtad" not in cleaned
        assert "广告文字" not in cleaned
        assert "正文" in cleaned


class TestLearnRepeatingJunk:
    def test_learns_footer_from_five_chapters(self):
        footer = "请到顶点小说网阅读最新章节无弹窗"
        chapters = [
            _chapter(f"<p>第{i}章剧情很长，主角走在路上。</p><p>{footer}</p>", f"第{i}章")
            for i in range(1, 6)
        ]
        cleaner = ContentCleaner()
        learned = learn_site_junk(cleaner, chapters)
        assert any(footer in item for item in learned)
        cleaned = cleaner.clean_html(chapters[0].content)
        assert "顶点小说网" not in cleaned
        assert "主角走在路上" in cleaned

    def test_does_not_strip_repeated_plot(self):
        chapters = [
            _chapter(f"<p>他笑了笑，转身离开了大厅。</p><p>第{i}章还有别的描写。</p>")
            for i in range(1, 6)
        ]
        cleaner = ContentCleaner()
        learned = learn_site_junk(cleaner, chapters)
        assert not any("他笑了笑" in item for item in learned)
        cleaned = cleaner.clean_html(chapters[0].content)
        assert "他笑了笑" in cleaned

    def test_skips_when_only_one_chapter(self):
        cleaner = ContentCleaner()
        learned = learn_site_junk(cleaner, [_chapter("<p>请收藏本站</p>")])
        assert learned == []

    def test_idempotent(self):
        footer = "请到某某网无弹窗阅读"
        chapters = [_chapter(f"<p>故事{i}</p><p>{footer}</p>") for i in range(5)]
        cleaner = ContentCleaner()
        first = learn_site_junk(cleaner, chapters)
        second = learn_site_junk(cleaner, chapters)
        assert first == second

    def test_repeating_helper_needs_junk_hint(self):
        htmls = ["<p>他走进了房间。</p>"] * 5
        assert repeating_junk_lines(htmls) == []
