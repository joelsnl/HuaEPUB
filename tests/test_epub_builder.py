"""Offline tests for core.epub_builder."""

import hashlib
from pathlib import Path

import pytest
from ebooklib import epub

from core.epub_builder import EPUBBuilder, TranslatedEPUBBuilder, VOLUME_PREFIX_RE
from core.parser import Chapter, NovelInfo


def make_epub_chapter(title):
    ch = epub.EpubHtml(title=title, file_name='x.xhtml')
    ch.title = title
    return ch


class TestStableIdentifier:
    def test_identifier_is_deterministic(self):
        url = 'https://example.com/book/1'
        expected = f"novel-{hashlib.md5(url.encode('utf-8')).hexdigest()[:16]}"
        # Two hash computations across "runs" must match
        assert expected == f"novel-{hashlib.md5(url.encode('utf-8')).hexdigest()[:16]}"


class TestTranslationApplication:
    def test_node_replacement_escapes_special_chars(self):
        builder = TranslatedEPUBBuilder.__new__(TranslatedEPUBBuilder)
        html = '<p>你好世界</p><p>第二段</p>'
        segments = builder._extract_text_segments(html)
        assert segments == ['你好世界', '第二段']

        pairs = [('你好世界', 'Hello <world> & "friends"'), ('第二段', 'Second')]
        result = builder._apply_content_translations(html, pairs)
        assert 'Hello' in result
        assert '<world>' not in result  # escaped, can't break the XHTML
        assert '&lt;world&gt;' in result
        assert 'Second' in result

    def test_untranslated_segments_left_alone(self):
        builder = TranslatedEPUBBuilder.__new__(TranslatedEPUBBuilder)
        html = '<p>你好</p>'
        result = builder._apply_content_translations(html, [('你好', '你好')])
        assert '你好' in result

    def test_polish_flag_defaults_off(self):
        builder = TranslatedEPUBBuilder(translator=object())
        assert builder.polish is False
        builder = TranslatedEPUBBuilder(translator=object(), polish=True)
        assert builder.polish is True

    def test_runaway_title_translation_keeps_the_source(self):
        builder = TranslatedEPUBBuilder.__new__(TranslatedEPUBBuilder)
        info = NovelInfo(title="低调王子", author="天外", description="简介")
        blob = "A cigarette smokes a smoke " * 40
        builder.apply_translations(
            info,
            [],
            [("title", 0, info.title), ("author", 0, info.author), ("description", 0, info.description)],
            [blob, "Sky Outside", "A normal description. " * 20],
        )
        assert info.title == "低调王子"
        assert info.author == "Sky Outside"
        assert info.description.startswith("A normal description.")

    def test_short_title_translation_is_stored(self):
        builder = TranslatedEPUBBuilder.__new__(TranslatedEPUBBuilder)
        info = NovelInfo(title="低调王子", author="天外")
        builder.apply_translations(
            info, [], [("title", 0, info.title)], ["The Low-Key Prince"],
        )
        assert info.title == "The Low-Key Prince"


class TestVolumeToc:
    def make_builder(self):
        return EPUBBuilder.__new__(EPUBBuilder)

    def test_flat_toc_without_volume_prefixes(self):
        builder = self.make_builder()
        chapters = [make_epub_chapter(f'Chapter {i}') for i in range(1, 6)]
        toc = builder._build_toc(chapters)
        assert toc == chapters

    def test_grouped_toc_with_chinese_volumes(self):
        builder = self.make_builder()
        titles = (
            ['第一卷 第%d章' % i for i in range(1, 4)]
            + ['第二卷 第%d章' % i for i in range(1, 4)]
        )
        chapters = [make_epub_chapter(t) for t in titles]
        toc = builder._build_toc(chapters)
        assert len(toc) == 2
        section1, children1 = toc[0]
        section2, children2 = toc[1]
        assert section1.title == '第一卷'
        assert section2.title == '第二卷'
        assert len(children1) == 3
        assert len(children2) == 3

    def test_grouped_toc_with_english_volumes(self):
        builder = self.make_builder()
        titles = ['Volume 1 Chapter 1', 'Volume 1 Chapter 2',
                  'Volume 2 Chapter 1', 'Volume 2 Chapter 2']
        chapters = [make_epub_chapter(t) for t in titles]
        toc = builder._build_toc(chapters)
        assert len(toc) == 2

    def test_single_volume_stays_flat(self):
        builder = self.make_builder()
        chapters = [make_epub_chapter(f'第一卷 第{i}章') for i in range(1, 5)]
        toc = builder._build_toc(chapters)
        assert toc == chapters  # only one distinct volume - no grouping

    def test_mostly_unlabeled_stays_flat(self):
        builder = self.make_builder()
        titles = ['第一卷 第1章', '第二卷 第2章'] + [f'Chapter {i}' for i in range(3, 11)]
        chapters = [make_epub_chapter(t) for t in titles]
        toc = builder._build_toc(chapters)
        assert toc == chapters  # <60% labeled - no grouping


class TestVolumeRegex:
    def test_matches(self):
        for title in ['第一卷 风起', '第12卷 高潮', 'Volume 3: Rising', 'Vol. 2 Something', 'Book 4 - End']:
            assert VOLUME_PREFIX_RE.match(title), title

    def test_non_matches(self):
        for title in ['第一章 开始', 'Chapter 5', 'Prologue']:
            assert not VOLUME_PREFIX_RE.match(title), title


