# Author: joelsnl and Anthropic Claude
"""Atomic sibling-tmp + replace writes for JSON/text user-data files."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Union

PathLike = Union[str, Path]


def atomic_write_text(
    path: PathLike,
    payload: str,
    *,
    fsync: bool = False,
    encoding: str = "utf-8",
    tmp_suffix: str = ".json.tmp",
) -> None:
    """Write text via a sibling tmp file, then replace. Raises on I/O errors."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(tmp_suffix)
    with open(tmp, "w", encoding=encoding) as handle:
        handle.write(payload)
        handle.flush()
        if fsync:
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
    tmp.replace(dest)
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass


def atomic_write_json(
    path: PathLike,
    obj: Any,
    *,
    fsync: bool = False,
    indent: int = 2,
    ensure_ascii: bool = False,
    tmp_suffix: str = ".json.tmp",
) -> None:
    """Serialize obj as JSON, then atomic_write_text."""
    payload = json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii)
    atomic_write_text(
        path, payload, fsync=fsync, tmp_suffix=tmp_suffix
    )
