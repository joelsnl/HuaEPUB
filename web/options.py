# Author: joelsnl and Anthropic Claude
"""Download options for browser jobs, read from the shared settings.json.

The browser can change the choices below and the books folder. It cannot change
LibreTranslate or Ollama URLs. A headless server applies app updates itself.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from core.settings import save_settings

BACKENDS = [
    ("google", "Google (New)"),
    ("google_html", "Google (HTML)"),
    ("google_gtx", "Google (Old)"),
    ("microsoft", "Microsoft Edge"),
    ("libretranslate", "LibreTranslate"),
    ("ollama", "Ollama"),
    ("ctranslate2", "Offline NMT"),
]
GLOSSARIES = [
    ("auto", "Auto"),
    ("xianxia", "Cultivation pack"),
    ("user", "Names only"),
    ("off", "Off"),
]
_BACKEND_IDS = {b for b, _ in BACKENDS}
_GLOSSARY_IDS = {g for g, _ in GLOSSARIES}
MAX_WORKERS = 500


def nmt_ready() -> bool:
    try:
        from core.translation.nmt import nmt_model_ready, nmt_runtime_available

        return bool(nmt_runtime_available() and nmt_model_ready())
    except Exception:
        return False


def polish_ready() -> bool:
    try:
        from core.translation.qwen_glossary import polish_gguf_on_disk

        return bool(polish_gguf_on_disk())
    except Exception:
        return False


def backend_available(backend: str) -> bool:
    """Engines a browser may pick: anything that needs no install step now."""
    if backend == "ctranslate2":
        return nmt_ready()
    return backend in _BACKEND_IDS


def job_options(settings: Dict[str, Any]) -> Dict[str, Any]:
    """The same dict ``OptionsBar.snapshot()`` returns on the desktop.

    Polish runs only when its model is already on disk, and Offline NMT only
    when its model is, so a phone never starts a multi-GB download.
    """
    backend = str(settings.get("translation_backend") or "google")
    if not backend_available(backend):
        backend = "google"
    polish = bool(settings.get("ollama_polish", False)) and backend != "ollama" and polish_ready()
    try:
        workers = int(settings.get("workers", 200) or 200)
    except (TypeError, ValueError):
        workers = 200
    return {
        "translate": bool(settings.get("translate", True)),
        "clean": bool(settings.get("clean", True)),
        "use_cache": bool(settings.get("use_chapter_cache", True)),
        "clipboard": bool(settings.get("clipboard_watcher", False)),
        "workers": max(1, min(workers, MAX_WORKERS)),
        "backend": backend,
        "glossary": str(settings.get("translation_glossary") or "off"),
        "ollama_model": str(settings.get("ollama_model") or "qwen2.5:3b"),
        "ollama_url": str(settings.get("ollama_url") or "http://127.0.0.1:11434"),
        "ollama_polish": polish,
        "output_dir": str(settings.get("output_dir") or ""),
    }


def settings_payload(settings: Dict[str, Any]) -> Dict[str, Any]:
    opts = job_options(settings)
    nmt = nmt_ready()
    polish = polish_ready()
    backends: List[Dict[str, Any]] = []
    for value, label in BACKENDS:
        if value == "ctranslate2" and not nmt:
            continue
        backends.append({"value": value, "label": label})
    try:
        font_pt = int(settings.get("reader_font_pt") or 18)
    except (TypeError, ValueError):
        font_pt = 18
    return {
        "translate": opts["translate"],
        "clean": opts["clean"],
        "use_cache": opts["use_cache"],
        "workers": opts["workers"],
        "backend": opts["backend"],
        "glossary": opts["glossary"] if opts["glossary"] in _GLOSSARY_IDS else "auto",
        "polish": bool(settings.get("ollama_polish", False)) and polish,
        "polish_available": polish,
        "backends": backends,
        "glossaries": [{"value": v, "label": label} for v, label in GLOSSARIES],
        "reader_font_pt": max(12, min(36, font_pt)),
        "reader_theme": _choice(settings.get("reader_theme"), ("paper", "sepia", "night"), "paper"),
        "reader_mode": _choice(settings.get("reader_mode"), ("pages", "scroll"), "pages"),
        "reader_face": _choice(settings.get("reader_face"), ("serif", "sans"), "serif"),
        "reader_leading": _choice(settings.get("reader_leading"), ("tight", "normal", "loose"), "normal"),
        "reader_align": _choice(settings.get("reader_align"), ("justify", "left"), "justify"),
        "output_dir": opts["output_dir"],
        "max_workers": MAX_WORKERS,
    }


class SettingsError(ValueError):
    pass


def _choice(value, allowed, default: str) -> str:
    text = str(value or "")
    return text if text in allowed else default


def _books_folder(value) -> str:
    """An existing writable directory, or empty to use the default books folder."""
    if not isinstance(value, str):
        raise SettingsError("output_dir must be a path")
    text = value.strip()
    if not text:
        return ""
    path = Path(text)
    if not path.is_absolute():
        raise SettingsError("The books folder must be a full path")
    if not path.is_dir():
        raise SettingsError("That books folder does not exist")
    if not os.access(path, os.W_OK):
        raise SettingsError("That books folder is not writable")
    return str(path)


def apply_settings(settings: Dict[str, Any], changes: Dict[str, Any]) -> None:
    """Validate and write the browser-editable keys. Unknown keys are refused."""
    allowed = {"translate", "clean", "use_cache", "workers", "backend", "glossary", "polish",
               "reader_font_pt", "reader_theme", "reader_mode", "reader_face",
               "reader_leading", "reader_align", "output_dir"}
    unknown = set(changes) - allowed
    if unknown:
        raise SettingsError(f"Not editable from the browser: {', '.join(sorted(unknown))}")
    updates: Dict[str, Any] = {}
    for key, setting in (("translate", "translate"), ("clean", "clean"),
                         ("use_cache", "use_chapter_cache")):
        if key in changes:
            if not isinstance(changes[key], bool):
                raise SettingsError(f"{key} must be true or false")
            updates[setting] = changes[key]
    if "workers" in changes:
        value = changes["workers"]
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_WORKERS:
            raise SettingsError(f"workers must be 1–{MAX_WORKERS}")
        updates["workers"] = value
    if "backend" in changes:
        value = changes["backend"]
        if value not in _BACKEND_IDS or not backend_available(value):
            raise SettingsError("That translator is not available on this PC")
        updates["translation_backend"] = value
    if "glossary" in changes:
        if changes["glossary"] not in _GLOSSARY_IDS:
            raise SettingsError("Unknown glossary mode")
        updates["translation_glossary"] = changes["glossary"]
    if "polish" in changes:
        if not isinstance(changes["polish"], bool):
            raise SettingsError("polish must be true or false")
        if changes["polish"] and not polish_ready():
            raise SettingsError("Install Polish below first")
        updates["ollama_polish"] = changes["polish"]
    if "reader_font_pt" in changes:
        value = changes["reader_font_pt"]
        if not isinstance(value, int) or isinstance(value, bool) or not 12 <= value <= 36:
            raise SettingsError("reader_font_pt must be 12–36")
        updates["reader_font_pt"] = value
    for key, allowed_values in (
        ("reader_theme", ("paper", "sepia", "night")),
        ("reader_mode", ("pages", "scroll")),
        ("reader_face", ("serif", "sans")),
        ("reader_leading", ("tight", "normal", "loose")),
        ("reader_align", ("justify", "left")),
    ):
        if key in changes:
            if changes[key] not in allowed_values:
                raise SettingsError(f"{key} is not a reader choice")
            updates[key] = changes[key]
    if "output_dir" in changes:
        updates["output_dir"] = _books_folder(changes["output_dir"])
    if not updates:
        return
    settings.update(updates)
    save_settings(settings)
