"""Offline tests for Play Books Read Aloud overlays (core.read_aloud)."""

from core.read_aloud import (
    ACTIVE_CLASS,
    build_smil,
    epub_language_code,
    estimate_narration_seconds,
    mark_narration_fragments,
    smil_clock,
    split_sentences,
)


class TestLanguage:
    def test_translated_is_english(self):
        assert epub_language_code("zh", translated=True) == "en"
        assert epub_language_code("zh-Hant", translated=True) == "en"

    def test_maps_chinese_variants(self):
        assert epub_language_code("zh") == "zh-CN"
        assert epub_language_code("zh-Hans") == "zh-CN"
        assert epub_language_code("zh-Hant") == "zh-TW"
        assert epub_language_code("zh-TW") == "zh-TW"

    def test_english_passthrough(self):
        assert epub_language_code("en") == "en"
        assert epub_language_code("en-US") == "en"


class TestSentences:
    def test_english_split(self):
        assert split_sentences("Hello world. How are you? Fine!") == [
            "Hello world.",
            "How are you?",
            "Fine!",
        ]

    def test_chinese_split(self):
        parts = split_sentences("你好。世界！继续")
        assert parts[0] == "你好。"
        assert parts[1].startswith("世界！")
        assert "继续" in parts[-1]

    def test_empty(self):
        assert split_sentences("") == []
        assert split_sentences("   ") == []


class TestMarkFragments:
    def test_sentence_spans_on_simple_paragraph(self):
        html, fragments = mark_narration_fragments(
            "<p>Hello world. How are you?</p>", "s0000"
        )
        assert len(fragments) == 2
        assert fragments[0] == ("s0000-0001", "Hello world.")
        assert fragments[1] == ("s0000-0002", "How are you?")
        assert 'id="s0000-0001"' in html
        assert "Hello world." in html

    def test_nested_markup_gets_block_id(self):
        html, fragments = mark_narration_fragments(
            "<p>Hello <em>world</em>.</p>", "s0001"
        )
        assert len(fragments) == 1
        assert fragments[0][0] == "s0001-0001"
        assert "<em>world</em>" in html
        assert 'id="s0001-0001"' in html

    def test_bare_text_wrapped(self):
        html, fragments = mark_narration_fragments("Just a sentence.", "s0002")
        assert fragments
        assert "<p>" in html
        assert fragments[0][1] == "Just a sentence."


class TestSmil:
    def test_text_only_pars(self):
        xml = build_smil("chapter_0000.xhtml", [("s0000-0001", "Hello.")])
        assert 'src="chapter_0000.xhtml#s0000-0001"' in xml
        assert "<audio" not in xml
        assert 'epub:textref="chapter_0000.xhtml"' in xml
        assert 'version="3.0"' in xml

    def test_clock_format(self):
        assert smil_clock(5.25) == "0:00:05.250"
        assert smil_clock(3661.5) == "1:01:01.500"
        assert smil_clock(0) == "0:00:00.000"

    def test_english_duration_scales_with_words(self):
        short = estimate_narration_seconds("one two", "en")
        long = estimate_narration_seconds("one two three four five six", "en")
        assert long > short

    def test_active_class_css_token(self):
        assert ACTIVE_CLASS.startswith("-epub-")
