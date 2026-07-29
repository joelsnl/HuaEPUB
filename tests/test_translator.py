"""Offline tests for core.translator.GoogleTranslator (no network)."""

import time as _time

import pytest

from core.translator import GoogleTranslator


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Skip retry cooldowns so tests run instantly."""
    monkeypatch.setattr(_time, 'sleep', lambda s: None)
    import core.translator
    monkeypatch.setattr(core.translator.time, 'sleep', lambda s: None)


def make_translator(**kwargs):
    return GoogleTranslator(max_workers=4, **kwargs)


class TestRetryLoopTermination:
    def test_terminates_when_all_segments_permanently_fail(self):
        t = make_translator()
        # Always "fail": return the original Chinese text
        t._translate_single = lambda text, index: (t._update_progress(), (index, text))[1]

        texts = [f'中文段落测试内容第{i}句话继续' for i in range(6)]
        start = _time.monotonic()
        results = t.translate_texts_with_retry(texts, max_retry_passes=8)
        assert results == texts
        assert _time.monotonic() - start < 10

    def test_accepts_partial_improvement(self):
        t = make_translator()
        state = {'pass': 0}

        def fake_single(text, index):
            t._update_progress()
            if state['pass'] == 0:
                return (index, text)
            # Mostly translated, but a Chinese name remains
            return (index, 'The hero 李明 walked away.')

        original_translate_texts = t.translate_texts

        def first_pass(texts, cb=None):
            result = original_translate_texts(texts, cb)
            state['pass'] = 1
            return result

        t._translate_single = fake_single
        t.translate_texts = first_pass

        texts = ['中文段落测试内容这是一个很长的句子' for _ in range(2)]
        results = t.translate_texts_with_retry(texts, max_retry_passes=8)
        assert all(r == 'The hero 李明 walked away.' for r in results)

    def test_immediate_success_no_retries(self):
        t = make_translator()
        t._translate_single = lambda text, index: (t._update_progress(), (index, 'translated'))[1]
        results = t.translate_texts_with_retry(['中文内容测试' * 3])
        assert results == ['translated']
        assert t.stats['retry_passes'] == 0


class TestPersistentCache:
    class FakeCache:
        def __init__(self):
            self.store = {}
            self.deleted = []

        def get_translation(self, source, backend):
            return self.store.get((backend, source.strip()))

        def put_translation(self, source, translated, backend):
            self.store[(backend, source.strip())] = translated

        def delete_translation(self, source, backend):
            self.deleted.append(source.strip())
            self.store.pop((backend, source.strip()), None)

    def test_persistent_cache_hit_avoids_request(self):
        cache = self.FakeCache()
        cache.put_translation('你好世界', 'Hello world', 'google')
        t = make_translator(persistent_cache=cache)

        def boom(text):
            raise AssertionError("network request should not happen on cache hit")

        t._request_translation = boom
        results = t.translate_texts(['你好世界'])
        assert results == ['Hello world']
        assert t.stats['cache_hits'] == 1

    def test_successful_translation_written_to_cache(self):
        cache = self.FakeCache()
        t = make_translator(persistent_cache=cache)
        t._request_translation = lambda text: 'Hello'
        results = t.translate_texts(['你好'])
        assert results == ['Hello']
        assert cache.get_translation('你好', 'google') == 'Hello'


class TestBackends:
    def test_invalid_backend_falls_back_to_google(self):
        t = make_translator(backend='nonsense')
        assert t.backend == 'google'

    def test_libretranslate_request(self, monkeypatch):
        t = make_translator(backend='libretranslate', libretranslate_url='https://lt.example/')
        assert t.libretranslate_url == 'https://lt.example'

        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {'translatedText': 'Hello'}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured['url'] = url
            captured['json'] = json
            return FakeResponse()

        import core.translator
        monkeypatch.setattr(core.translator.requests, 'post', fake_post)

        assert t._request_translation('你好') == 'Hello'
        assert captured['url'] == 'https://lt.example/translate'
        assert captured['json']['source'] == 'zh'  # zh-CN mapped to plain ISO code
