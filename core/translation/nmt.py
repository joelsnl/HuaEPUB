# Author: joelsnl
"""
Optional CTranslate2 adapter for Helsinki-NLP opus-mt-zh-en.

Not a hard dependency: frozen builds and CI stay lean. Install with
``pip install -r requirements-nmt.txt`` then pick Translator → Offline NMT.

GPU: CTranslate2 needs CUDA 12 libraries (cublas64_12.dll), not only an
NVIDIA driver. We register toolkit / pip / Ollama DLL dirs via
``os.add_dll_directory``. CUDA 13 is the wrong major.

Weights are downloaded from Hugging Face https only
(``validate_polish_download_url``). No invented SHA256. Cache lives in
``~/.huaepub/nmt/`` and is never Drive-synced.
"""

from __future__ import annotations

import os
import platform
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable, Optional

Log = Callable[[str], None]

_download_lock = threading.Lock()
_download_error: Optional[str] = None
_cuda_broken = False
_cuda_broken_logged = False
_cuda_prepared = False
_cuda_dll_dirs_registered: set[str] = set()
# os.add_dll_directory() is undone when the returned cookie is GC'd.
_cuda_dll_cookies: list[object] = []
_cuda_preload_logged = False

# CTranslate2 Windows/Linux wheels are built against CUDA 12 (cublas64_12.dll).
# CUDA 13 toolkits do not provide that DLL. cuDNN is not required for opus-mt.
_CUDA_PIP_PACKAGES = ("nvidia-cublas-cu12", "nvidia-cuda-runtime-cu12")
_CUBLAS_NAMES = ("cublas64_12.dll", "libcublas.so.12")
_CUDA_TOOLKIT_ARCHIVE = "https://developer.nvidia.com/cuda-12-9-0-download-archive"


# Community CTranslate2 conversion of Helsinki-NLP/opus-mt-zh-en (CC-BY-4.0).
# File list matches the HF repo; hashes are not published — do not invent them.
_HF_REPO = "https://huggingface.co/Sams200/opus-mt-zh-en/resolve/main"
_MODEL_FILES = (
    "config.json",
    "shared_vocabulary.json",
    "source.spm",
    "target.spm",
    "model.bin",
)
_MODEL_DIRNAME = "opus-mt-zh-en"


def nmt_cache_dir() -> Path:
    from core.settings import get_data_dir

    path = get_data_dir() / "nmt"
    path.mkdir(parents=True, exist_ok=True)
    return path


def nmt_model_dir() -> Path:
    return nmt_cache_dir() / _MODEL_DIRNAME


def nmt_runtime_available() -> bool:
    try:
        import ctranslate2  # noqa: F401
        import sentencepiece  # noqa: F401
    except ImportError:
        return False
    return True


def nmt_model_ready(model_dir: Optional[Path] = None) -> bool:
    root = Path(model_dir) if model_dir else nmt_model_dir()
    needed = ("model.bin", "config.json", "source.spm", "target.spm")
    return all((root / name).is_file() for name in needed)


def _log(message: str) -> None:
    print(message)


def nmt_download_failed() -> bool:
    """True after a failed model fetch in this process (do not retry forever)."""
    return _download_error is not None


