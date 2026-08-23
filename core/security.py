# Author: joelsnl and Anthropic Claude
"""
Shared security helpers: safe URL fetches, zip extraction, and secret file perms.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import stat
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

# Hosts / nets that must never be fetched as novel content, covers, or LT backends
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
}

_LOOPBACK_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "127.0.0.1",
    "::1",
    "0:0:0:0:0:0:0:1",
}

_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})

# Zip bomb guards
_MAX_ZIP_MEMBER_BYTES = 512 * 1024 * 1024  # 512 MiB per entry
_MAX_ZIP_TOTAL_BYTES = 1024 * 1024 * 1024  # 1 GiB uncompressed total
_MAX_ZIP_ENTRIES = 10_000

# Cover preview / EPUB image cap (hostile pages can serve gigabyte "covers")
MAX_COVER_BYTES = 8 * 1024 * 1024

# GitHub release asset hosts (browser_download_url + CDN redirects)
_GITHUB_ASSET_HOSTS = {
    "github.com",
    "www.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
}

_HF_ASSET_HOSTS = {
    "huggingface.co",
    "www.huggingface.co",
    "cdn-lfs.huggingface.co",
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


def tighten_file_permissions(path: Path) -> None:
    """Best-effort owner-only perms on an existing file."""
    try:
        os.chmod(Path(path), stat.S_IRUSR | stat.S_IWUSR)
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


def _is_loopback_host(host: str) -> bool:
    """True for localhost names and loopback IP literals (no DNS)."""
    h = (host or "").strip("[]").lower().rstrip(".")
    if not h:
        return False
    if h in _LOOPBACK_HOSTS or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


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
    allow_loopback: bool = False,
) -> None:
    """
    Raise UnsafeURLError if url is not safe to fetch from this app.
    Blocks non-http(s), credentials-in-URL, and private/loopback hosts.
    Set allow_loopback=True only for user-configured local services (Ollama).
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

    if allow_loopback and _is_loopback_host(host):
        return

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


def github_asset_host_ok(host: str) -> bool:
    """True for github.com and GitHub release CDN hostnames."""
    h = (host or "").strip("[]").lower().rstrip(".")
    if not h:
        return False
    if h in _GITHUB_ASSET_HOSTS:
        return True
    if h.endswith(".githubusercontent.com"):
        return True
    if h.endswith(".github.com"):
        return True
    return False


def validate_github_asset_host(url: str) -> None:
    """Raise UnsafeURLError if url is not a GitHub release/CDN https host."""
    host = urlparse(url or "").hostname
    if not github_asset_host_ok(host or ""):
        raise UnsafeURLError(f"Update download host not allowed: {host or '(none)'}")


def huggingface_host_ok(host: str) -> bool:
    """True for huggingface.co and its LFS / Xet CDN hostnames."""
    h = (host or "").strip("[]").lower().rstrip(".")
    if not h:
        return False
    if h in _HF_ASSET_HOSTS:
        return True
    if h.endswith(".huggingface.co"):
        return True
    if h.endswith(".xethub.hf.co"):
        return True
    if h.endswith(".hf.co") and "cas-bridge" in h:
        return True
    return False


def polish_download_host_ok(host: str) -> bool:
    """llama.cpp GitHub builds and official Hugging Face GGUFs only."""
    return github_asset_host_ok(host) or huggingface_host_ok(host)


