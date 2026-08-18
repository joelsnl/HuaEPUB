# Author: joelsnl
"""KEEP/REPLACE polish cache under ~/.huaepub/polish."""

from __future__ import annotations

from pathlib import Path


def env_value(*names: str, default: str = "") -> str:
    import os

    for name in names:
        raw = os.environ.get(name, "").strip()
        if raw:
            return raw
    return default


def cache_dir() -> Path:
    env = env_value("HUAEPUB_POLISH_CACHE")
    if env:
        path = Path(env)
    else:
        from core.settings import get_data_dir

        path = get_data_dir() / "polish"
    path.mkdir(parents=True, exist_ok=True)
    return path


def package_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"
