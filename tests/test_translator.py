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

        class FakeSession:
            def post(self, url, json=None, headers=None, timeout=None, data=None):
                captured['url'] = url
                captured['json'] = json
                return FakeResponse()

            def get(self, *a, **k):
                raise AssertionError('unexpected GET')

        monkeypatch.setattr(t, '_get_http_session', lambda: FakeSession())
        # Skip live DNS for the fake host used in this unit test
        monkeypatch.setattr(
            'core.security.validate_fetch_url',
            lambda *a, **k: None,
        )

        assert t._request_translation('你好') == 'Hello'
        assert captured['url'] == 'https://lt.example/translate'
        assert captured['json']['source'] == 'zh'  # zh-CN mapped to plain ISO code

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

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {'message': {'content': 'Hello'}}

        class FakeSession:
            def post(self, url, json=None, headers=None, timeout=None, data=None):
                captured['url'] = url
                captured['json'] = json
                return FakeResponse()

            def get(self, *a, **k):
                raise AssertionError('unexpected GET')

        monkeypatch.setattr(t, '_get_http_session', lambda: FakeSession())
        assert t._request_translation('你好') == 'Hello'
        assert captured['url'] == 'http://127.0.0.1:11434/api/chat'
        assert captured['json']['model'] == 'qwen2.5'
        assert captured['json']['stream'] is False


class TestOllamaDevice:
    def test_env_forces_cpu(self, monkeypatch):
        from core import translator as tr
        monkeypatch.setenv('HUAEPUB_OLLAMA_GPU', '0')
        tr._gpu_cached = None
        assert tr.ollama_gpu_available() is False
        opts = tr.ollama_infer_options()
        assert opts['num_gpu'] == 0
        assert opts['num_thread'] >= 2

    def test_env_forces_gpu(self, monkeypatch):
        from core import translator as tr
        monkeypatch.setenv('HUAEPUB_OLLAMA_GPU', '1')
        tr._gpu_cached = None
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

    def test_polish_batches_and_keeps_original_on_bad_output(self):
        t = make_translator(backend='google', ollama_url='http://127.0.0.1:11434')
        seen = []

        def fake_ollama(text, **kwargs):
            seen.append(text)
            if 'broken' in text:
                return 'not marked up at all'
            return '<<<1>>>\nShe goes to school every morning.'

        t._request_ollama = fake_ollama
        fluent = 'He walked into the room and sat down by the window.'
        awkward = 'She go to school every morning.'
        out = t.polish_texts([fluent, awkward], max_chars=5000)
        assert out[0] == fluent
        assert out[1] == 'She goes to school every morning.'
        assert len(seen) == 1

        bad = t.polish_texts(
            ['this broken english here is really awkward.'],
            max_chars=5000,
        )
        assert bad == ['this broken english here is really awkward.']

    def test_polish_skips_fluent_google_english(self):
        from core.translator import should_polish_english
        assert should_polish_english(
            'He walked into the room and sat down by the window.'
        ) is False
        t = make_translator()
        t._request_ollama = lambda *a, **k: (_ for _ in ()).throw(AssertionError('skip fluent'))
        text = 'He walked into the room and sat down by the window.'
        assert t.polish_texts([text]) == [text]

    def test_polish_skips_still_chinese(self):
        t = make_translator()
        t._request_ollama = lambda *a, **k: (_ for _ in ()).throw(AssertionError('should skip'))
        assert t.polish_texts(['这是中文段落内容测试']) == ['这是中文段落内容测试']

    def test_polish_skips_short_titles(self):
        from core.translator import should_polish_english
        assert should_polish_english('Chapter 1') is False
        assert should_polish_english('Li Ming') is False
        assert should_polish_english('She go to school every morning.') is True

        t = make_translator()
        t._request_ollama = lambda *a, **k: (_ for _ in ()).throw(AssertionError('skip titles'))
        assert t.polish_texts(['Chapter 12', 'Li Ming']) == ['Chapter 12', 'Li Ming']

    def test_polish_skip_token_keeps_original(self):
        t = make_translator()
        t._request_ollama = lambda *a, **k: '<<<1>>>\nSKIP'
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

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {'models': [{'name': 'llama3.2:latest'}, {'name': 'mistral:latest'}]}

        class FakeSession:
            def request(self, *a, **k):
                return FakeResponse()

        import requests as req_mod
        monkeypatch.setattr(req_mod, 'Session', lambda: FakeSession())
        monkeypatch.setattr(sec, 'safe_http_request', lambda *a, **k: FakeResponse())
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
        import requests as req_mod

        lines = [
            b'{"status":"pulling manifest"}',
            b'{"status":"downloading","total":100,"completed":40}',
            b'{"status":"success"}',
        ]
        seen = []

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def iter_lines(self):
                return iter(lines)

            def close(self):
                pass

        class FakeSession:
            def post(self, *a, **k):
                assert k.get('stream') is True
                assert k.get('allow_redirects') is False
                return FakeResponse()

        monkeypatch.setattr(req_mod, 'Session', lambda: FakeSession())
        pull_ollama_model('qwen2.5:3b', progress_callback=lambda p, s: seen.append((p, s)))
        assert seen[-1] == (100, 'success')
        assert any(p == 40 for p, _ in seen)

    def test_ollama_is_installed_uses_path(self, monkeypatch, tmp_path):
        import shutil
        import core.translator as tr

        monkeypatch.setattr(shutil, 'which', lambda _name: str(tmp_path / 'ollama'))
        assert tr.ollama_is_installed() is True

        monkeypatch.setattr(shutil, 'which', lambda _name: None)
        # Do not patch os.name — pathlib.Path then tries WindowsPath and
        # crashes pytest on Linux CI.
        monkeypatch.setattr(tr, '_is_windows', lambda: True)
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
        monkeypatch.setattr(tr, '_is_windows', lambda: False)
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

    def test_respects_requested_worker_count(self):
        t = GoogleTranslator(max_workers=200)
        assert t.max_workers == 200