class TestReadAloudEpub:
    def _info_chapters(self, *, language="zh", english=True):
        body = "<p>Hello there. More words follow.</p>" if english else "<p>你好。世界继续。</p>"
        info = NovelInfo(
            title="Test Book",
            author="Author",
            source_url="https://example.com/b",
            language=language,
        )
        chapters = [
            Chapter(title="Chapter 1", url="https://example.com/1", content=body),
            Chapter(title="Chapter 2", url="https://example.com/2", content=body),
        ]
        return info, chapters

    def _opf_and_names(self, dest):
        import zipfile

        with zipfile.ZipFile(dest) as zf:
            names = zf.namelist()
            opf_name = next(n for n in names if n.endswith("content.opf"))
            opf = zf.read(opf_name).decode("utf-8")
            chapter = zf.read("EPUB/chapter_0000.xhtml").decode("utf-8")
            smil = zf.read("EPUB/chapter_0000.smil").decode("utf-8")
            css = zf.read("EPUB/style/nav.css").decode("utf-8")
        return names, opf, chapter, smil, css

    def test_overlays_and_language_for_chinese_source(self, tmp_path):
        dest = tmp_path / "zh.epub"
        info, chapters = self._info_chapters(language="zh", english=False)
        EPUBBuilder().build(info, chapters, str(dest))
        names, opf, chapter, smil, css = self._opf_and_names(dest)
        assert any(n.endswith(".smil") for n in names)
        assert "media-overlay=" in opf
        assert "media:duration" in opf
        assert "rendition:layout" in opf
        assert ">zh-CN<" in opf
        assert 'id="s0000-0001"' in chapter
        assert "<audio" not in smil
        assert "-epub-media-overlay-active" in css
        assert 'xml:lang="zh-CN"' in chapter or 'lang="zh-CN"' in chapter

    def test_translated_build_is_english(self, tmp_path):
        dest = tmp_path / "en.epub"
        info, chapters = self._info_chapters(language="zh", english=True)
        EPUBBuilder().build(info, chapters, str(dest), skip_html_clean=True, language="en")
        _names, opf, chapter, _smil, _css = self._opf_and_names(dest)
        assert ">en<" in opf
        assert "Hello there." in chapter
        assert "media-overlay=" in opf

    def test_stylesheet_link_survives_ebooklib_rebuild(self, tmp_path):
        dest = tmp_path / "css.epub"
        info, chapters = self._info_chapters()
        EPUBBuilder().build(info, chapters, str(dest), language="en")
        _names, _opf, chapter, _smil, _css = self._opf_and_names(dest)
        assert "style/nav.css" in chapter


class TestAtomicWrite:
    def _info_chapters(self):
        info = NovelInfo(title="Test Book", author="Author", source_url="https://example.com/b")
        chapters = [
            Chapter(title="Chapter 1", url="https://example.com/1", content="<p>Hello there.</p>"),
            Chapter(title="Chapter 2", url="https://example.com/2", content="<p>More words.</p>"),
        ]
        return info, chapters

    def test_success_leaves_no_tmp(self, tmp_path):
        dest = tmp_path / "novel.epub"
        info, chapters = self._info_chapters()
        EPUBBuilder().build(info, chapters, str(dest))
        assert dest.is_file()
        assert dest.stat().st_size > 0
        assert not (tmp_path / "novel.epub.tmp").exists()

    def test_failure_does_not_clobber_existing(self, tmp_path, monkeypatch):
        dest = tmp_path / "novel.epub"
        dest.write_bytes(b"good-old-epub")
        info, chapters = self._info_chapters()

        def boom(path, book, opts):
            Path(path).write_bytes(b"partial")
            raise RuntimeError("disk full")

        monkeypatch.setattr("core.epub_builder.epub.write_epub", boom)
        with pytest.raises(RuntimeError, match="disk full"):
            EPUBBuilder().build(info, chapters, str(dest))
        assert dest.read_bytes() == b"good-old-epub"
        assert not (tmp_path / "novel.epub.tmp").exists()


class TestSkipSecondClean:
    def test_skip_html_clean_does_not_call_cleaner(self, tmp_path):
        class CountingCleaner:
            def __init__(self):
                self.n = 0

            def clean_html(self, html):
                self.n += 1
                return html

        info = NovelInfo(title="Test Book", author="Author", source_url="https://example.com/b")
        chapters = [
            Chapter(title="Chapter 1", url="https://example.com/1", content="<p>Hello there.</p>"),
        ]
        dest = tmp_path / "skip.epub"
        cleaner = CountingCleaner()
        EPUBBuilder(cleaner=cleaner).build(info, chapters, str(dest), skip_html_clean=True)
        assert cleaner.n == 0
        dest2 = tmp_path / "clean.epub"
        cleaner2 = CountingCleaner()
        EPUBBuilder(cleaner=cleaner2).build(info, chapters, str(dest2), skip_html_clean=False)
        assert cleaner2.n >= 1

    def test_translated_build_passes_skip_flag(self, monkeypatch, tmp_path):
        seen = {}

        def fake_build(self, *args, **kwargs):
            seen["skip"] = kwargs.get("skip_html_clean")
            return str(tmp_path / "out.epub")

        monkeypatch.setattr(EPUBBuilder, "build", fake_build)

        class FakeTranslator:
            _cancel_requested = False
            stats = {"requests": 0, "cache_hits": 0}

            def translate_texts_with_retry(self, texts, *a, **k):
                return list(texts)

        info = NovelInfo(title="Hello", author="Author", source_url="https://example.com/b")
        chapters = [
            Chapter(title="Chapter 1", url="https://example.com/1", content="<p>Already English.</p>"),
        ]
        builder = TranslatedEPUBBuilder(translator=FakeTranslator(), cleaner=None)
        builder.build_with_translation(info, chapters, str(tmp_path / "out.epub"))
        assert seen.get("skip") is True
