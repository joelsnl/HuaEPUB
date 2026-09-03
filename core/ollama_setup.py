# Author: joelsnl and Anthropic Claude
"""
Ollama install/probe/pull helpers and GPU detection.

Kept out of translator.py so Google/LibreTranslate retry+cache stays focused.
Callers can keep importing these names from core.translator.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from core.parser import CHROME_UA

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:3b"
USER_AGENT = CHROME_UA

_gpu_lock = threading.Lock()
_gpu_cached: Optional[bool] = None
_gpu_logged = False


def _nvidia_smi_cmd() -> Optional[str]:
    from core.polish.hardware import nvidia_smi_executable
    return nvidia_smi_executable()


def ollama_gpu_available() -> bool:
    """
    True if a local GPU Ollama can use is present (NVIDIA, ROCm, or Apple Metal).
    Cached. HUAEPUB_OLLAMA_GPU=0|1 overrides.
    """
    global _gpu_cached
    env = (os.environ.get("HUAEPUB_OLLAMA_GPU") or "").strip().lower()
    if env in ("0", "false", "cpu", "no"):
        return False
    if env in ("1", "true", "gpu", "yes"):
        return True
    with _gpu_lock:
        if _gpu_cached is not None:
            return _gpu_cached
        _gpu_cached = _detect_ollama_gpu()
        return _gpu_cached


def _detect_ollama_gpu() -> bool:
    smi = _nvidia_smi_cmd()
    if smi:
        try:
            kwargs: Dict[str, Any] = {
                "capture_output": True,
                "timeout": 2,
                "text": True,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run([smi, "-L"], **kwargs)
            out = (result.stdout or "") + (result.stderr or "")
            if result.returncode == 0 and "GPU" in out.upper():
                return True
        except Exception:
            pass
    if shutil.which("rocm-smi"):
        try:
            result = subprocess.run(
                ["rocm-smi"], capture_output=True, timeout=2, text=True,
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass
    if sys.platform == "darwin":
        return True
    return False


def ollama_infer_options() -> Dict[str, Any]:
    """num_gpu / num_thread: all layers on GPU when present, else CPU."""
    global _gpu_logged
    threads = max(2, min(16, int(os.cpu_count() or 4)))
    if ollama_gpu_available():
        opts: Dict[str, Any] = {"num_gpu": 99, "num_thread": min(8, threads)}
        device = "GPU"
    else:
        opts = {"num_gpu": 0, "num_thread": threads}
        device = "CPU"
    if not _gpu_logged:
        _gpu_logged = True
        print(f"Ollama inference: {device}")
    return opts


def probe_ollama(
    ollama_url: str = DEFAULT_OLLAMA_URL,
    timeout: float = 1.5,
) -> Optional[List[str]]:
    """
    Models already pulled. None if Ollama is not reachable; [] if it is
    running but has no models. Never pulls.
    """
    from core.security import UnsafeURLError, safe_http_request, validate_ollama_url
    try:
        base = validate_ollama_url(ollama_url or DEFAULT_OLLAMA_URL)
    except UnsafeURLError:
        return None
    session = requests.Session()
    try:
        response = safe_http_request(
            session,
            "GET",
            f"{base}/api/tags",
            allow_http=True,
            allow_loopback=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        data = response.json() if hasattr(response, "json") else {}
    except Exception:
        return None
    names: List[str] = []
    for item in (data.get("models") or []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or item.get("model") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _is_windows() -> bool:
    """Isolated so tests can fake Windows without patching os.name (Path breaks)."""
    return os.name == "nt"


def ollama_is_installed() -> bool:
    """True if the Ollama app/CLI looks present (not whether it is running)."""
    if shutil.which("ollama"):
        return True
    candidates: List[Path] = []
    if _is_windows():
        local = os.environ.get("LOCALAPPDATA") or ""
        pf = os.environ.get("PROGRAMFILES") or r"C:\Program Files"
        pf86 = os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)"
        if local:
            candidates.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
        candidates.append(Path(pf) / "Ollama" / "ollama.exe")
        candidates.append(Path(pf86) / "Ollama" / "ollama.exe")
    else:
        home = Path.home()
        candidates.extend([
            Path("/usr/local/bin/ollama"),
            Path("/usr/bin/ollama"),
            Path("/opt/homebrew/bin/ollama"),
            home / ".local" / "bin" / "ollama",
            Path("/Applications/Ollama.app"),
            home / "Applications" / "Ollama.app",
        ])
    return any(p.exists() for p in candidates)


def list_ollama_models(
    ollama_url: str = DEFAULT_OLLAMA_URL,
    timeout: float = 1.5,
) -> List[str]:
    """Like probe_ollama, but [] when Ollama is down (never None)."""
    found = probe_ollama(ollama_url, timeout=timeout)
    return [] if found is None else found


def ollama_model_installed(name: str, installed: List[str]) -> bool:
    """True if name is pulled. qwen2.5:3b does not match qwen2.5:7b."""
    want = (name or "").strip()
    if not want or not installed:
        return False
    for have in installed:
        if have == want or have.startswith(want + ":"):
            return True
    return False


def pull_ollama_model(
    model: str,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> None:
    """
    Stream-pull a model into local Ollama. Loopback only. Raises on failure
    or cancel. progress_callback(percent_or_-1, status). Uses safe_http_request
    so redirect hops are re-validated (still loopback-only).
    """
    from core.security import UnsafeURLError, safe_http_request, validate_ollama_url

    name = (model or "").strip()
    if not name:
        raise ValueError("No Ollama model name to download")
    try:
        base = validate_ollama_url(ollama_url or DEFAULT_OLLAMA_URL)
    except UnsafeURLError as e:
        raise ValueError(f"Invalid Ollama URL: {e}") from e

    session = requests.Session()
    try:
        response = safe_http_request(
            session,
            "POST",
            f"{base}/api/pull",
            allow_http=True,
            allow_loopback=True,
            timeout=(10, 3600),
            json={"model": name, "name": name, "stream": True},
            stream=True,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
            },
        )
    except UnsafeURLError as e:
        raise ValueError(f"Blocked Ollama URL: {e}") from e
    except Exception as e:
        err = str(e).lower()
        if any(s in err for s in ("connection", "refused", "10061")):
            raise ValueError(
                "Ollama is not running. Start Ollama, then try again."
            ) from e
        raise
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code == 404:
        raise ValueError(f"Ollama does not know how to pull '{name}'")
    response.raise_for_status()

    for raw in response.iter_lines():
        if cancel_check and cancel_check():
            try:
                response.close()
            except Exception:
                pass
            raise ValueError("Download cancelled")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("error"):
            raise ValueError(str(data["error"]))
        status = str(data.get("status") or "")
        total = int(data.get("total") or 0)
        completed = int(data.get("completed") or 0)
        if progress_callback:
            if total > 0:
                pct = min(100, int(completed * 100 / total))
            elif status == "success":
                pct = 100
            else:
                pct = -1
            progress_callback(pct, status)
        if status == "success":
            return
    if progress_callback:
        progress_callback(100, "success")


def resolve_ollama_model(preferred: str, installed: List[str]) -> str:
    """
    Pick a model that is actually installed.
    Exact match, then same family (qwen2.5:3b → qwen2.5:7b), else first installed.
    If nothing is installed, keep the preferred name so the user can still type it.
    """
    pref = (preferred or "").strip()
    if not installed:
        return pref
    if pref in installed:
        return pref
    pref_base = pref.split(":")[0] if pref else ""
    if pref_base:
        for name in installed:
            if name.split(":")[0] == pref_base:
                return name
    return installed[0]
