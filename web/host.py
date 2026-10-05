# Author: joelsnl and Anthropic Claude
"""Run the server-mode app inside the desktop process (uvicorn on a thread).

The desktop window calls ``ServerHost.start`` / ``stop``; nothing here
imports Qt. Secrets (access code, password, cookies) are never printed, so
they never reach ``huaepub.log``.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

DEFAULT_PORT = 8765


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


def port_is_free(port: int, host: str = "0.0.0.0") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform != "win32":
            # Same as uvicorn: connections still in TIME_WAIT from the last run
            # must not make a free port look taken. (On Windows this option
            # would let us bind over a live listener, so it stays off.)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


class ServerStartError(Exception):
    pass


@dataclass
class ServerStatus:
    running: bool = False
    mode: str = "lan"
    port: int = DEFAULT_PORT
    https: bool = False
    lan_ip: str = ""
    hostname: str = ""
    fingerprint: str = ""
    own_certificate: bool = False
    error: str = ""

    @property
    def scheme(self) -> str:
        return "https" if self.https else "http"

    @property
    def local_url(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.port}/"

    @property
    def lan_url(self) -> str:
        return f"{self.scheme}://{self.lan_ip}:{self.port}/" if self.lan_ip else ""

    @property
    def public_url(self) -> str:
        return f"https://{self.hostname}:{self.port}/" if self.hostname else ""


class ServerHost:
    """Owns the uvicorn server thread and the shared ServerContext."""

    def __init__(self, session, *, version: str = "", data_dir: Optional[Path] = None,
                 log: Callable[[str], None] = print):
        self.session = session
        self.version = version
        self.data_dir = Path(data_dir or session.data_dir)
        self.log = log
        self.status = ServerStatus()
        self.ctx = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        from web.auth import ServerSecrets

        self.secrets = ServerSecrets(self.data_dir / "server" / "secret.json")

    @property
    def running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def start(self, *, mode: str = "lan", port: int = DEFAULT_PORT, hostname: str = "",
              cert_path: str = "", key_path: str = "") -> ServerStatus:
        if self.running:
            return self.status
        import uvicorn

        from web.books import drive_sync_after_change, start_drive_sync
        from web.context import ServerContext, book_roots
        from web.server import create_app
        from web.tasks import Busy, TaskManager

        if mode not in ("lan", "remote"):
            raise ServerStartError("Unknown server mode")
        if not 1024 <= int(port) <= 65535:
            raise ServerStartError("Pick a port between 1024 and 65535.")
        if mode == "remote" and not self.secrets.has_password:
            raise ServerStartError("Set a password before serving outside your network.")
        if not port_is_free(int(port)):
            raise ServerStartError(f"Port {port} is already in use. Close the other program or "
                                   "pick another port.")
        lan_ip = lan_address() or ""
        status = ServerStatus(mode=mode, port=int(port), lan_ip=lan_ip,
                              hostname=(hostname or "").strip())
        ssl_kwargs = {}
        if mode == "remote":
            from web import tls

            if cert_path and key_path:
                cert = tls.load_own_certificate(cert_path, key_path)
            else:
                names = [n for n in (lan_ip, status.hostname) if n]
                cert = tls.ensure_certificate(self.data_dir / "server", names)
            status.https = True
            status.fingerprint = cert.fingerprint
            status.own_certificate = not cert.generated
            ssl_kwargs = {"ssl_certfile": str(cert.cert_path), "ssl_keyfile": str(cert.key_path)}
        manager = TaskManager(
            self.session,
            file_roots=lambda: book_roots(self.session),
            after_library_change=drive_sync_after_change,
        )
        ctx = ServerContext(
            session=self.session, tasks=manager, secrets=self.secrets, mode=mode,
            version=self.version, https=status.https,
            hsts=status.https and status.own_certificate,
        )
        app = create_app(ctx)
        config = uvicorn.Config(
            app, host="0.0.0.0", port=int(port), log_level="warning", access_log=False,
            limit_concurrency=64, timeout_keep_alive=20, **ssl_kwargs,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # we are not on the main thread

        def run():
            try:
                server.run()
            except Exception as exc:  # pragma: no cover - surfaced through status
                status.error = str(exc)
                self.log(f"Server stopped with an error: {exc}")

        thread = threading.Thread(target=run, name="huaepub-server", daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not server.started and thread.is_alive():
            time.sleep(0.05)
        if not server.started:
            server.should_exit = True
            thread.join(2)
            raise ServerStartError(status.error or "The server did not start.")
        status.running = True
        self.status = status
        self.ctx = ctx
        self._server = server
        self._thread = thread
        where = "your network" if mode == "lan" else "anywhere (HTTPS)"
        self.log(f"Server mode on: port {port}, reachable from {where}")
        # The desktop skips its startup sync while serving: pull other devices' changes now.
        if self.session.settings.get("drive_sync_enabled"):
            try:
                start_drive_sync(manager)
            except Busy:
                pass
        return status

    def stop(self, timeout: float = 15.0) -> None:
        """Stop serving. A running download keeps its resume point."""
        ctx = self.ctx
        if ctx is not None:
            try:
                ctx.tasks.shutdown(timeout=timeout)
            except Exception as exc:
                self.log(f"Server: could not stop the running task cleanly: {exc}")
        server = self._server
        if server is not None:
            server.should_exit = True
        if self._thread is not None:
            self._thread.join(5)
        self._server = None
        self._thread = None
        self.ctx = None
        self.status = ServerStatus()
        self.log("Server mode off")

    # -- host-PC helpers -----------------------------------------------------
    def open_link(self) -> str:
        """A one-time sign-in link for the browser on this PC."""
        if self.ctx is None:
            return ""
        token = self.ctx.open_links.issue()
        return f"{self.status.local_url}open/{token}"

    def phone_link(self) -> str:
        """LAN: the QR link with the access code. Remote: the address only."""
        st = self.status
        if not st.running:
            return ""
        if st.mode == "lan":
            base = st.lan_url or st.local_url
            return f"{base}?code={self.secrets.lan_code}"
        return st.public_url or st.lan_url

    def task_snapshot(self):
        return self.ctx.tasks.snapshot() if self.ctx is not None else None

    def task_busy(self) -> bool:
        return bool(self.ctx is not None and self.ctx.tasks.is_busy())


def find_public_address(timeout: float = 8.0) -> str:
    """Ask api.ipify.org for this network's public IP (only when the user asks)."""
    from core.parser import create_http_session
    from core.security import safe_http_request

    http = create_http_session()
    try:
        resp = safe_http_request(http, "GET", "https://api.ipify.org", timeout=timeout)
        text = (getattr(resp, "text", "") or "").strip()
    finally:
        close = getattr(http, "close", None)
        if callable(close):
            close()
    ipaddress.ip_address(text)  # raises on anything that is not an address
    return text
