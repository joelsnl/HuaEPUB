"""Offline tests for Offline NMT download gating (no Hugging Face traffic)."""

from pathlib import Path

import pytest

from core.translation import nmt as nmtmod


@pytest.fixture(autouse=True)
def _reset_nmt_download_state():
    nmtmod.reset_nmt_download_state_for_tests()
    yield
    nmtmod.reset_nmt_download_state_for_tests()


def test_ensure_nmt_model_remembers_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(nmtmod, "nmt_model_dir", lambda: tmp_path / "opus")
    calls = []

    def boom(url, path, log=None):
        calls.append(url)
        raise RuntimeError("Polish download host not allowed: us.aws.cdn.hf.co")

    monkeypatch.setattr("core.polish.serve.download_file", boom)
    with pytest.raises(RuntimeError, match="not allowed"):
        nmtmod.ensure_nmt_model()
    with pytest.raises(RuntimeError, match="not allowed"):
        nmtmod.ensure_nmt_model()
    assert len(calls) == 1
    assert nmtmod.nmt_download_failed()


def _dummy_model(tmp_path):
    for name in ("model.bin", "config.json", "source.spm", "target.spm"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    return tmp_path


class _FakeSP:
    def load(self, path):
        return True

    def encode(self, text, out_type=str):
        return ["x"]

    def decode(self, pieces):
        return "hello"


class _FakeSPMod:
    SentencePieceProcessor = _FakeSP


def test_cublas_error_is_cuda_runtime():
    assert nmtmod._looks_like_cuda_runtime_error(
        RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
    )
    assert not nmtmod._looks_like_cuda_runtime_error(RuntimeError("out of memory"))


def test_device_candidates_skip_cuda_when_broken(monkeypatch):
    nmtmod._cuda_broken = True
    assert nmtmod._device_candidates() == [("cpu", "int8")]


def test_cuda_cublas_warmup_falls_back_to_cpu(tmp_path, monkeypatch):
    model_dir = _dummy_model(tmp_path)
    monkeypatch.setattr(nmtmod, "nmt_runtime_available", lambda: True)
    monkeypatch.setattr(nmtmod, "_sentencepiece", lambda: _FakeSPMod)
    devices = []

    class FakeT:
        def __init__(self, device):
            self.device = device

        def translate_batch(self, tokens, **kwargs):
            if self.device == "cuda":
                raise RuntimeError(
                    "Library cublas64_12.dll is not found or cannot be loaded"
                )
            class Item:
                hypotheses = [["hello"]]
            return [Item() for _ in tokens]

    def fake_make(_model_dir, device, compute):
        devices.append(device)
        return FakeT(device)

    monkeypatch.setattr(nmtmod, "_make_translator", fake_make)
    monkeypatch.setattr(
        nmtmod, "_device_candidates", lambda: [("cuda", "int8_float16"), ("cpu", "int8")]
    )
    engine = nmtmod.CTranslate2Engine(model_dir)
    assert engine.translate_batch(["你好"]) == ["hello"]
    assert devices == ["cuda", "cpu"]
    assert engine._device == "cpu"
    assert nmtmod._cuda_broken


def test_cuda_batch_failure_reloads_cpu(tmp_path, monkeypatch):
    model_dir = _dummy_model(tmp_path)
    monkeypatch.setattr(nmtmod, "nmt_runtime_available", lambda: True)
    monkeypatch.setattr(nmtmod, "_sentencepiece", lambda: _FakeSPMod)

    class FakeT:
        def __init__(self, device):
            self.device = device
            self.calls = 0

        def translate_batch(self, tokens, **kwargs):
            self.calls += 1
            if self.device == "cuda":
                if self.calls == 1:
                    return []
                raise RuntimeError(
                    "Library cublas64_12.dll is not found or cannot be loaded"
                )
            class Item:
                hypotheses = [["hello"]]
            return [Item() for _ in tokens]

    monkeypatch.setattr(
        nmtmod, "_make_translator", lambda *_a, **_k: FakeT(_k.get("device") or _a[1])
    )
    monkeypatch.setattr(
        nmtmod,
        "_device_candidates",
        lambda: (
            [("cpu", "int8")]
            if nmtmod._cuda_broken
            else [("cuda", "int8_float16"), ("cpu", "int8")]
        ),
    )
    engine = nmtmod.CTranslate2Engine(model_dir)
    assert engine.translate_batch(["你好世界"]) == ["hello"]
    assert engine._device == "cpu"


def test_cuda_library_dirs_from_cuda_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "cublas64_12.dll").write_bytes(b"x")
    monkeypatch.setenv("CUDA_PATH", str(tmp_path))
    dirs = nmtmod.cuda_library_dirs()
    assert any(p.resolve() == bin_dir.resolve() for p in dirs)
    assert nmtmod.cublas_present()


def test_install_instructions_name_cuda12_packages():
    text = nmtmod.nmt_cuda_install_instructions()
    assert "nvidia-cublas-cu12" in text
    assert "cublas64_12" in text
    assert "13" in text


def test_prepare_cuda_does_not_pip_under_pytest(monkeypatch):
    monkeypatch.setattr(nmtmod, "cublas_present", lambda: False)
    monkeypatch.setattr(nmtmod, "register_cuda_library_dirs", lambda: [])

    def boom(_log):
        raise AssertionError("pip must not run during tests")

    monkeypatch.setattr(nmtmod, "_pip_install_cuda_libs", boom)
    assert nmtmod.prepare_cuda_runtime() is False


def test_register_keeps_add_dll_directory_cookies(monkeypatch):
    class Cookie:
        def close(self):
            pass

    def fake_add(_path):
        return Cookie()

    monkeypatch.setattr(nmtmod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(nmtmod.os, "add_dll_directory", fake_add, raising=False)
    monkeypatch.setattr(nmtmod, "cuda_library_dirs", lambda: [Path(".")])
    monkeypatch.setattr(nmtmod, "_preload_cuda_shared_libs", lambda _dirs: None)
    nmtmod.register_cuda_library_dirs()
    assert nmtmod._cuda_dll_cookies
    assert type(nmtmod._cuda_dll_cookies[0]).__name__ == "Cookie"

