# Author: joelsnl and Anthropic Claude
"""Server-mode sign-in: access code, password, signed cookies, rate limits.

Secrets live in ``~/.huaepub/server/secret.json`` (owner-only, never
Drive-synced, never logged). Cookies are HMAC-signed so they survive an app
restart; rotating the code or the password bumps a generation number and
every older cookie stops working.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Dict, Optional

from core.security import write_secret_file

COOKIE_NAME = "huaepub_session"
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
CODE_LENGTH = 8
MIN_PASSWORD = 10
LAN_COOKIE_DAYS = 30
REMOTE_COOKIE_DAYS = 7
OPEN_LINK_SECONDS = 60

# scrypt cost: ~16 MiB, a few tens of ms per try on a desktop CPU.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def new_access_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(text: str) -> str:
    """Codes are typed on phones: ignore case, spaces and dashes."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class PasswordTooShort(ValueError):
    pass


class ServerSecrets:
    """Load/save the per-install key, access code and password hash."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: Dict[str, object] = {}
        self._load()

    # -- storage ---------------------------------------------------------
    def _load(self) -> None:
        raw: Dict[str, object] = {}
        try:
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded
        except Exception:
            raw = {}
        changed = False
        if not isinstance(raw.get("key"), str) or len(str(raw.get("key"))) < 32:
            raw["key"] = secrets.token_hex(32)
            changed = True
        if not isinstance(raw.get("lan_code"), str) or not raw.get("lan_code"):
            raw["lan_code"] = new_access_code()
            changed = True
        for gen in ("lan_gen", "pw_gen"):
            if not isinstance(raw.get(gen), int):
                raw[gen] = 1
                changed = True
        self._data = raw
        if changed:
            self._save()

    def _save(self) -> None:
        write_secret_file(self.path, json.dumps(self._data, indent=2))

    # -- values ----------------------------------------------------------
    @property
    def key(self) -> bytes:
        return bytes.fromhex(str(self._data["key"]))

    @property
    def lan_code(self) -> str:
        return str(self._data["lan_code"])

    @property
    def lan_gen(self) -> int:
        return int(self._data["lan_gen"])

    @property
    def pw_gen(self) -> int:
        return int(self._data["pw_gen"])

    @property
    def has_password(self) -> bool:
        return bool(self._data.get("pw_hash")) and bool(self._data.get("pw_salt"))

    def rotate_code(self) -> str:
        with self._lock:
            self._data["lan_code"] = new_access_code()
            self._data["lan_gen"] = self.lan_gen + 1
            self._save()
            return self.lan_code

    def set_password(self, password: str) -> None:
        if len(password or "") < MIN_PASSWORD:
            raise PasswordTooShort(f"Use at least {MIN_PASSWORD} characters.")
        salt = secrets.token_bytes(16)
        digest = hash_password(password, salt)
        with self._lock:
            self._data["pw_salt"] = salt.hex()
            self._data["pw_hash"] = digest.hex()
            self._data["pw_gen"] = self.pw_gen + 1
            self._save()

    def check_code(self, supplied: str) -> bool:
        a = normalize_code(supplied).encode("utf-8")
        b = normalize_code(self.lan_code).encode("utf-8")
        return bool(a) and hmac.compare_digest(a, b)

    def check_password(self, supplied: str) -> bool:
        if not self.has_password or not supplied:
            return False
        try:
            salt = bytes.fromhex(str(self._data["pw_salt"]))
            want = bytes.fromhex(str(self._data["pw_hash"]))
        except ValueError:
            return False
        return hmac.compare_digest(hash_password(supplied, salt), want)

    # -- cookies ---------------------------------------------------------
    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self.key, body.encode("ascii"), hashlib.sha256).digest())

    def make_cookie(self, mode: str, *, now: Optional[float] = None) -> str:
        days = REMOTE_COOKIE_DAYS if mode == "remote" else LAN_COOKIE_DAYS
        gen = self.pw_gen if mode == "remote" else self.lan_gen
        expires = int((now if now is not None else time.time()) + days * 86400)
        body = f"v1.{mode}.{gen}.{expires}.{secrets.token_hex(8)}"
        return f"{body}.{self._sign(body)}"

    def cookie_valid(self, value: str, mode: str, *, now: Optional[float] = None) -> bool:
        parts = (value or "").split(".")
        if len(parts) != 6 or parts[0] != "v1" or parts[1] != mode:
            return False
        body = ".".join(parts[:5])
        if not hmac.compare_digest(parts[5].encode("ascii", "ignore"), self._sign(body).encode("ascii")):
            return False
        try:
            gen = int(parts[2])
            expires = int(parts[3])
        except ValueError:
            return False
        want_gen = self.pw_gen if mode == "remote" else self.lan_gen
        if gen != want_gen:
            return False
        return expires > (now if now is not None else time.time())


@dataclass
class LoginVerdict:
    allowed: bool
    retry_after: int = 0


class LoginLimiter:
    """Failed sign-ins: per client address, plus a cap across all addresses."""

    def __init__(
        self,
        *,
        per_client: int = 5,
        per_client_window: float = 15 * 60,
        global_limit: int = 30,
        global_window: float = 10 * 60,
        global_pause: float = 10 * 60,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.per_client = per_client
        self.per_client_window = per_client_window
        self.global_limit = global_limit
        self.global_window = global_window
        self.global_pause = global_pause
        self._clock = clock
        self._lock = threading.Lock()
        self._by_client: Dict[str, Deque[float]] = defaultdict(deque)
        self._all: Deque[float] = deque()
        self._paused_until = 0.0

    def _trim(self, now: float) -> None:
        for ip in list(self._by_client):
            q = self._by_client[ip]
            while q and now - q[0] > self.per_client_window:
                q.popleft()
            if not q:
                del self._by_client[ip]
        while self._all and now - self._all[0] > self.global_window:
            self._all.popleft()

    def check(self, client: str) -> LoginVerdict:
        with self._lock:
            now = self._clock()
            self._trim(now)
            if now < self._paused_until:
                return LoginVerdict(False, int(self._paused_until - now) + 1)
            q = self._by_client.get(client)
            if q and len(q) >= self.per_client:
                return LoginVerdict(False, int(self.per_client_window - (now - q[0])) + 1)
            return LoginVerdict(True)

    def failed(self, client: str) -> None:
        with self._lock:
            now = self._clock()
            self._by_client[client].append(now)
            self._all.append(now)
            self._trim(now)
            if len(self._all) >= self.global_limit:
                self._paused_until = now + self.global_pause
                self._all.clear()

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._by_client.pop(client, None)


class OpenLinks:
    """One-time, short-lived sign-in links for the host PC's own browser."""

    def __init__(self, *, ttl: float = OPEN_LINK_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens: Dict[str, float] = {}

    def issue(self) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            now = self._clock()
            self._tokens = {t: exp for t, exp in self._tokens.items() if exp > now}
            self._tokens[token] = now + self.ttl
        return token

    def redeem(self, token: str) -> bool:
        with self._lock:
            exp = self._tokens.pop(token or "", None)
        return exp is not None and exp > self._clock()
