# Author: joelsnl and Anthropic Claude
"""
Shared security helpers: safe URL fetches, zip extraction, and secret file perms.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import stat
import zipfile
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse

# Hosts / nets that must never be fetched as novel content, covers, or LT backends
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
}


class UnsafeURLError(Exception):
    """Raised when a URL targets a disallowed scheme or private/internal host."""


def write_secret_file(path: Path, data: str) -> None:
    """
    Write sensitive data with owner-only permissions when the OS supports it.
    On Windows, chmod is limited; still set what we can after write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create/truncate with restrictive mode where supported
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    mode = 0o600
    fd = os.open(str(path), flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            fd = -1  # fdopen owns it
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except Exception:
                pass
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (ip.version == 4 and ip in ipaddress.ip_network("169.254.0.0/16"))
        or (ip.version == 6 and (
            ip in ipaddress.ip_network("fc00::/7")
            or ip in ipaddress.ip_network("fe80::/10")
            or ip in ipaddress.ip_network("::/128")
        ))
    )


def _hostname_resolves_to_blocked(hostname: str) -> Tuple[bool, str]:
    host = (hostname or "").strip(".").lower()
    if not host:
        return True, "empty host"
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        return True, f"blocked hostname ({host})"

    # Literal IP in the URL
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(ip):
            return True, f"blocked address ({ip})"
        return False, ""
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return True, f"DNS lookup failed ({e})"

    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            return True, f"resolves to blocked address ({ip})"
    return False, ""


def validate_fetch_url(
    url: str,
    *,
    allow_http: bool = True,
    resolve_dns: bool = True,
) -> None:
    """
    Raise UnsafeURLError if url is not safe to fetch from this app.
    Blocks non-http(s), credentials-in-URL, and private/loopback hosts.
    """
    raw = (url or "").strip()
    if not raw:
        raise UnsafeURLError("Empty URL")

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme == "https":
        pass
    elif scheme == "http":
        if not allow_http:
            raise UnsafeURLError("Only https URLs are allowed here")
    else:
        raise UnsafeURLError(f"Disallowed URL scheme: {scheme or '(none)'}")

    if parsed.username or parsed.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")

    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL missing host")

    if not resolve_dns:
        # Still block obvious literals / localhost names
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
            if _is_blocked_ip(ip):
                raise UnsafeURLError(f"Blocked address: {ip}")
        except ValueError:
            h = host.lower().rstrip(".")
            if h in _BLOCKED_HOSTNAMES or h.endswith(".localhost"):
                raise UnsafeURLError(f"Blocked hostname: {h}")
        return

    blocked, reason = _hostname_resolves_to_blocked(host)
    if blocked:
        raise UnsafeURLError(f"Blocked URL host: {reason}")


def is_fetch_url_safe(url: str, **kwargs) -> bool:
    try:
        validate_fetch_url(url, **kwargs)
        return True
    except UnsafeURLError:
        return False


def validate_libretranslate_url(url: str, *, resolve_dns: bool = True) -> str:
    """Normalize and validate a LibreTranslate base URL; return stripped form."""
    raw = (url or "").strip().rstrip("/")
    if not raw:
        raise UnsafeURLError("LibreTranslate URL is empty")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    rebuilt = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.path and parsed.path != "/":
        rebuilt += parsed.path.rstrip("/")
    validate_fetch_url(rebuilt, allow_http=True, resolve_dns=resolve_dns)
    return rebuilt.rstrip("/")


def safe_extract_zip(
    zip_path: Path,
    dest_dir: Path,
    *,
    allowed_names: Optional[Iterable[str]] = None,
) -> List[Path]:
    """
    Extract a zip without Zip Slip. Optionally restrict to a set of basenames
    (matched against the final path name, case-sensitive on the archive entry
    basename). Returns list of written file paths.
    """
    dest_dir = Path(dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    allowed = set(allowed_names) if allowed_names is not None else None
    written: List[Path] = []

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            # Reject absolute / drive / unc / parent traversal
            if name.startswith("/") or name.startswith("../") or "/../" in f"/{name}/":
                raise ValueError(f"Unsafe zip entry path: {info.filename!r}")
            if ":" in name.split("/")[0]:  # Windows drive in entry
                raise ValueError(f"Unsafe zip entry path: {info.filename!r}")

            # Normalize and ensure stays under dest
            target = (dest_dir / name).resolve()
            try:
                target.relative_to(dest_dir)
            except ValueError as e:
                raise ValueError(f"Zip entry escapes destination: {info.filename!r}") from e

            if allowed is not None and target.name not in allowed:
                # Allow nested folders in source archives; only filter when
                # extracting a known platform asset with a single exe.
                # For allowed_names mode, skip unrelated files instead of failing
                # so checksummed zips can contain README etc.
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as out:
                # Bound individual file size (512 MB) against zip bombs of one huge file
                max_bytes = 512 * 1024 * 1024
                copied = 0
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > max_bytes:
                        raise ValueError(f"Zip entry too large: {info.filename!r}")
                    out.write(chunk)
            written.append(target)

    return written


def write_update_helper_config(path: Path, *, new_exe: Path, old_exe: Path, pid: int) -> None:
    """Sidecar JSON for the update helper — avoids shell-interpolating paths."""
    payload = {
        "new_exe": str(Path(new_exe)),
        "old_exe": str(Path(old_exe)),
        "pid": int(pid),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
