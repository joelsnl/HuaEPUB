# Author: joelsnl and Anthropic Claude
"""``python3 -m web``: start HuaEPUB Simple on this computer.

Default binds 127.0.0.1 only. ``--lan`` binds this computer's private Wi-Fi
address instead, behind a one-time access code, so a phone on the same
network can use the page.
"""

from __future__ import annotations

import argparse
import ipaddress
import secrets
import socket
import sys
from typing import Optional

DEFAULT_PORT = 8765
_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def lan_address() -> Optional[str]:
    """This computer's private network address, or None. Sends no packets."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET address: only picks the route
        ip = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return ip if addr.is_private and not addr.is_loopback else None


def _say(text: str = "") -> None:
    # Bypass the log tee: the access code must never reach huaepub.log.
    out = sys.__stdout__ or sys.stdout
    out.write(text + "\n")
    out.flush()


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m web", description="HuaEPUB Simple")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument(
        "--lan",
        action="store_true",
        help="also let a phone on the same Wi-Fi use it (needs the printed access code)",
    )
    args = ap.parse_args(argv)

    try:
        import uvicorn
        import fastapi  # noqa: F401
    except ImportError:
        _say("HuaEPUB Simple needs: pip install -r requirements-web.txt")
        return 2

    from core.branding import SIMPLE_TITLE
    from core.cache import NovelCache
    from core.logger import setup_logging
    from core.settings import get_data_dir, load_settings
    from core.updater import __version__
    from core.utils import sanitize_runtime_env
    from web.jobs import JobManager
    from web.server import create_app

    sanitize_runtime_env()
    data_dir = get_data_dir()
    setup_logging(data_dir)

    host, code = "127.0.0.1", None
    if args.lan:
        ip = lan_address()
        if not ip:
            _say("No private Wi-Fi address found, so --lan was not started.")
            return 2
        host = ip
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    if not _port_is_free(host, args.port):
        _say(f"Port {args.port} on {host} is already in use. Close the other copy or use --port.")
        return 1

    cache = NovelCache(data_dir / "cache.db")
    try:
        workers = int(load_settings().get("workers", 200) or 200)
    except Exception:
        workers = 200
    manager = JobManager(data_dir / "simple" / "staging", cache, workers=workers)
    app = create_app(manager=manager, version=__version__, lan_code=code, cache=cache)

    url = f"http://{host}:{args.port}/"
    _say(f"{SIMPLE_TITLE} {__version__}")
    if code:
        full = f"{url}?code={code}"
        _say(f"Open on your phone (same Wi-Fi): {full}")
        try:
            import qrcode

            qr = qrcode.QRCode(border=1)
            qr.add_data(full)
            qr.print_ascii(out=sys.__stdout__, invert=True)
        except Exception:
            _say("(Install the optional 'qrcode' package to show a QR code here.)")
        _say("Keep this window open. Anyone with that link can use this page.")
    else:
        _say(f"Open {url} in your browser. Press Ctrl+C to stop.")
    try:
        uvicorn.run(app, host=host, port=args.port, log_level="warning", access_log=False)
    finally:
        cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
