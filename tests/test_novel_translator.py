"""Offline tests for NovelTranslator (no network, no CTranslate2)."""

import asyncio

from core.polish.glossary import Glossary, Term
from core.translation.glossary import GlossaryEngine
from core.translation.novel_translator import NovelTranslator


def _translator(**kwargs):
    kwargs.setdefault("use_glossary", False)
    kwargs.setdefault("max_workers", 2)
    return NovelTranslator(**kwargs)


class TestGlossaryAroundEngine:
    def test_protects_terms_before_engine(self):
        seen = []
        gloss = GlossaryEngine(Glossary())
        gloss.add_terms([Term(source="大长老", target="Grand Elder")])
        t = _translator(glossary=gloss, backend="google")

        def fake(text):
            seen.append(text)
            return "The §G0§ nodded."

        t._request_google = fake
        out = t.translate_text("大长老点了点头。")
        assert seen and "§G0§点了点头。" in seen[0]
        assert out == "The Grand Elder nodded."

    def test_cache_key_is_original_chinese(self):
        class FakeCache:
            def __init__(self):
                self.store = {}

            def get_translation(self, source, backend):
                return self.store.get((backend, source.strip()))

            def put_translation(self, source, translated, backend, commit=True):
                self.store[(backend, source.strip())] = translated

            def flush(self):
                pass

            def delete_translation(self, source, backend):
                self.store.pop((backend, source.strip()), None)

            def delete_translations(self, items):
                for source, backend in items:
                    self.delete_translation(source, backend)

        gloss = GlossaryEngine(Glossary())
        gloss.add_terms([("大长老", "Grand Elder")])
        cache = FakeCache()
        t = _translator(glossary=gloss, persistent_cache=cache)
        t._request_google = lambda text: "The §G0§ bowed."
        assert t.translate_text("大长老行礼。") == "The Grand Elder bowed."
        stored = [key[1] for key in cache.store]
        assert "大长老行礼。" in stored
        assert any(key[0].startswith("google+g") for key in cache.store)

    def test_use_glossary_false_skips_protect(self):
        seen = []
        t = _translator(use_glossary=False)

        def fake(text):
            seen.append(text)
            return "Big old person"

        t._request_google = fake
        assert t.translate_text("大长老") == "Big old person"
        assert seen and "大长老" in seen[0]

    def test_configure_glossary_skips_pack_for_urban(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setattr(
            "core.translation.glossary.user_glossary_path",
            lambda: tmp_path / "glossary.json",
        )
        monkeypatch.setattr(
            "core.translation.glossary.qwen_glossary_path",
            lambda: tmp_path / "glossary-qwen.json",
        )
        monkeypatch.setattr(
            "core.translation.glossary.novel_glossary_path",
            lambda title: tmp_path / "missing.json",
        )
        t = NovelTranslator(use_glossary=True, max_workers=2)
        t.configure_glossary(
            SimpleNamespace(title="霸道总裁爱上我", description="都市恋爱")
        )
        assert t.glossary is not None
        sources = {term.source for term in t.glossary.glossary.terms}
        assert "金丹" not in sources
        assert "公子" not in sources
        t.configure_glossary(
            SimpleNamespace(title="凡人修仙传", description="修仙")
        )
        sources = {term.source for term in t.glossary.glossary.terms}
        assert "金丹" not in sources

    def test_configure_glossary_adds_pack_for_xianxia(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setattr(
            "core.translation.glossary.user_glossary_path",
            lambda: tmp_path / "glossary.json",
        )
        monkeypatch.setattr(
            "core.translation.glossary.qwen_glossary_path",
            lambda: tmp_path / "glossary-qwen.json",
        )
        monkeypatch.setattr(
            "core.translation.glossary.novel_glossary_path",
            lambda title: tmp_path / "missing.json",
        )
        t = NovelTranslator(use_glossary=True, max_workers=2)
        t.configure_glossary(SimpleNamespace(title="凡人修仙传", description=""))
        sources = {term.source for term in t.glossary.glossary.terms}
        assert "金丹" in sources
        assert "大长老" in sources


class TestOfflineFallback:
    def test_missing_packages_fall_back_to_google(self, monkeypatch):
        monkeypatch.setattr(
            "core.translation.novel_translator.nmt_runtime_available",
            lambda: False,
        )
        t = _translator(backend="ctranslate2")
        assert t.backend == "google"

    def test_ctranslate2_alias_offline(self, monkeypatch):
        monkeypatch.setattr(
            "core.translation.novel_translator.nmt_runtime_available",
            lambda: False,
        )
        t = _translator(backend="offline")
        assert t.backend == "google"

    def test_google_translator_accepts_ctranslate2_name(self):
        from core.translator import GoogleTranslator

        t = GoogleTranslator(backend="ctranslate2", max_workers=20)
        assert t.backend == "ctranslate2"
        assert t.max_workers == 4
        assert t._cache_backend() == "ctranslate2:opus-mt-zh-en"


class TestGooglePackAndLegacyCache:
    def test_legacy_google_cache_is_reused(self):
        class FakeCache:
            def __init__(self):
                self.store = {("google", "你好世界"): "Hello world"}

            def get_translation(self, source, backend):
                return self.store.get((backend, source.strip()))

            def put_translation(self, source, translated, backend, commit=True):
                self.store[(backend, source.strip())] = translated

            def flush(self):
                pass

            def delete_translation(self, source, backend):
                self.store.pop((backend, source.strip()), None)

            def delete_translations(self, items):
                for source, backend in items:
                    self.delete_translation(source, backend)

        gloss = GlossaryEngine(Glossary())
        gloss.add_terms([("大长老", "Grand Elder")])
        t = _translator(glossary=gloss, persistent_cache=FakeCache())

        def boom(_text):
            raise AssertionError("legacy google rows must still count as cache hits")

        t._request_google = boom
        assert t.translate_text("你好世界") == "Hello world"
        assert t.stats["cache_hits"] == 1

    def test_packing_uses_one_request_for_many_segments(self):
        from core.translation.pack import pack_mt_segments

        t = _translator(use_glossary=False)
        texts = [f"中文段落编号{i}内容继续" for i in range(12)]
        seen = []

        def fake(blob):
            seen.append(blob)
            parts = blob.split("[[#")
            # Echo English so unpack succeeds
            out = []
            for i, text in enumerate(texts, 1):
                out.append(f"[[#{i}#]]\nEN {text}")
            return "\n".join(out)

        t._request_google = fake
        results = t._translate_packed(texts, None)
        assert len(seen) == 1
        assert pack_mt_segments(texts[:2])[:8] == "[[#1#]]\n"
        assert results == [f"EN {text}" for text in texts]
        assert t.stats["requests"] == 1

    def test_unpack_failure_falls_back_to_singles(self):
        t = _translator(use_glossary=False, max_workers=8)
        calls = []

        def fake(text):
            calls.append(text)
            if "[[#" in (text or ""):
                return "one blob with no markers left"
            return "Hello there friend"

        t._request_google = fake
        texts = [f"中文段落编号{i}内容继续下去吧" for i in range(4)]
        results = t._translate_packed(texts, None)
        assert results == ["Hello there friend"] * 4
        assert any("[[#" in c for c in calls)
        assert sum(1 for c in calls if "[[#" not in c) == 4

    def test_throttle_does_not_shrink_workers(self, monkeypatch):
        t = _translator(use_glossary=False, max_workers=200)
        monkeypatch.setattr(t, "_interruptible_sleep", lambda _s: None)
        assert t.max_workers == 200
        t._note_throttle(2.0)
        assert t.max_workers == 200
        t._note_throttle(2.0)
        assert t.max_workers == 200

    def test_throttle_log_is_once_per_15s(self, monkeypatch):
        import core.translator as tr

        monkeypatch.setattr(tr, "_GTX_LOG_AT", 0.0)
        monkeypatch.setattr(tr, "_GTX_HIDDEN", 0)
        printed = []
        monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(a[0] if a else ""))
        t = _translator(use_glossary=False, max_workers=200)
        monkeypatch.setattr(t, "_interruptible_sleep", lambda _s: None)
        t._note_throttle(2.0)
        t._note_throttle(2.0)
        t._note_throttle(2.0)
        hits = [line for line in printed if "HTTP 429" in str(line)]
        assert len(hits) == 1
        assert "rate limited" in hits[0]

    def test_google_translate_texts_does_not_pack(self):
        t = _translator(use_glossary=False, max_workers=8)
        seen = []

        def fake(text):
            seen.append(text)
            return f"EN {text}"

        t._request_google = fake
        texts = [f"中文段落编号{i}内容继续下去吧" for i in range(4)]
        results = t.translate_texts(texts)
        assert len(seen) == 4
        assert all("[[#" not in c for c in seen)
        assert results == [f"EN {text}" for text in texts]

    def test_pack_unpack_roundtrip(self):
        from core.translation.pack import pack_mt_segments, unpack_mt_segments

        texts = ["第一段", "第二段", "第三段"]
        assert unpack_mt_segments(pack_mt_segments(texts), 3) == texts


class TestPrefetchAndAsync:
    def test_google_prefetch_does_not_hit_gtx(self):
        t = _translator(use_glossary=False)
        calls = []

        def boom(text):
            calls.append(text)
            raise AssertionError("google prefetch must not hit gtx")

        t._request_google = boom
        t.prefetch_chapter("<p>这是一段中文正文内容</p>")
        t.wait_prefetch()
        assert calls == []
        t._request_google = lambda text: "Hello after fetch"
        assert t.translate_text("这是一段中文正文内容") == "Hello after fetch"

    def test_libre_prefetch_does_not_block_and_warms_cache(self):
        t = _translator(use_glossary=False, backend="libretranslate")
        t._request_libretranslate = lambda text: "Hello from prefetch"

        t.prefetch_chapter("<p>这是一段中文正文内容</p>")
        t.wait_prefetch()
        assert t.translate_text("这是一段中文正文内容") == "Hello from prefetch"
        assert t.stats["cache_hits"] >= 1

    def test_libre_prefetch_applies_to_chapter(self):
        from core.parser import Chapter

        t = _translator(use_glossary=False, backend="libretranslate")
        t._request_libretranslate = lambda text: "Hello from prefetch"
        ch = Chapter(
            title="一",
            url="https://example.com/1",
            content="<p>这是一段中文正文内容</p>",
        )
        t.prefetch_chapter(ch.content, chapter=ch)
        t.wait_prefetch()
        assert ch.translation_applied is True
        assert ch.translation_pairs
        assert "Hello" in (ch.translated_content or "")

    def test_libre_prefetch_stops_after_429(self, monkeypatch):
        from core.parser import Chapter
        from core.translator import RateLimitedError

        t = _translator(use_glossary=False, backend="libretranslate", max_workers=200)
        monkeypatch.setattr(t, "_interruptible_sleep", lambda _s: None)
        calls = []

        def fake(_text):
            calls.append(1)
            raise RateLimitedError(2.0)

        t._request_libretranslate = fake
        ch1 = Chapter(
            title="一",
            url="https://example.com/1",
            content="<p>这是一段中文正文内容</p>",
        )
        ch2 = Chapter(
            title="二",
            url="https://example.com/2",
            content="<p>另一段中文正文内容啊</p>",
        )
        t.prefetch_chapter(ch1.content, chapter=ch1)
        t.wait_prefetch()
        assert t._skip_prefetch_translate is True
        before = len(calls)
        t.prefetch_chapter(ch2.content, chapter=ch2)
        t.wait_prefetch()
        assert len(calls) == before
        assert ch2.translation_applied is False

    def test_does_not_cache_echoed_chinese(self):
        class FakeCache:
            def __init__(self):
                self.store = {}

            def get_translation(self, source, backend):
                return self.store.get((backend, source.strip()))

            def put_translation(self, source, translated, backend, commit=True):
                self.store[(backend, source.strip())] = translated

            def flush(self):
                pass

            def delete_translation(self, source, backend):
                self.store.pop((backend, source.strip()), None)

            def delete_translations(self, items):
                for source, backend in items:
                    self.delete_translation(source, backend)

        src = "这是一段很长的中文正文内容需要被翻译处理完成"
        cache = FakeCache()
        t = _translator(use_glossary=False, persistent_cache=cache)
        t._request_google = lambda text: text
        assert t.translate_text(src) == src
        assert cache.store == {}

    def test_rejects_poisoned_chinese_cache(self):
        class FakeCache:
            def __init__(self):
                self.store = {}

            def get_translation(self, source, backend):
                return self.store.get((backend, source.strip()))

            def put_translation(self, source, translated, backend, commit=True):
                self.store[(backend, source.strip())] = translated

            def flush(self):
                pass

            def delete_translation(self, source, backend):
                self.store.pop((backend, source.strip()), None)

            def delete_translations(self, items):
                for source, backend in items:
                    self.delete_translation(source, backend)

        src = "这是一段很长的中文正文内容需要被翻译处理完成"
        cache = FakeCache()
        cache.store[("google", src)] = src
        t = _translator(use_glossary=False, persistent_cache=cache)
        t._request_google = lambda text: "This long chapter is now English."
        assert t.translate_text(src) == "This long chapter is now English."
        assert cache.store[("google", src)] == "This long chapter is now English."

    def test_failed_prefetch_does_not_mark_applied(self):
        from core.parser import Chapter

        t = _translator(use_glossary=False)
        t._request_google = lambda text: text
        ch = Chapter(
            title="一",
            url="https://example.com/1",
            content="<p>这是一段很长的中文正文内容需要被翻译处理完成</p>",
        )
        t.prefetch_chapter(ch.content, chapter=ch)
        t.wait_prefetch()
        assert ch.translation_applied is False

    def test_translate_batch_async(self):
        t = _translator(use_glossary=False)
        t._request_google = lambda text: "Hi"

        async def run():
            return await t.translate_batch(["你好世界"])

        assert asyncio.run(run()) == ["Hi"]

    def test_nmt_prefetch_skips_until_model_ready(self, monkeypatch):
        monkeypatch.setattr(
            "core.translation.novel_translator.nmt_runtime_available",
            lambda: True,
        )
        monkeypatch.setattr(
            "core.translation.novel_translator.nmt_model_ready",
            lambda *a, **k: False,
        )
        t = _translator(backend="ctranslate2", use_glossary=False)
        assert t.backend == "ctranslate2"
        t._request_google = lambda text: (_ for _ in ()).throw(
            AssertionError("prefetch must not hit Google")
        )
        t._ensure_nmt = lambda: (_ for _ in ()).throw(
            AssertionError("prefetch must not download NMT")
        )
        t.prefetch_chapter("<p>这是一段中文正文内容</p>")
        t.wait_prefetch()

    def test_nmt_reports_progress_between_chunks(self, monkeypatch):
        monkeypatch.setattr(
            "core.translation.novel_translator.nmt_runtime_available",
            lambda: True,
        )
        monkeypatch.setattr(
            "core.translation.novel_translator.nmt_model_ready",
            lambda *a, **k: True,
        )
        t = _translator(backend="ctranslate2", use_glossary=False)
        seen_sizes = []

        class FakeEngine:
            def translate_batch(self, texts):
                seen_sizes.append(len(texts))
                return [f"EN {text}" for text in texts]

        t._ensure_nmt = lambda: FakeEngine()
        calls = []
        texts = [f"中文段落编号{i}内容继续下去吧" for i in range(40)]
        out = t.translate_texts(
            texts, progress_callback=lambda done, total: calls.append(done)
        )
        assert seen_sizes == [32, 8]
        assert any(c == 32 for c in calls)
        assert calls[-1] == 40
        assert out[0].startswith("EN ")
