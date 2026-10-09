# Author: joelsnl and Anthropic Claude
"""How much room is left on the disks and in the RAM this server writes with.

The books folder and the app data folder (cache, library, settings) are usually on one disk,
but a custom books folder can sit on another. The disk number shown is the *tightest* disk,
since either one filling up stops downloads. RAM is MemAvailable (Linux) or available
physical memory, which is what a translation job can actually use. Only byte counts leave
the server, never a path.
"""

from __future__ import annotations

import os
import shutil
import sys
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


# A 512 MB board can never clear the disk's 500 MB floor, so RAM has its own lines.
RAM_LOW_BYTES, RAM_LOW_FRACTION = 128 * MIB, 0.15
RAM_CRITICAL_BYTES, RAM_CRITICAL_FRACTION = 64 * MIB, 0.08


def ram_level(free: int, total: int) -> str:
    if total <= 0:
        return "ok"
    if free < RAM_CRITICAL_BYTES or free / total < RAM_CRITICAL_FRACTION:
        return "critical"
    if free < RAM_LOW_BYTES or free / total < RAM_LOW_FRACTION:
        return "low"
    return "ok"


def _parse_meminfo(text: str) -> Tuple[int, int, int, int]:
    """(total, available, swap total, swap used) from /proc/meminfo text. Values are kB there."""
    info: Dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            info[key] = int(parts[0]) * 1024
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", swap_total)
    return total, avail, swap_total, max(0, swap_total - swap_free)


def _memory_windows() -> Tuple[int, int, int, int]:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(stat)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return 0, 0, 0, 0
    # ponytail: Windows page file mixes RAM and swap, so swap stays 0 here.
    return int(stat.ullTotalPhys), int(stat.ullAvailPhys), 0, 0


def _memory_sysctl() -> Tuple[int, int, int, int]:
    """macOS has no /proc/meminfo. Available is free + inactive + speculative pages."""
    import subprocess

    def sh(*args: str) -> str:
        return subprocess.check_output(args, text=True, timeout=2)

    try:
        total = int(sh("sysctl", "-n", "hw.memsize").strip())
        page = int(sh("sysctl", "-n", "hw.pagesize").strip() or "4096")
        pages: Dict[str, int] = {}
        for line in sh("vm_stat").splitlines():
            key, _, rest = line.partition(":")
            num = rest.strip().strip(".").replace(".", "")
            if num.isdigit():
                pages[key.strip()] = int(num)
        avail = page * sum(
            pages.get(name, 0) for name in ("Pages free", "Pages inactive", "Pages speculative")
        )
        return total, min(avail, total), 0, 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0, 0, 0, 0


def _memory() -> Tuple[int, int, int, int]:
    if sys.platform == "win32":
        return _memory_windows()
    try:
        return _parse_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8"))
    except OSError:
        return _memory_sysctl()


def memory_payload() -> Optional[Dict[str, object]]:
    """Available RAM, plus swap in use. None when the OS won't say."""
    try:
        total, free, swap_total, swap_used = _memory()
    except OSError:
        return None
    if total <= 0:
        return None
    free = max(0, min(int(free), int(total)))
    return {
        "free": free,
        "total": int(total),
        "used": int(total) - free,
        "level": ram_level(free, int(total)),
        "swap_used": max(0, int(swap_used)),
        "swap_total": max(0, int(swap_total)),
    }


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
