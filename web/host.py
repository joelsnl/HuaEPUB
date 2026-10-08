# Author: joelsnl and Anthropic Claude
"""Run the server-mode app inside the desktop process (uvicorn on a thread).

The desktop window calls ``ServerHost.start`` / ``stop``. ``serve_headless``
runs the same server with no window. Nothing here imports Qt. Secrets
(access code, password, cookies) are never printed, so they never reach
``huaepub.log``.
"""

from __future__ import annotations

import ipaddress
import os
import signal
import socket
import subprocess
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

        from web.context import ServerContext, book_roots
        from web.server import create_app
        from web.tasks import TaskManager

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


_HANDOFF = None
_PID_NAME = "headless.pid"
_STOP_NAME = "headless.stop"


def _server_dir(data_dir) -> Path:
    path = Path(data_dir) / "server"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pid_alive(pid: int) -> bool:
    """True when ``pid`` is a running process. Does not signal or stop it."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) calls TerminateProcess on Windows.
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            code = ctypes.c_ulong()
            ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel.CloseHandle(handle)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        return ctypes.get_last_error() == 5  # access denied: the process exists
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def running_headless_pid(data_dir) -> Optional[int]:
    """Pid of a live no-window server, or None. A dead pid file is removed."""
    path = Path(data_dir) / "server" / _PID_NAME
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid_alive(pid):
        return pid
    try:
        path.unlink()
    except OSError:
        pass
    return None


def request_headless_stop(data_dir) -> None:
    """Ask the no-window server to stop. It notices within about a second."""
    path = _server_dir(data_dir) / _STOP_NAME
    path.write_text("stop", encoding="utf-8")


def wait_headless_exit(pid: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


def keep_serving_after_gui(host, session) -> None:
    """Remember the live server so the process can drop the Qt window and keep it."""
    global _HANDOFF
    _HANDOFF = (host, session)


def finish_gui_handoff() -> Optional[int]:
    """After the window closes, block until the server is stopped. None if it did not hand off."""
    global _HANDOFF
    if _HANDOFF is None:
        return None
    host, session = _HANDOFF
    _HANDOFF = None
    return serve_until_stopped(host, session)


def _install_stop_signals() -> threading.Event:
    stopped = threading.Event()

    def _request_stop(_signum=None, _frame=None):
        stopped.set()

    try:
        signal.signal(signal.SIGINT, _request_stop)
        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, _request_stop)
    except ValueError:
        pass  # only the main thread may install handlers
    return stopped


def serve_until_stopped(host, session, stopped: Optional[threading.Event] = None) -> int:
    """Block until Ctrl+C, SIGTERM, or the stop file, then stop the server."""
    if stopped is None:
        stopped = _install_stop_signals()
    folder = _server_dir(session.data_dir)
    stop_path = folder / _STOP_NAME
    pid_path = folder / _PID_NAME
    try:
        stop_path.unlink()
    except OSError:
        pass
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        while not stopped.is_set():
            if stop_path.is_file():
                break
            stopped.wait(0.4)
    except KeyboardInterrupt:
        pass
    finally:
        if host.running:
            host.stop()
        try:
            session.close()
        except Exception:
            pass
        for path in (pid_path, stop_path):
            try:
                path.unlink()
            except OSError:
                pass
    return 0


def headless_requested(argv=None) -> bool:
    """True when this process should serve the browser app and skip Qt."""
    args = sys.argv[1:] if argv is None else list(argv)
    if "--headless" in args:
        return True
    # No display (Raspberry Pi OS Lite, or ssh). A Qt window cannot open.
    if sys.platform.startswith("linux") and not os.environ.get("QT_QPA_PLATFORM"):
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return True
    return False


def _write_terminal(text: str) -> None:
    """The real terminal, not print(). print() is copied into huaepub.log."""
    try:
        os.write(1, (text.rstrip() + "\n").encode("utf-8", errors="replace"))
    except OSError:
        pass


def serve_headless() -> int:
    """Serve until Ctrl+C or SIGTERM. Uses the saved server settings."""
    from core.logger import setup_logging
    from core.session import AppSession
    from core.updater import get_current_version

    session = AppSession()
    setup_logging(session.data_dir)
    settings = session.settings
    host = ServerHost(session, version=get_current_version())
    stopped = _install_stop_signals()
    try:
        status = host.start(
            mode=settings.get("server_mode") or "lan",
            port=int(settings.get("server_port") or DEFAULT_PORT),
            hostname=settings.get("server_hostname") or "",
            cert_path=settings.get("server_cert_path") or "",
            key_path=settings.get("server_key_path") or "",
        )
    except Exception as exc:
        host.log(f"Headless server could not start: {exc}")
        session.close()
        return 1
    lines = ["HuaEPUB is serving with no window.", status.local_url]
    if status.lan_url and status.lan_url != status.local_url:
        lines.append(status.lan_url)
    if status.public_url:
        lines.append(status.public_url)
    if status.mode == "lan" and os.environ.get("HUAEPUB_SERVICE"):
        lines.append("Access code is in ~/.huaepub/server/secret.json (lan_code).")
    elif status.mode == "lan":
        lines.append(f"Access code: {host.secrets.lan_code}")
    else:
        lines.append("Sign in with the server password.")
    _write_terminal("\n".join(lines))
    return serve_until_stopped(host, session, stopped)


SERVICE_NAMES = ("huaepub.service", "noveldownloader.service")


def install_service_requested() -> bool:
    return "--install-service" in sys.argv


def service_unit_roots() -> list:
    home = Path.home()
    return [
        home / ".config" / "systemd" / "user",
        Path("/etc/systemd/system"),
        Path("/lib/systemd/system"),
        Path("/usr/lib/systemd/system"),
    ]


def _is_huaepub_unit(path: Path) -> bool:
    if path.name in SERVICE_NAMES:
        try:
            return path.is_file()
        except OSError:
            return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return "--headless" in text and ("huaepub" in text or "noveldownloader" in text or "app.py" in text)


def existing_service_unit(roots=None) -> Optional[Path]:
    """A HuaEPUB unit that is already on disk. None when this machine has none."""
    for root in (service_unit_roots() if roots is None else roots):
        root = Path(root)
        for name in SERVICE_NAMES:
            path = root / name
            if _is_huaepub_unit(path):
                return path
        try:
            extras = [p for p in root.glob("*.service") if p.name not in SERVICE_NAMES]
        except OSError:
            continue
        for path in extras:
            if _is_huaepub_unit(path):
                return path
    return None


def _systemd_quote(text: str) -> str:
    if not text or any(ch.isspace() or ch in '"\\' for ch in text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _service_exec() -> tuple:
    if getattr(sys, "frozen", False):
        exe = str(Path(sys.executable).resolve())
        return str(Path(exe).parent), _systemd_quote(exe) + " --headless"
    app = str((Path(__file__).resolve().parents[1] / "app.py"))
    return str(Path(app).parent), _systemd_quote(sys.executable) + " " + _systemd_quote(app) + " --headless"


def _unit_text() -> str:
    work, cmd = _service_exec()
    return (
        "[Unit]\n"
        "Description=HuaEPUB\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "Environment=HUAEPUB_SERVICE=1\n"
        f"WorkingDirectory={_systemd_quote(work)}\n"
        f"ExecStart={cmd}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_user_service(*, roots=None, unit_dir=None, runner=None) -> tuple:
    """Write a user systemd unit when none exists, then enable it. Returns (code, message)."""
    found = existing_service_unit(roots)
    if found is not None:
        return 0, f"HuaEPUB service is already installed:\n{found}"
    if not sys.platform.startswith("linux"):
        return 1, "A systemd service is a Linux option. This machine is not Linux."
    dest_dir = Path(unit_dir) if unit_dir is not None else Path.home() / ".config" / "systemd" / "user"
    path = dest_dir / "huaepub.service"
    if path.is_file():
        return 0, f"HuaEPUB service is already installed:\n{path}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(_unit_text(), encoding="utf-8")
    lines = [f"Installed {path}"]

    def run(cmd):
        if runner is not None:
            return runner(cmd)
        try:
            return subprocess.run(cmd, check=False, capture_output=True, text=True)
        except FileNotFoundError:
            return None

    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if user:
        linger = run(["loginctl", "enable-linger", user])
        if linger is not None and getattr(linger, "returncode", 1) != 0:
            lines.append("To start at boot without a login: loginctl enable-linger")
    reload = run(["systemctl", "--user", "daemon-reload"])
    if reload is None:
        lines.append("systemctl was not found. When it is:")
        lines.append("systemctl --user daemon-reload")
        lines.append("systemctl --user enable --now huaepub.service")
        return 0, "\n".join(lines)
    enable = run(["systemctl", "--user", "enable", "--now", "huaepub.service"])
    if enable is None or getattr(enable, "returncode", 1) != 0:
        err = ""
        if enable is not None:
            err = (getattr(enable, "stderr", "") or getattr(enable, "stdout", "") or "").strip()
        return 1, f"Wrote {path} but could not enable it.\n{err}".rstrip()
    lines.append("Enabled huaepub.service.")
    return 0, "\n".join(lines)


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
