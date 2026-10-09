# Author: joelsnl and Anthropic Claude
"""How much room is left on the disks this server writes to.

The books folder and the app data folder (cache, library, settings) are usually on one disk,
but a custom books folder can sit on another. The number shown is the *tightest* disk, since
either one filling up stops downloads. Only free/total bytes leave the server, never a path.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

GIB = 1024 ** 3
MIB = 1024 ** 2
LOW_BYTES, LOW_FRACTION = 2 * GIB, 0.10
CRITICAL_BYTES, CRITICAL_FRACTION = 500 * MIB, 0.03


def _existing(path: Path) -> Optional[Path]:
    """The path itself, or its nearest parent that exists (a books folder not made yet)."""
    for candidate in (path, *path.parents):
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def _device(path: Path) -> int:
    return os.stat(path).st_dev


def _usage(path: Path) -> Tuple[int, int]:
    """(total, free) bytes available to this user."""
    usage = shutil.disk_usage(path)
    return int(usage.total), int(usage.free)


def level(free: int, total: int) -> str:
    if total <= 0:
        return "ok"
    if free < CRITICAL_BYTES or free / total < CRITICAL_FRACTION:
        return "critical"
    if free < LOW_BYTES or free / total < LOW_FRACTION:
        return "low"
    return "ok"


def storage_payload(places: Iterable[Tuple[str, Path]]) -> Optional[Dict[str, object]]:
    """Free space on the tightest disk among ``places`` (label, path), or None if none can be read."""
    groups: Dict[int, Dict[str, object]] = {}
    for label, raw in places:
        path = _existing(Path(raw))
        if path is None:
            continue
        try:
            dev = _device(path)
            total, free = _usage(path)
        except OSError:
            continue
        group = groups.setdefault(dev, {"total": total, "free": free, "labels": []})
        labels: List[str] = group["labels"]  # type: ignore[assignment]
        if label not in labels:
            labels.append(label)
    if not groups:
        return None
    tight = min(groups.values(), key=lambda g: g["free"])  # type: ignore[arg-type,return-value]
    free, total = int(tight["free"]), int(tight["total"])  # type: ignore[call-overload]
    return {
        "free": free,
        "total": total,
        "used": max(0, total - free),
        "level": level(free, total),
        "where": " and ".join(tight["labels"]),  # type: ignore[arg-type]
    }
