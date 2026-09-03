"""Offline tests for core.translator.GoogleTranslator (no network)."""

import time as _time

import pytest

from core.translator import GoogleTranslator, is_usable_translation


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Skip retry cooldowns so tests run instantly."""
    monkeypatch.setattr(_time, 'sleep', lambda s: None)
    import core.translator
    monkeypatch.setattr(core.translator.time, 'sleep', lambda s: None)


def make_translator(**kwargs):
    return GoogleTranslator(max_workers=4, **kwargs)


class _FakeResponse:
    def __init__(self, json_data=None, *, status_code=200, headers=None, text="", lines=None):
        self._json = json_data
        self.status_code = status_code
        self.headers = headers if headers is not None else {}
        self.text = text
        self._lines = lines

    def raise_for_status(self):
        pass

    def json(self):
        return self._json

    def iter_lines(self):
        return iter(self._lines or [])

    def close(self):
        pass


class _FakeSession:
    def __init__(self, *, on_request=None, on_post=None):
        self._on_request = on_request
        self._on_post = on_post
        # safe_http_request prefers session.request when the attribute exists.
        if on_request is not None:
            self.request = self._dispatch_request

    def _dispatch_request(self, method, url, **kwargs):
        return self._on_request(method, url, **kwargs)

    def post(self, url, json=None, headers=None, timeout=None, data=None):
        if self._on_post is None:
            raise AssertionError("unexpected POST")
        return self._on_post(url, json=json, headers=headers, timeout=timeout, data=data)

    def get(self, *a, **k):
        raise AssertionError("unexpected GET")


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

    def test_google_retry_keeps_configured_workers(self):
        t = GoogleTranslator(max_workers=200)
        seen_workers = []
        seen_interval = []
        real = t.translate_texts

        def wrap(texts, cb=None):
            seen_workers.append(t.max_workers)
            seen_interval.append(t.request_interval)
            if len(seen_workers) == 1:
                return list(texts)
            return ["The hero walked away."] * len(texts)

        t.translate_texts = wrap
        texts = ["中文段落测试内容这是一个很长的句子"] * 40
        results = t.translate_texts_with_retry(texts, max_retry_passes=2)
        assert results == ["The hero walked away."] * 40
        assert seen_workers[0] == 200
        # Old retry table used 16 here. Keep the configured pool (capped
        # only by how many are left).
        assert seen_workers[1] == 40
        assert seen_interval[1] == 0.0
        t.translate_texts = real


class TestPersistentCache:
    class FakeCache:
        def __init__(self):
            self.store = {}
            self.deleted = []

        def get_translation(self, source, backend):
            return self.store.get((backend, source.strip()))

        def put_translation(self, source, translated, backend, commit=True):
            self.store[(backend, source.strip())] = translated

        def flush(self):
            pass

        def delete_translation(self, source, backend):
            self.deleted.append(source.strip())
            self.store.pop((backend, source.strip()), None)

        def delete_translations(self, items):
            for source, backend in items:
                self.delete_translation(source, backend)

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

    def test_poisoned_chinese_cache_is_ignored(self):
        src = "这是一段很长的中文正文内容需要被翻译处理完成"
        cache = self.FakeCache()
        cache.put_translation(src, src, "google")
        t = make_translator(persistent_cache=cache)
        t._request_translation = lambda text: "This chapter is now English."
        assert t.translate_texts([src]) == ["This chapter is now English."]
        assert src in cache.deleted
        assert cache.get_translation(src, "google") == "This chapter is now English."

    def test_echoed_chinese_is_not_cached(self):
        src = "这是一段很长的中文正文内容需要被翻译处理完成"
        cache = self.FakeCache()
        t = make_translator(persistent_cache=cache)
        t._request_translation = lambda text: text
        assert t.translate_texts([src]) == [src]
        assert cache.get_translation(src, "google") is None
        assert src not in t.cache


class TestUsableTranslation:
    def test_rejects_echoed_chapter(self):
        src = "这是一段很长的中文正文内容需要被翻译处理完成"
        assert is_usable_translation(src, src) is False
        assert is_usable_translation(src, "") is False
        assert is_usable_translation(src, "This chapter is now English.") is True

    def test_keeps_leftover_names(self):
        src = "这是一段很长的中文正文内容需要被翻译处理完成并且继续下去"
        assert is_usable_translation(src, "The hero 李明 walked away.") is True


class TestCancelAndPause:
    def test_control_cancel_stops_further_google_gets(self):
        from core.download_runner import DownloadControl

        t = GoogleTranslator(max_workers=2)
        ctrl = DownloadControl()
        t.bind_control(ctrl)
        seen = []

        def fake(text):
            seen.append(text)
            if len(seen) == 1:
                ctrl.request_cancel()
            return "ok"

        t._request_translation = fake
        texts = [f"中文段落测试内容第{i}句话继续足够长" for i in range(24)]
        t.translate_texts(texts)
        assert t._cancel_requested is True
        assert 1 <= len(seen) < 24

    def test_pause_blocks_the_next_google_get(self):
        import threading

        from core.download_runner import DownloadControl

        t = GoogleTranslator(max_workers=1)
        ctrl = DownloadControl()
        t.bind_control(ctrl)
        entered_pause = threading.Event()
        order = []

        def fake(text):
            order.append(text)
            if len(order) == 1:
                ctrl.is_paused = True
            return "ok"

        real_wait = t._wait_if_paused

        def wait_and_signal():
            if ctrl.is_paused:
                entered_pause.set()
            real_wait()

        t._wait_if_paused = wait_and_signal
        t._request_translation = fake
        texts = [f"中文段落测试内容第{i}句话继续足够长" for i in range(3)]

        def resume():
            entered_pause.wait(timeout=2)
            ctrl.is_paused = False

        threading.Thread(target=resume, daemon=True).start()
        t.translate_texts(texts)
        assert len(order) == 3
        assert entered_pause.is_set()


class TestCancelLatch:
    def test_translate_texts_does_not_clear_cancel(self):
        t = make_translator()
        t.cancel()

        def boom(_text):
            raise AssertionError("cancelled translator must not hit the network")

        t._request_translation = boom
        results = t.translate_texts(['你好世界'])
        assert results == ['你好世界']
        assert t._cancel_requested is True

    def test_retry_loop_stops_when_cancelled(self):
        t = make_translator()
        t.cancel()
        texts = [f'中文段落测试内容第{i}句话继续' for i in range(4)]
        results = t.translate_texts_with_retry(texts, max_retry_passes=8)
        assert results == texts
        assert t._cancel_requested is True


class TestGoogleConcurrency:
    def test_google_429_does_not_shrink_max_workers(self):
        from core.translator import RateLimitedError

        t = GoogleTranslator(max_workers=200)
        t.max_retries = 1
        t._request_translation = lambda _t: (_ for _ in ()).throw(RateLimitedError(1.0))
        t._translate_single("你好世界测试段落足够长", 0)
        assert t.max_workers == 200
        assert t._gtx.max_limit == 200
        assert t._gtx.limit <= 8


class TestGtxThrottle:
    def test_start_is_8_not_200(self):
        from core.translator import GtxThrottle

        g = GtxThrottle(200)
        assert g.limit == 8
        assert g.max_limit == 200
        assert g.would_admit() is True

    def test_google_new_throttle_starts_at_8_not_1(self):
        t = GoogleTranslator(max_workers=200, backend="google")
        assert t._gtx is not None
        assert t._gtx.limit == 8
        assert t._gtx.max_limit == 200
        one = GoogleTranslator(max_workers=1, backend="google")
        assert one._gtx.limit == 8
        assert one._gtx.max_limit == 8

    def test_many_429s_in_one_window_cut_once(self):
        from core.translator import GtxThrottle

        clock = {"t": 10.0}
        g = GtxThrottle(200, clock=lambda: clock["t"])
        assert g.limit == 8
        first, wait = g.on_429(8.0)
        assert first == 4
        assert wait >= 8.0
        assert g.on_429(8.0)[0] is None
        assert g.limit == 4
        assert g.would_admit() is False
        clock["t"] = 12.0
        cap, _ = g.on_429(8.0)
        assert cap == 2
        clock["t"] = 80.0
        assert g.would_admit() is True

    def test_floor_is_2(self):
        from core.translator import GtxThrottle

        clock = {"t": 0.0}
        g = GtxThrottle(200, clock=lambda: clock["t"])
        for i in range(20):
            clock["t"] = float(i) * 2.0
            g.on_429(8.0)
        assert g.limit == 2

    def test_success_climbs_toward_ceiling(self):
        from core.translator import GtxThrottle

        g = GtxThrottle(200)
        g.limit = 2
        for _ in range(20):
            g.on_success()
        assert g.limit == 22
        for _ in range(300):
            g.on_success()
        assert g.limit == 200

    def test_429_then_release_does_not_refill_during_cool(self):
        from core.translator import GtxThrottle

        clock = {"t": 1.0}
        g = GtxThrottle(200, clock=lambda: clock["t"])
        assert g.acquire(lambda: False, lambda: None, lambda _s: None)
        g.on_429(8.0)
        g.release()
        assert g.current == 0
        assert g.would_admit() is False
        clock["t"] = 20.0
        assert g.would_admit() is True

    def test_gated_request_holds_slot_during_call(self):
        from core.translator import RateLimitedError

        t = GoogleTranslator(max_workers=200)
        t.max_retries = 1
        held = []

        def fake(_text):
            held.append(t._gtx.current)
            raise RateLimitedError(1.0)

        t._request_translation = fake
        t._translate_single("你好世界测试段落足够长", 0)
        assert held == [1]
        assert t._gtx.current == 0


class TestGtxDedupeAndJitter:
    def test_needs_gtx_request_skips_non_cjk(self):
        from core.translator import needs_gtx_request

        assert needs_gtx_request("你好世界") is True
        assert needs_gtx_request("……") is False
        assert needs_gtx_request("Hello") is False
        assert needs_gtx_request("   ") is False

    def test_identical_nodes_share_one_google_get(self):
        t = make_translator()
        seen = []

        def fake(text):
            seen.append(text)
            return "The hero walked away."

        t._request_translation = fake
        src = "中文段落测试内容这是一个很长的句子"
        results = t.translate_texts([src, src, "  " + src + "  ", "……", "Hello"])
        assert seen == [src]
        assert results[0] == results[1] == results[2] == "The hero walked away."
        assert results[3] == "……"
        assert results[4] == "Hello"
        assert t.stats["requests"] == 1

    def test_jittered_backoff_stays_in_the_v264_band(self):
        from core.translator import jittered_backoff_seconds

        samples = [jittered_backoff_seconds(0) for _ in range(40)]
        assert all(1.5 <= s <= 3.0 for s in samples)
        assert min(samples) < max(samples)

    def test_429_uses_global_cool_not_only_that_worker(self, monkeypatch):
        from core.translator import RateLimitedError

        t = GoogleTranslator(max_workers=200)
        t.max_retries = 1
        monkeypatch.setattr(t, "_interruptible_sleep", lambda _s: None)
        monkeypatch.setattr(
            t, "_request_translation", lambda _t: (_ for _ in ()).throw(RateLimitedError(1.0))
        )
        t._translate_single("你好世界测试段落足够长", 0)
        assert t._gtx.limit <= 8
        assert t._gtx.would_admit() is False


class TestProgressEmits:
    def test_in_flight_stays_up_until_the_request_finishes(self):
        from core.translator import RateLimitedError

        t = make_translator(max_retries=1)
        seen = []

        def fake_request(_text):
            seen.append(t._in_flight)
            raise RateLimitedError(1.0)

        t._request_translation = fake_request
        t.total = 1
        index, out = t._translate_single("你好世界测试段落足够长", 0)
        assert index == 0
        assert out == "你好世界测试段落足够长"
        assert seen == [1]
        assert t._in_flight == 0

    def test_progress_callback_is_throttled_in_a_wave(self):
        t = make_translator()
        calls = []
        t.total = 100
        t.completed = 0
        t.progress_callback = lambda done, total: calls.append(done)
        for _ in range(40):
            t._update_progress()
        # First completion is forced; the rest of the same wave is coalesced.
        assert calls[0] == 1
        assert len(calls) < 40
        t._emit_progress(force=True)
        assert calls[-1] == 40

    def test_translate_emits_progress_before_first_http(self):
        t = GoogleTranslator(max_workers=200)
        order = []

        def cb(done, total):
            order.append(
                (
                    "progress",
                    done,
                    total,
                    int(t._unique_requests or 0),
                    int(t._in_flight or 0),
                )
            )

        def fake_request(_text):
            order.append(("http",))
            return "Hello world. This is translated English."

        t._request_translation = fake_request
        t.translate_texts(["你好世界测试段落足够长"], cb)
        assert order
        assert order[0][0] == "progress"
        assert order[0][1] == 0
        http_at = next(i for i, item in enumerate(order) if item[0] == "http")
        assert http_at > 0
        before_http = order[:http_at]
        assert any(item[0] == "progress" for item in before_http)
        assert any(item[0] == "progress" and item[3] == 1 for item in before_http)
        assert any(
            item[0] == "progress" and item[4] >= 1 for item in before_http
        )

    def test_google_pool_uses_ceiling_not_start_cap(self, monkeypatch):
        import concurrent.futures as cf

        t = GoogleTranslator(max_workers=200)
        sizes = []
        real = cf.ThreadPoolExecutor

        def capture(*a, **k):
            sizes.append(k.get("max_workers") if "max_workers" in k else (a[0] if a else None))
            return real(*a, **k)

        monkeypatch.setattr(cf, "ThreadPoolExecutor", capture)
        t._request_translation = lambda _t: "Hello world. This is translated English."
        texts = [f"你好世界测试段落{i}足够长" for i in range(40)]
        t.translate_texts(texts)
        assert sizes
        assert sizes[0] == 40

    def test_mark_source_progress_tracks_lowest_in_flight(self):
        t = make_translator()
        t._mark_source_progress(10, inflight=True)
        t._mark_source_progress(4, inflight=True)
        assert t._progress_source_index == 4
        t._mark_source_progress(4, inflight=False)
        assert t._progress_source_index == 10
        t._mark_source_progress(10, inflight=False)
        assert t._progress_source_index == 10


class TestBackends:
    def test_invalid_backend_falls_back_to_google(self):
        t = make_translator(backend='nonsense')
        assert t.backend == 'google'

    def test_libretranslate_request(self, monkeypatch):
        t = make_translator(backend='libretranslate', libretranslate_url='https://lt.example/')
        assert t.libretranslate_url == 'https://lt.example'

        captured = {}

        def on_post(url, json=None, headers=None, timeout=None, data=None):
            captured['url'] = url
            captured['json'] = json
            return _FakeResponse({'translatedText': 'Hello'})

        monkeypatch.setattr(t, '_get_http_session', lambda: _FakeSession(on_post=on_post))
        # Skip live DNS for the fake host used in this unit test
        monkeypatch.setattr(
            'core.security.validate_fetch_url',
            lambda *a, **k: None,
        )

        assert t._request_translation('你好') == 'Hello'
        assert captured['url'] == 'https://lt.example/translate'
        assert captured['json']['source'] == 'zh'  # zh-CN mapped to plain ISO code

    def test_google_skips_dns_on_hardcoded_endpoint(self, monkeypatch):
        import socket

        t = make_translator(backend='google')
        monkeypatch.setattr(
            socket, 'getaddrinfo',
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError('Google translate must not DNS-check every request')
            ),
        )

        def on_request(method, url, **kwargs):
            assert 'translate-pa.googleapis.com' in url
            return _FakeResponse({'translation': 'Hello'})

        monkeypatch.setattr(t, '_get_http_session', lambda: _FakeSession(on_request=on_request))
        assert t._request_translation('你好') == 'Hello'

    def test_google_old_gtx_endpoint(self, monkeypatch):
        t = make_translator(backend='google_gtx')
        assert t.backend == 'google_gtx'

        def on_request(method, url, **kwargs):
            assert 'translate.googleapis.com' in url
            return _FakeResponse({'sentences': [{'trans': 'Hello'}]})

        monkeypatch.setattr(t, '_get_http_session', lambda: _FakeSession(on_request=on_request))
        assert t._request_google('你好') == 'Hello'

    def test_google_html_endpoint(self, monkeypatch):
        t = make_translator(backend='google_html')
        captured = {}

        def on_request(method, url, **kwargs):
            captured['url'] = url
            captured['headers'] = kwargs.get('headers') or {}
            return _FakeResponse([['Hello']])

        monkeypatch.setattr(t, '_get_http_session', lambda: _FakeSession(on_request=on_request))
        assert t._request_google('你好') == 'Hello'
        assert 'translateHtml' in captured['url']
        assert captured['headers'].get('X-Goog-Api-Key')

    def test_microsoft_edge_request(self, monkeypatch):
        t = make_translator(backend='microsoft')
        assert t.backend == 'microsoft'
        assert t._gtx is not None

        def on_request(method, url, **kwargs):
            if 'translate/auth' in url:
                return _FakeResponse(text="aaa.bbb.sig")
            assert 'microsofttranslator.com' in url
            return _FakeResponse([{'translations': [{'text': 'Hello'}]}])

        monkeypatch.setattr(t, '_get_http_session', lambda: _FakeSession(on_request=on_request))
        assert t._request_translation('你好') == 'Hello'
        assert t._microsoft_lang('zh-CN') == 'zh-Hans'

    def test_ollama_caps_workers_and_namespaces_cache(self):
        t = GoogleTranslator(
            max_workers=200,
            backend='ollama',
            ollama_url='http://127.0.0.1:11434',
            ollama_model='qwen2.5',
        )
        assert t.backend == 'ollama'
        assert t.max_workers == 8
        assert t._cache_backend() == 'ollama:qwen2.5'
        assert t.request_timeout == 180

    def test_ollama_request(self, monkeypatch):
        t = make_translator(
            backend='ollama',
            ollama_url='http://127.0.0.1:11434',
            ollama_model='qwen2.5',
        )
        captured = {}

        def on_post(url, json=None, headers=None, timeout=None, data=None):
            captured['url'] = url
            captured['json'] = json
            return _FakeResponse({'message': {'content': 'Hello'}})

        monkeypatch.setattr(t, '_get_http_session', lambda: _FakeSession(on_post=on_post))
        assert t._request_translation('你好') == 'Hello'
        assert captured['url'] == 'http://127.0.0.1:11434/api/chat'
        assert captured['json']['model'] == 'qwen2.5'
        assert captured['json']['stream'] is False


class TestOllamaDevice:
    def test_env_forces_cpu(self, monkeypatch):
        from core import ollama_setup as setup
        from core import translator as tr
        monkeypatch.setenv('HUAEPUB_OLLAMA_GPU', '0')
        setup._gpu_cached = None
        assert tr.ollama_gpu_available() is False
        opts = tr.ollama_infer_options()
        assert opts['num_gpu'] == 0
        assert opts['num_thread'] >= 2

    def test_env_forces_gpu(self, monkeypatch):
        from core import ollama_setup as setup
        from core import translator as tr
        monkeypatch.setenv('HUAEPUB_OLLAMA_GPU', '1')
        setup._gpu_cached = None
        assert tr.ollama_gpu_available() is True
        opts = tr.ollama_infer_options()
        assert opts['num_gpu'] == 99


class TestOllamaPolish:
    def test_pack_unpack_roundtrip(self):
        from core.translator import pack_numbered_segments, unpack_numbered_segments
        texts = ['Hello world.', 'She go to school.']
        blob = pack_numbered_segments(texts)
        assert '<<<1>>>' in blob and '<<<2>>>' in blob
        assert unpack_numbered_segments(blob, 2) == texts

    def test_unpack_rejects_missing_marker(self):
        from core.translator import unpack_numbered_segments, unpack_sparse_segments
        assert unpack_numbered_segments('Hello world.', 2) is None
        assert unpack_sparse_segments('Hello world.', 2) is None
        sparse = unpack_sparse_segments('<<<1>>>\nHi\n<<<3>>>\nBye', 3)
        assert sparse == {1: 'Hi', 3: 'Bye'}
        assert unpack_sparse_segments('NONE', 4) == {}

    def test_polish_batches_and_keeps_original_on_bad_output(self, monkeypatch):
        t = make_translator(backend='google', ollama_url='http://127.0.0.1:11434')
        seen = []

        def fake_polish(texts, **kwargs):
            seen.append(list(texts))
            out = []
            for text in texts:
                if 'go to school' in text:
                    out.append('She goes to school every morning.')
                else:
                    out.append(text)
            return out, 'fake-model'

        monkeypatch.setattr('core.local_polish.polish_paragraphs', fake_polish)
        fluent = 'He walked into the room and sat down by the window.'
        awkward = 'She go to school every morning.'
        out = t.polish_texts([fluent, awkward], max_chars=5000)
        assert out[0] == fluent
        assert out[1] == 'She goes to school every morning.'
        assert len(seen) == 1
        assert seen[0] == [awkward]

        def boom(texts, **kwargs):
            raise RuntimeError('server down')

        monkeypatch.setattr('core.local_polish.polish_paragraphs', boom)
        bad = t.polish_texts(
            ['this broken english here is really awkward.'],
            max_chars=5000,
        )
        assert bad == ['this broken english here is really awkward.']

    def test_polish_skips_fluent_google_english(self, monkeypatch):
        from core.translator import should_polish_english
        assert should_polish_english(
            'He walked into the room and sat down by the window.'
        ) is False
        t = make_translator()

        def boom(*a, **k):
            raise AssertionError('skip fluent')

        monkeypatch.setattr('core.local_polish.polish_paragraphs', boom)
        text = 'He walked into the room and sat down by the window.'
        assert t.polish_texts([text]) == [text]

    def test_polish_skips_still_chinese(self, monkeypatch):
        t = make_translator()
        monkeypatch.setattr(
            'core.local_polish.polish_paragraphs',
            lambda *a, **k: (_ for _ in ()).throw(AssertionError('should skip')),
        )
        assert t.polish_texts(['这是中文段落内容测试']) == ['这是中文段落内容测试']

    def test_polish_skips_short_titles(self, monkeypatch):
        from core.translator import should_polish_english
        assert should_polish_english('Chapter 1') is False
        assert should_polish_english('Li Ming') is False
        assert should_polish_english('She go to school every morning.') is True

        t = make_translator()
        monkeypatch.setattr(
            'core.local_polish.polish_paragraphs',
            lambda *a, **k: (_ for _ in ()).throw(AssertionError('skip titles')),
        )
        assert t.polish_texts(['Chapter 12', 'Li Ming']) == ['Chapter 12', 'Li Ming']

    def test_polish_skip_token_keeps_original(self, monkeypatch):
        t = make_translator()

        def identity(texts, **kwargs):
            return list(texts), 'fake-model'

        monkeypatch.setattr('core.local_polish.polish_paragraphs', identity)
        assert t.polish_texts(['She go to school every morning.']) == [
            'She go to school every morning.'
        ]


class TestOllamaModelPick:
    def test_keeps_preferred_when_nothing_installed(self):
        from core.translator import resolve_ollama_model
        assert resolve_ollama_model('qwen2.5', []) == 'qwen2.5'

    def test_exact_match(self):
        from core.translator import resolve_ollama_model
        assert resolve_ollama_model('llama3.2:latest', ['mistral', 'llama3.2:latest']) == 'llama3.2:latest'

    def test_family_match_prefers_installed_qwen(self):
        from core.translator import resolve_ollama_model
        assert resolve_ollama_model('qwen2.5:3b', ['llama3.2:latest', 'qwen2.5:3b']) == 'qwen2.5:3b'
        assert resolve_ollama_model('qwen2.5:3b', ['qwen2.5:7b']) == 'qwen2.5:7b'

    def test_falls_back_to_first_installed(self):
        from core.translator import resolve_ollama_model
        assert resolve_ollama_model('qwen2.5', ['llama3.2:latest', 'mistral']) == 'llama3.2:latest'

    def test_list_models_empty_on_failure(self, monkeypatch):
        from core.translator import list_ollama_models
        import core.security as sec

        def boom(*a, **k):
            raise ConnectionError('refused')

        monkeypatch.setattr(sec, 'safe_http_request', boom)
        assert list_ollama_models('http://127.0.0.1:11434') == []

    def test_list_models_parses_tags(self, monkeypatch):
        from core.translator import list_ollama_models
        import core.security as sec

        payload = {'models': [{'name': 'llama3.2:latest'}, {'name': 'mistral:latest'}]}
        import requests as req_mod
        monkeypatch.setattr(
            req_mod, 'Session',
            lambda: _FakeSession(on_request=lambda *a, **k: _FakeResponse(payload)),
        )
        monkeypatch.setattr(sec, 'safe_http_request', lambda *a, **k: _FakeResponse(payload))
        assert list_ollama_models() == ['llama3.2:latest', 'mistral:latest']

    def test_probe_none_when_down(self, monkeypatch):
        from core.translator import probe_ollama
        import core.security as sec
        monkeypatch.setattr(sec, 'safe_http_request', lambda *a, **k: (_ for _ in ()).throw(ConnectionError('refused')))
        assert probe_ollama() is None

    def test_model_installed_does_not_confuse_sizes(self):
        from core.translator import ollama_model_installed
        assert ollama_model_installed('qwen2.5:3b', ['qwen2.5:3b'])
        assert ollama_model_installed('qwen2.5:3b', ['qwen2.5:3b:latest'])
        assert not ollama_model_installed('qwen2.5:3b', ['qwen2.5:7b'])
        assert not ollama_model_installed('qwen2.5:3b', [])

    def test_pull_streams_until_success(self, monkeypatch):
        from core.translator import pull_ollama_model
        import core.security as sec

        lines = [
            b'{"status":"pulling manifest"}',
            b'{"status":"downloading","total":100,"completed":40}',
            b'{"status":"success"}',
        ]
        seen = []

        def fake_safe(session, method, url, **kwargs):
            assert method == 'POST'
            assert url.endswith('/api/pull')
            assert kwargs.get('allow_loopback') is True
            assert kwargs.get('stream') is True
            return _FakeResponse(status_code=200, lines=lines)

        monkeypatch.setattr(sec, 'safe_http_request', fake_safe)
        pull_ollama_model('qwen2.5:3b', progress_callback=lambda p, s: seen.append((p, s)))
        assert seen[-1] == (100, 'success')
        assert any(p == 40 for p, _ in seen)

    def test_pull_rejects_non_loopback(self):
        from core.translator import pull_ollama_model

        with pytest.raises(ValueError, match='localhost|Blocked|Invalid'):
            pull_ollama_model('qwen2.5:3b', ollama_url='https://example.com/ollama')

    def test_ollama_is_installed_uses_path(self, monkeypatch, tmp_path):
        import shutil
        import core.translator as tr

        monkeypatch.setattr(shutil, 'which', lambda _name: str(tmp_path / 'ollama'))
        assert tr.ollama_is_installed() is True

        monkeypatch.setattr(shutil, 'which', lambda _name: None)
        # Do not patch os.name — pathlib.Path then tries WindowsPath and
        # crashes pytest on Linux CI.
        monkeypatch.setattr("core.ollama_setup._is_windows", lambda: True)
        monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
        monkeypatch.setenv('PROGRAMFILES', str(tmp_path / 'pf'))
        monkeypatch.setenv('PROGRAMFILES(X86)', str(tmp_path / 'pf86'))
        assert tr.ollama_is_installed() is False

        exe = tmp_path / 'local' / 'Programs' / 'Ollama' / 'ollama.exe'
        exe.parent.mkdir(parents=True)
        exe.write_text('')
        assert tr.ollama_is_installed() is True

    def test_ollama_is_installed_posix_home_path(self, monkeypatch, tmp_path):
        import shutil
        import core.translator as tr
        from pathlib import Path

        monkeypatch.setattr(shutil, 'which', lambda _name: None)
        monkeypatch.setattr("core.ollama_setup._is_windows", lambda: False)
        monkeypatch.setattr(Path, 'home', staticmethod(lambda: tmp_path))
        assert tr.ollama_is_installed() is False
        exe = tmp_path / '.local' / 'bin' / 'ollama'
        exe.parent.mkdir(parents=True)
        exe.write_text('')
        assert tr.ollama_is_installed() is True


class TestHttpSession:
    def test_session_reused_in_same_thread(self):
        t = make_translator()
        a = t._get_http_session()
        b = t._get_http_session()
        assert a is b

    def test_caps_google_workers_at_packed_max(self):
        from core.translator import MAX_PACKED_WORKERS

        t = GoogleTranslator(max_workers=500)
        assert t.max_workers == MAX_PACKED_WORKERS
        assert t.max_workers == 200

    def test_parse_retry_after_and_429(self, monkeypatch):
        from core.translator import RateLimitedError, parse_retry_after

        class Resp:
            headers = {"Retry-After": "3"}

        assert parse_retry_after(Resp()) == 3.0
        t = make_translator()

        def on_request(*a, **k):
            resp = _FakeResponse({}, status_code=429, headers={"Retry-After": "1.5"})

            def boom():
                raise AssertionError("429 must not reach raise_for_status")

            resp.raise_for_status = boom  # type: ignore[method-assign]
            return resp

        monkeypatch.setattr(t, "_get_http_session", lambda: _FakeSession(on_request=on_request))
        try:
            t._request_google("你好")
            assert False, "expected RateLimitedError"
        except RateLimitedError as exc:
            assert exc.retry_after == 1.5