def validate_polish_download_url(url: str) -> None:
    parsed = urlparse(url or "")
    if (parsed.scheme or "").lower() != "https":
        raise UnsafeURLError("Polish downloads must use https")
    host = parsed.hostname or ""
    if not polish_download_host_ok(host):
        raise UnsafeURLError(f"Polish download host not allowed: {host or '(none)'}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> None:
    want = (expected or "").strip().lower()
    if len(want) != 64 or any(c not in "0123456789abcdef" for c in want):
        raise ValueError("sha256 must be a 64-char hex digest")
    got = sha256_file(path)
    if got != want:
        raise ValueError(f"SHA256 mismatch for {Path(path).name}: expected {want}, got {got}")


def github_asset_digest(asset: dict) -> str:
    """
    GitHub release assets may publish `digest` as `sha256:<hex>`.
    Empty string if upstream did not publish one.
    """
    raw = str((asset or {}).get("digest") or "").strip().lower()
    if raw.startswith("sha256:"):
        raw = raw.split(":", 1)[1]
    if len(raw) == 64 and all(c in "0123456789abcdef" for c in raw):
        return raw
    return ""


def safe_epub_basename(value: str, *, fallback_title: str = "") -> str:
    """
    Single-component *.epub name. Strips directories so library.json cannot
    walk outside the books folder.
    """
    raw = (value or "").strip().replace("\\", "/")
    base = Path(raw).name
    if base in ("", ".", ".."):
        base = ""
    if base.lower().endswith(".epub") and len(base) > 5:
        stem = Path(base).stem.strip()
        if stem and stem not in (".", "..") and "/" not in stem and "\\" not in stem:
            return f"{stem}.epub"
    if fallback_title:
        from core.utils import safe_filename
        return f"{safe_filename(fallback_title)}.epub"
    return ""


def path_is_under(path: Path, root: Path) -> bool:
    """True if path.resolve() is root or a descendant of root.resolve()."""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def is_allowed_epub_path(path: Path, roots: Iterable[Path]) -> bool:
    """True if path is a .epub file under one of roots (after resolve)."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    if resolved.suffix.lower() != ".epub":
        return False
    if resolved.name != safe_epub_basename(resolved.name):
        return False
    return any(path_is_under(resolved, root) for root in roots if root)


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


def validate_ollama_url(url: str) -> str:
    """
    Normalize an Ollama base URL. Must be loopback (127.0.0.1 / localhost)
    so a translator setting cannot be turned into an SSRF trampoline.
    """
    raw = (url or "").strip().rstrip("/")
    if not raw:
        raise UnsafeURLError("Ollama URL is empty")
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise UnsafeURLError(f"Disallowed URL scheme: {scheme or '(none)'}")
    if parsed.username or parsed.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")
    host = parsed.hostname
    if not host or not _is_loopback_host(host):
        raise UnsafeURLError(
            "Ollama URL must be localhost (e.g. http://127.0.0.1:11434)"
        )
    rebuilt = f"{scheme}://{parsed.netloc}"
    return rebuilt.rstrip("/")


def safe_http_request(
    session: Any,
    method: str,
    url: str,
    *,
    allow_http: bool = True,
    allow_loopback: bool = False,
    resolve_dns: bool = True,
    max_redirects: int = 5,
    timeout: float = 30,
    extra_check: Optional[Any] = None,
    **kwargs: Any,
) -> Any:
    """
    HTTP request that re-validates every redirect hop against the SSRF blocklist.

    Does not follow redirects automatically — each Location is checked with
    validate_fetch_url (including DNS) before the next request.

    resolve_dns=False skips getaddrinfo on the *first* hop only (hardcoded
    public endpoints such as Google Translate). Redirect targets are still
    DNS-checked. Literal loopback/private IPs stay blocked either way.

    extra_check(url), if set, runs on every hop after validate_fetch_url
    (used to pin update downloads to GitHub CDN hosts).
    """
    current = (url or "").strip()
    method = (method or "GET").upper()
    # Callers must not bypass redirect checks
    kwargs.pop("allow_redirects", None)
    first_hop = True

    for _ in range(max_redirects + 1):
        validate_fetch_url(
            current,
            allow_http=allow_http,
            resolve_dns=resolve_dns if first_hop else True,
            allow_loopback=allow_loopback,
        )
        if extra_check:
            extra_check(current)
        first_hop = False

        def _call(fn, *args, **kw):
            try:
                return fn(*args, allow_redirects=False, **kw)
            except TypeError:
                # Test doubles / odd clients may not accept allow_redirects
                return fn(*args, **kw)

        if hasattr(session, "request"):
            resp = _call(
                session.request, method, current, timeout=timeout, **kwargs
            )
        elif method == "GET" and hasattr(session, "get"):
            resp = _call(session.get, current, timeout=timeout, **kwargs)
        elif method == "POST" and hasattr(session, "post"):
            resp = _call(session.post, current, timeout=timeout, **kwargs)
        else:
            raise UnsafeURLError("HTTP session does not support request/get/post")

        status = int(getattr(resp, "status_code", 0) or 0)
        if status in _REDIRECT_STATUS:
            headers = getattr(resp, "headers", {}) or {}
            loc = headers.get("Location") or headers.get("location")
            if not loc:
                raise UnsafeURLError(f"Redirect {status} without Location header")
            current = urljoin(current, loc)
            # Match common client behavior: 301/302/303 turn POST into GET
            if status in (301, 302, 303) and method == "POST":
                method = "GET"
                kwargs.pop("json", None)
                kwargs.pop("data", None)
                kwargs.pop("files", None)
            continue
        return resp

    raise UnsafeURLError(f"Too many redirects (>{max_redirects})")


def fetch_cover_bytes(session: Any, url: str, *, timeout: float = 20) -> bytes:
    """
    Download a novel cover through the SSRF guard. Raises UnsafeURLError
    (or the session's HTTP error) on failure. Caps body size.
    """
    resp = safe_http_request(
        session, "GET", url, allow_http=True, timeout=timeout
    )
    try:
        resp.raise_for_status()
    except Exception as e:
        raise UnsafeURLError(f"Cover fetch failed: {e}") from e
    data = getattr(resp, "content", None) or b""
    if not data:
        raise UnsafeURLError("Cover fetch returned no data")
    if len(data) > MAX_COVER_BYTES:
        raise UnsafeURLError(
            f"Cover image exceeds size limit ({MAX_COVER_BYTES} bytes)"
        )
    return data


def safe_extract_zip(
    zip_path: Path,
    dest_dir: Path,
    *,
    allowed_names: Optional[Iterable[str]] = None,
    max_member_bytes: int = _MAX_ZIP_MEMBER_BYTES,
    max_total_bytes: int = _MAX_ZIP_TOTAL_BYTES,
    max_entries: int = _MAX_ZIP_ENTRIES,
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
    total_bytes = 0
    entry_count = 0

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

            entry_count += 1
            if entry_count > max_entries:
                raise ValueError(
                    f"Zip has too many extractable entries (>{max_entries})"
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as out:
                copied = 0
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    total_bytes += len(chunk)
                    if copied > max_member_bytes:
                        raise ValueError(f"Zip entry too large: {info.filename!r}")
                    if total_bytes > max_total_bytes:
                        raise ValueError(
                            f"Zip uncompressed size exceeds limit "
                            f"({max_total_bytes} bytes)"
                        )
                    out.write(chunk)
            written.append(target)

    return written


def safe_extract_tar(
    tar_path: Path,
    dest_dir: Path,
    *,
    max_member_bytes: int = _MAX_ZIP_MEMBER_BYTES,
    max_total_bytes: int = _MAX_ZIP_TOTAL_BYTES,
    max_entries: int = _MAX_ZIP_ENTRIES,
) -> List[Path]:
    """Extract a tar/tar.gz without path traversal. Same size caps as zip."""
    dest_dir = Path(dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    total_bytes = 0
    entry_count = 0

    with tarfile.open(tar_path, "r:*") as tf:
        for info in tf.getmembers():
            if not info.isfile():
                continue
            name = (info.name or "").replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            if name.startswith("/") or name.startswith("../") or "/../" in f"/{name}/":
                raise ValueError(f"Unsafe tar entry path: {info.name!r}")
            if ":" in name.split("/")[0]:
                raise ValueError(f"Unsafe tar entry path: {info.name!r}")
            target = (dest_dir / name).resolve()
            try:
                target.relative_to(dest_dir)
            except ValueError as e:
                raise ValueError(f"Tar entry escapes destination: {info.name!r}") from e

            entry_count += 1
            if entry_count > max_entries:
                raise ValueError(f"Tar has too many extractable entries (>{max_entries})")
            if info.size > max_member_bytes:
                raise ValueError(f"Tar entry too large: {info.name!r}")
            total_bytes += int(info.size or 0)
            if total_bytes > max_total_bytes:
                raise ValueError(
                    f"Tar uncompressed size exceeds limit ({max_total_bytes} bytes)"
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(info)
            if src is None:
                continue
            with src, open(target, "wb") as out:
                copied = 0
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > max_member_bytes:
                        raise ValueError(f"Tar entry too large: {info.name!r}")
                    out.write(chunk)
            written.append(target)
    return written


def validate_update_helper_paths(
    new_exe: Path, old_exe: Path, app_dir: Path
) -> Tuple[Path, Path]:
    """
    Ensure update helper paths stay under app_dir and contain no control chars.
    Returns resolved (new_exe, old_exe).
    """
    app_dir = Path(app_dir).resolve()
    resolved: List[Path] = []
    for label, raw in (("new_exe", new_exe), ("old_exe", old_exe)):
        p = Path(raw)
        text = str(p)
        if not text or any(c in text for c in ("\x00", "\n", "\r")):
            raise ValueError(f"Invalid {label}: control characters not allowed")
        # Reject characters that are hazardous if ever mis-handled by a shell
        if any(c in text for c in ("$", "`", ";", "|", "&", "\t")):
            raise ValueError(f"Invalid {label}: disallowed shell metacharacters")
        try:
            rp = p.resolve()
        except OSError as e:
            raise ValueError(f"Invalid {label}: {e}") from e
        try:
            rp.relative_to(app_dir)
        except ValueError as e:
            raise ValueError(
                f"{label} must be inside the application directory ({app_dir})"
            ) from e
        resolved.append(rp)
    return resolved[0], resolved[1]


def write_update_helper_config(
    path: Path,
    *,
    new_exe: Path,
    old_exe: Path,
    pid: int,
    sha256: Optional[str] = None,
) -> None:
    """Sidecar JSON for the update helper — owner-only perms, validated paths."""
    app_dir = Path(path).resolve().parent
    new_r, old_r = validate_update_helper_paths(new_exe, old_exe, app_dir)
    payload = {
        "new_exe": str(new_r),
        "old_exe": str(old_r),
        "pid": int(pid),
    }
    if sha256:
        digest = str(sha256).strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("sha256 must be a 64-char hex digest")
        payload["sha256"] = digest
    write_secret_file(path, json.dumps(payload))