def reset_nmt_download_state_for_tests() -> None:
    global _download_error, _cuda_broken, _cuda_broken_logged, _cuda_prepared
    global _cuda_preload_logged
    with _download_lock:
        _download_error = None
        _cuda_broken = False
        _cuda_broken_logged = False
        _cuda_prepared = False
        _cuda_preload_logged = False
        for cookie in _cuda_dll_cookies:
            closer = getattr(cookie, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
        _cuda_dll_cookies.clear()
        _cuda_dll_dirs_registered.clear()


def ensure_nmt_model(
    *,
    log: Log = _log,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Path:
    """Download missing OPUS-MT CT2 files. Raises RuntimeError on failure."""
    global _download_error
    dest = nmt_model_dir()
    dest.mkdir(parents=True, exist_ok=True)
    if nmt_model_ready(dest):
        return dest
    if cancelled and cancelled():
        raise RuntimeError("Offline NMT download cancelled")
    with _download_lock:
        if nmt_model_ready(dest):
            return dest
        if _download_error:
            raise RuntimeError(_download_error)
        if cancelled and cancelled():
            raise RuntimeError("Offline NMT download cancelled")
        log(f"Downloading Offline NMT model (~320 MB) into {dest}")
        from core.polish.serve import download_file

        try:
            for name in _MODEL_FILES:
                if cancelled and cancelled():
                    raise RuntimeError("Offline NMT download cancelled")
                path = dest / name
                if path.is_file() and path.stat().st_size > 0:
                    continue
                url = f"{_HF_REPO}/{name}"
                log(f"  Fetching {name}…")
                download_file(url, path, log=log)
                if not path.is_file() or path.stat().st_size <= 0:
                    raise RuntimeError(f"Offline NMT download produced an empty {name}")
            if not nmt_model_ready(dest):
                raise RuntimeError("Offline NMT model is incomplete after download")
        except Exception as exc:
            if cancelled and cancelled():
                raise
            msg = str(exc)
            if "cancelled" in msg.lower():
                raise
            _download_error = msg
            raise
    log("Offline NMT model ready.")
    return dest


def nmt_cuda_install_instructions() -> str:
    """Exact Offline NMT GPU setup. CTranslate2 needs CUDA 12 libraries, not only a driver."""
    pip = " ".join(_CUDA_PIP_PACKAGES)
    py = sys.executable
    if platform.system() == "Darwin":
        return (
            "Offline NMT cannot use Apple GPUs (CTranslate2 has no Metal/CUDA on macOS). "
            "It runs on CPU. That is expected."
        )
    return "\n".join(
        [
            "Your NVIDIA GPU is fine. CTranslate2 still needs CUDA 12 libraries",
            "(especially cublas64_12.dll). The Game Ready driver is not enough.",
            "Do not install CUDA 13 for this — CTranslate2 looks for CUDA 12.",
            "cuDNN is not required for Offline NMT (opus-mt has no conv layers).",
            "",
            "Option A — recommended, same Python as HuaEPUB (~200–550 MB, one-time):",
            f"  {py} -m pip install {pip}",
            "then fully quit and reopen the app.",
            "",
            "Option B — NVIDIA CUDA Toolkit 12.x (runtime is enough):",
            "  Windows:  winget install Nvidia.CUDA --version 12.9",
            f"            or {_CUDA_TOOLKIT_ARCHIVE}",
            "            (Windows × x86_64 × 12.x exe; skip CUDA 13.x)",
            "  Linux:    install CUDA 12.x from NVIDIA or your distro, then reboot.",
            "then fully quit and reopen the app so Python can see the new DLLs.",
            "",
            "Ollama's CUDA 12 folder is also picked up automatically if Ollama is installed.",
        ]
    )


def _iter_nvidia_pip_lib_dirs() -> Iterable[Path]:
    roots: list[Path] = []
    try:
        import nvidia  # type: ignore

        roots.extend(Path(p) for p in getattr(nvidia, "__path__", []))
    except ImportError:
        pass
    for entry in sys.path:
        candidate = Path(entry) / "nvidia"
        if candidate.is_dir():
            roots.append(candidate)
    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        if resolved in seen or not root.is_dir():
            continue
        seen.add(resolved)
        for sub in ("cublas", "cuda_runtime", "cuda_nvrtc"):
            for name in ("bin", "lib"):
                path = root / sub / name
                if path.is_dir():
                    yield path
        for match in root.glob("*/bin"):
            if match.is_dir():
                yield match
        for match in root.glob("*/lib"):
            if match.is_dir():
                yield match


def _iter_toolkit_lib_dirs() -> Iterable[Path]:
    env_keys = (
        "CUDA_PATH",
        "CUDA_HOME",
        "CUDA_PATH_V12_9",
        "CUDA_PATH_V12_8",
        "CUDA_PATH_V12_6",
        "CUDA_PATH_V12_4",
    )
    seen: set[Path] = set()
    for key in env_keys:
        raw = os.environ.get(key)
        if not raw:
            continue
        base = Path(raw)
        for sub in ("bin", "lib", "lib64"):
            path = base / sub
            if path.is_dir() and path not in seen:
                seen.add(path)
                yield path
    if platform.system() == "Windows":
        toolkit = Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA")
        if toolkit.is_dir():
            for ver in sorted(toolkit.glob("v12*"), reverse=True):
                bin_dir = ver / "bin"
                if bin_dir.is_dir() and bin_dir not in seen:
                    seen.add(bin_dir)
                    yield bin_dir
        local = os.environ.get("LOCALAPPDATA")
        if local:
            ollama = Path(local) / "Programs" / "Ollama" / "lib" / "ollama" / "cuda_v12"
            if ollama.is_dir() and ollama not in seen:
                yield ollama
    else:
        for path in (Path("/usr/local/cuda/lib64"), Path("/usr/lib/x86_64-linux-gnu")):
            if path.is_dir() and path not in seen:
                yield path


def cuda_library_dirs() -> list[Path]:
    """Folders that may contain CUDA 12 cuBLAS (pip wheels, toolkit, Ollama)."""
    out: list[Path] = []
    seen: set[Path] = set()
    for path in (*_iter_nvidia_pip_lib_dirs(), *_iter_toolkit_lib_dirs()):
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(path)
    return out


def _dir_has_cublas(path: Path) -> bool:
    return any((path / name).is_file() for name in _CUBLAS_NAMES)


def cublas_present() -> bool:
    return any(_dir_has_cublas(path) for path in cuda_library_dirs())


def register_cuda_library_dirs() -> list[Path]:
    """Make CUDA 12 DLLs visible to CTranslate2 (Python 3.8+ ignores PATH on Windows)."""
    dirs = cuda_library_dirs()
    if platform.system() == "Windows":
        adder = getattr(os, "add_dll_directory", None)
        for path in dirs:
            key = str(path)
            if key in _cuda_dll_dirs_registered:
                continue
            if adder is not None and path.is_dir():
                try:
                    cookie = adder(key)
                    _cuda_dll_cookies.append(cookie)
                    _cuda_dll_dirs_registered.add(key)
                except OSError:
                    pass
        _preload_cuda_shared_libs(dirs)
    elif platform.system() == "Linux":
        extra = [str(p) for p in dirs if p.is_dir()]
        if extra:
            current = os.environ.get("LD_LIBRARY_PATH", "")
            os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(
                extra + ([current] if current else [])
            )
        _preload_cuda_shared_libs(dirs)
    return dirs


def _preload_cuda_shared_libs(dirs: list[Path]) -> None:
    """Load CUDA 12 libs by absolute path so CTranslate2 does not have to search PATH."""
    global _cuda_preload_logged
    try:
        import ctypes
    except ImportError:
        return
    # cudart first — cublas / cublasLt depend on it.
    names = (
        "cudart64_12.dll",
        "cublasLt64_12.dll",
        "cublas64_12.dll",
        "nvrtc64_120_0.dll",
        "nvrtc64_12.dll",
        "libcudart.so.12",
        "libcublasLt.so.12",
        "libcublas.so.12",
    )
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["winmode"] = 0
    loaded = []
    for name in names:
        for folder in dirs:
            lib = folder / name
            if not lib.is_file():
                continue
            try:
                ctypes.CDLL(str(lib), **kwargs)
                loaded.append(str(lib))
            except OSError:
                pass
            break
    if loaded and not _cuda_preload_logged:
        _cuda_preload_logged = True
        parent = Path(loaded[0]).parent
        print(f"  Offline NMT: using CUDA 12 libraries in {parent}")


def _running_under_pytest() -> bool:
    from core.utils import in_pytest

    return in_pytest()


def _can_pip_install_cuda() -> bool:
    if getattr(sys, "frozen", False):
        return False
    if _running_under_pytest():
        return False
    return True


def _pip_install_cuda_libs(log: Log) -> bool:
    import subprocess

    cmd = [sys.executable, "-m", "pip", "install", *_CUDA_PIP_PACKAGES]
    log(f"  Running: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"  CUDA library pip install failed to start ({exc}).")
        return False
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        log("  CUDA library pip install failed:")
        for line in tail:
            log(f"    {line}")
        return False
    return True


def prepare_cuda_runtime(log: Log = _log) -> bool:
    """
    Register CUDA 12 library dirs, and on a source install with a GPU but no
    cuBLAS, pip-install NVIDIA's CUDA 12 wheels. Returns True if cuBLAS is
    visible afterwards. Safe to call more than once.
    """
    global _cuda_prepared
    if _cuda_broken:
        return False
    if platform.system() == "Darwin":
        return False
    register_cuda_library_dirs()
    if cublas_present():
        _cuda_prepared = True
        return True
    if _cuda_prepared:
        return cublas_present()
    if not _can_pip_install_cuda():
        _cuda_prepared = True
        return False
    gpu = False
    try:
        import ctranslate2

        gpu = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        gpu = False
    if not gpu:
        _cuda_prepared = True
        return False
    log(
        "Offline NMT: NVIDIA GPU found, but CUDA 12 cuBLAS is not on the DLL path. "
        "Installing nvidia-cublas-cu12 and nvidia-cuda-runtime-cu12 "
        "(~200–550 MB, one-time) so the GPU can be used…"
    )
    ok = _pip_install_cuda_libs(log)
    register_cuda_library_dirs()
    _cuda_prepared = True
    if ok and cublas_present():
        log("  CUDA 12 libraries ready. Using the GPU.")
        return True
    if ok:
        log(
            "  pip finished, but cublas64_12.dll is still not visible. "
            "Fully quit and reopen the app, then try Offline NMT again."
        )
    return cublas_present()


def _looks_like_cuda_runtime_error(exc: BaseException) -> bool:
    """True when the GPU driver is present but CUDA libs (cuBLAS, etc.) are not."""
    msg = str(exc).lower()
    hints = ("cublas", "cudnn", "cudart", "cufft", "cusparse", "nvcuda", "nvrtc")
    if any(h in msg for h in hints):
        return True
    if "dll is not found" in msg or "cannot be loaded" in msg:
        return "cuda" in msg
    return False


def _mark_cuda_broken(reason: str) -> None:
    global _cuda_broken, _cuda_broken_logged
    _cuda_broken = True
    if _cuda_broken_logged:
        return
    _cuda_broken_logged = True
    print(
        f"  Offline NMT: GPU cannot run this model ({reason}). "
        "Using CPU instead (not Google)."
    )
    print(nmt_cuda_install_instructions())


def _device_candidates() -> list[tuple[str, str]]:
    """Prefer CUDA when the toolkit is actually usable; always include CPU."""
    out: list[tuple[str, str]] = []
    if not _cuda_broken:
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                prepare_cuda_runtime(_log)
                out.append(("cuda", "int8_float16"))
        except Exception:
            pass
    out.append(("cpu", "int8"))
    return out


def _make_translator(model_dir: Path, device: str, compute: str):
    import ctranslate2

    if device == "cuda":
        register_cuda_library_dirs()
    return ctranslate2.Translator(
        str(model_dir),
        device=device,
        compute_type=compute,
        inter_threads=2,
        intra_threads=0,
    )


def _sentencepiece():
    import sentencepiece as spm

    return spm


class CTranslate2Engine:
    """Lazy CTranslate2 + SentencePiece wrapper. Thread-safe after load."""

    def __init__(self, model_dir: Optional[Path] = None):
        self.model_dir = Path(model_dir) if model_dir else nmt_model_dir()
        self._translator = None
        self._src = None
        self._tgt = None
        self._device = "cpu"
        self._load_error: Optional[str] = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return nmt_runtime_available() and nmt_model_ready(self.model_dir)

    def _load_tokenizers(self) -> None:
        if self._src is not None and self._tgt is not None:
            return
        spm = _sentencepiece()
        self._src = spm.SentencePieceProcessor()
        self._src.load(str(self.model_dir / "source.spm"))
        tgt_path = self.model_dir / "target.spm"
        self._tgt = spm.SentencePieceProcessor()
        self._tgt.load(
            str(tgt_path if tgt_path.is_file() else self.model_dir / "source.spm")
        )

    def _warmup(self, translator) -> None:
        dummy = ["<unk>"]
        if self._src is not None:
            encoded = self._src.encode("。", out_type=str)
            if encoded:
                dummy = encoded
        translator.translate_batch([dummy], beam_size=1, max_batch_size=1)

    def _load(self) -> None:
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        if self._translator is not None:
            return
        if self._load_error:
            raise RuntimeError(self._load_error)
        if not nmt_runtime_available():
            self._load_error = (
                "Offline NMT needs: pip install -r requirements-nmt.txt"
            )
            raise RuntimeError(self._load_error)
        if not nmt_model_ready(self.model_dir):
            self._load_error = f"Offline NMT model missing under {self.model_dir}"
            raise RuntimeError(self._load_error)
        try:
            self._load_tokenizers()
        except Exception as exc:
            self._load_error = str(exc)
            raise RuntimeError(f"Offline NMT failed to load: {exc}") from exc

        last_exc: Optional[BaseException] = None
        for device, compute in _device_candidates():
            if device == "cuda" and _cuda_broken:
                continue
            try:
                translator = _make_translator(self.model_dir, device, compute)
                if device == "cuda":
                    self._warmup(translator)
                self._translator = translator
                self._device = device
                print(f"  Offline NMT: CTranslate2 opus-mt-zh-en on {device} ({compute})")
                return
            except Exception as exc:
                last_exc = exc
                self._translator = None
                if device == "cuda":
                    _mark_cuda_broken(str(exc))
                    continue
                break
        self._load_error = str(last_exc) if last_exc else "Offline NMT failed to load"
        raise RuntimeError(f"Offline NMT failed to load: {self._load_error}") from last_exc

    def translate(self, text: str) -> str:
        results = self.translate_batch([text])
        return results[0] if results else text

    def _run_batch(self, token_lists: list[list]):
        return self._translator.translate_batch(
            token_lists,
            beam_size=2,
            max_batch_size=32,
        )

    def translate_batch(self, texts: list[str]) -> list[str]:
        if not texts:
            return []
        with self._lock:
            self._load_locked()
            assert self._translator is not None and self._src is not None and self._tgt is not None
            out = list(texts)
            indexed: list[tuple[int, list]] = []
            for i, raw in enumerate(texts):
                piece = (raw or "").strip()
                if not piece:
                    continue
                indexed.append((i, self._src.encode(piece, out_type=str)))
            if not indexed:
                return out
            token_lists = [tokens for _, tokens in indexed]
            try:
                results = self._run_batch(token_lists)
            except Exception as exc:
                if self._device == "cuda":
                    _mark_cuda_broken(str(exc))
                    self._translator = None
                    self._load_error = None
                    self._load_locked()
                    results = self._run_batch(token_lists)
                else:
                    raise RuntimeError(f"Offline NMT batch failed: {exc}") from exc
            for (i, _), item in zip(indexed, results):
                if not item.hypotheses:
                    continue
                decoded = self._tgt.decode(item.hypotheses[0])
                if decoded.strip():
                    out[i] = decoded
            return out
