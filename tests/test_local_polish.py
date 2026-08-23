"""Offline tests for KEEP/REPLACE polish wiring and download guards."""

import hashlib
import zipfile

import pytest

from core.local_polish import wants_polish
from core.polish.engine import EngineError
from core.translator import should_polish_english


def test_wants_polish_skips_fluent_and_titles():
    assert wants_polish("He walked into the room and sat down by the window.") is False
    assert wants_polish("Chapter 12") is False
    assert wants_polish("Li Ming") is False
    assert wants_polish("这是中文段落内容测试") is False


def test_wants_polish_catches_grammar_and_mtl_calques():
    assert should_polish_english("She go to school every morning.") is True
    assert wants_polish("She go to school every morning.") is True
    assert wants_polish(
        "The corners of her mouth raised slightly, revealing a playful smile."
    ) is True
    assert wants_polish("Jiang Kai's eyes narrowed.") is True


def test_polish_stack_lives_in_huaepub():
    import core.polish.api as api
    import core.polish.paths as paths

    path = api.__file__.replace("\\", "/")
    assert "/core/polish/" in path
    assert "copydecode" not in path
    assert not hasattr(paths, "extra_model_dirs")
    assert api.wants_polish("Jiang Kai's eyes narrowed.") is True


class _DummyProgress:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def add_task(self, *a, **k):
        return 1

    def advance(self, *a, **k):
        pass


class _StreamResp:
    def __init__(self, payload=b"abc", status=200):
        self.status_code = status
        self.headers = {"Content-Length": str(len(payload))}
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        yield self._payload

    def close(self):
        pass


def test_download_file_rejects_bad_hash(tmp_path, monkeypatch):
    from core.polish import serve

    monkeypatch.setattr(serve, "safe_http_request", lambda *a, **k: _StreamResp())
    monkeypatch.setattr(serve, "Progress", _DummyProgress)
    dest = tmp_path / "qwen2.5-3b-instruct-q4_k_m.gguf"
    with pytest.raises(EngineError, match="SHA256"):
        serve.download_file(
            "https://huggingface.co/Qwen/x.gguf",
            dest,
            expected_sha256="0" * 64,
        )
    assert not dest.exists()


def test_download_file_accepts_matching_hash(tmp_path, monkeypatch):
    from core.polish import serve

    payload = b"abc"
    monkeypatch.setattr(
        serve, "safe_http_request", lambda *a, **k: _StreamResp(payload)
    )
    monkeypatch.setattr(serve, "Progress", _DummyProgress)
    dest = tmp_path / "model.gguf"
    serve.download_file(
        "https://huggingface.co/Qwen/x.gguf",
        dest,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    assert dest.read_bytes() == payload


def test_extract_rejects_zip_slip(tmp_path):
    from core.polish import serve

    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.exe", b"nope")
    with pytest.raises(ValueError):
        serve._extract(archive, tmp_path / "out")
    assert not (tmp_path / "escape.exe").exists()


def test_github_latest_release_uses_cache(tmp_path, monkeypatch):
    from core.polish import serve

    monkeypatch.setattr(serve, "cache_dir", lambda: tmp_path)
    (tmp_path / "llama.cpp-release.json").write_text(
        '{"tag_name":"cached"}', encoding="utf-8"
    )

    def boom(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(serve, "safe_http_request", boom)
    data = serve.github_latest_release()
    assert data["tag_name"] == "cached"
