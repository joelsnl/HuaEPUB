# Author: joelsnl and Anthropic Claude
"""
Optional Google Drive sync for library metadata and EPUB files.

Offline-first: local ~/.huaepub/ is always usable. Drive is opt-in.
drive.file scope only — visible My Drive folder (never hidden appDataFolder).
Never sync cache.db, active_download.json, ~/.huaepub/polish/,
~/.huaepub/nmt/, glossary.json, glossary-qwen.json, or glossaries/.

Auth uses browser OAuth (loopback) via google-auth-oauthlib.

Client config: ~/.huaepub/google_oauth_client.json
  (Desktop OAuth client JSON downloaded from Google Cloud Console)
Token cache: ~/.huaepub/google_token.json

The GUI queues a silent auto-sync after Library Update / Update All
(and after Drive Connect) when Drive is enabled. Single / Multi do not.
"""

from __future__ import annotations

import io
import json
import re
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.library import (
    LIBRARY_FILE,
    LibraryData,
    library_data_from_dict,
    library_data_to_dict,
    library_payload_hash,
    merge_library,
)
from core.settings import get_data_dir, get_setting, set_setting
from core.branding import DRIVE_FOLDER_NAME

OAUTH_CLIENT_FILE = "google_oauth_client.json"
TOKEN_FILE = "google_token.json"
BOOKS_FOLDER_NAME = "books"
CUSTOM_ROOT_FOLDER_NAME = DRIVE_FOLDER_NAME

# Always sync into a visible My Drive folder (not hidden appDataFolder)
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# https://drive.google.com/drive/folders/ID or /folderview?id=ID
_FOLDER_ID_RE = re.compile(
    r'(?:/folders/|id=|/drive/u/\d+/folders/)([a-zA-Z0-9_-]{10,})'
)


@dataclass(frozen=True)
class RemoteEpubInfo:
    """Drive metadata for a file under books/."""
    id: str
    size: int = 0
    modified_time: str = ""  # RFC3339 from Drive


def _parse_drive_rfc3339(value: str) -> Optional[float]:
    """Parse Drive modifiedTime to UTC epoch seconds."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def local_epub_needs_push(local_path: Path, remote: Optional[RemoteEpubInfo]) -> bool:
    """
    True if local EPUB should be uploaded/updated on Drive.

    - Missing remotely → upload
    - Remote newer than local (by mtime) → keep remote (other device won)
    - Different size, or local clearly newer → push/overwrite
    """
    path = Path(local_path)
    if not path.is_file():
        return False
    if remote is None:
        return True

    st = path.stat()
    local_size = int(st.st_size)
    local_mtime = float(st.st_mtime)
    remote_size = int(remote.size or 0)
    remote_mtime = _parse_drive_rfc3339(remote.modified_time)

    # Don't clobber a newer copy that another device already uploaded
    if remote_mtime is not None and local_mtime < remote_mtime - 5:
        return False

    if remote_size > 0 and local_size != remote_size:
        return True
    if remote_mtime is not None and local_mtime > remote_mtime + 2:
        return True
    # Remote size unknown / zero: push if local is newer or sizes differ when known
    if remote_size == 0 and remote_mtime is None:
        return True
    return False


# Soft dependency — import errors become clear messages at connect time
_GOOGLE_IMPORT_ERROR: Optional[str] = None
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    from googleapiclient.errors import HttpError
except ImportError as e:
    _GOOGLE_IMPORT_ERROR = str(e)
    Request = Credentials = InstalledAppFlow = build = None  # type: ignore
    MediaFileUpload = MediaIoBaseDownload = HttpError = None  # type: ignore


class DriveSyncError(Exception):
    """User-facing Drive sync failure."""
    pass


class DriveRevisionConflict(DriveSyncError):
    """Remote library.json changed since the last pull; merge again before overwrite."""
    pass


def oauth_client_path() -> Path:
    return get_data_dir() / OAUTH_CLIENT_FILE


def token_path() -> Path:
    return get_data_dir() / TOKEN_FILE


def oauth_setup_instructions() -> str:
    path = oauth_client_path()
    return (
        "Google Drive sync needs a Desktop OAuth client JSON file.\n\n"
        "1. Open Google Cloud Console → create/select a project\n"
        "2. Enable the Google Drive API\n"
        "3. APIs & Services → Credentials → Create OAuth client ID\n"
        "   Application type: Desktop app\n"
        "4. Download the JSON and save it as:\n"
        f"   {path}\n\n"
        "Then click Connect again. Sync stays optional and offline-first.\n"
        "Scope is drive.file only — HuaEPUB can manage folders this app created, "
        "not a folder you made by hand with a different OAuth client."
    )


class DriveSync:
    """Thread-safe Google Drive sync helper."""

    def __init__(self):
        self._lock = threading.RLock()
        self._creds: Optional[Credentials] = None
        self._service = None
        self._root_id: Optional[str] = None  # Drive root folder id
        self._books_id: Optional[str] = None
        self._email: str = ""

    def reset_layout_cache(self) -> None:
        """Clear cached folder ids."""
        with self._lock:
            self._root_id = None
            self._books_id = None

    @staticmethod
    def configured_folder_name() -> str:
        name = (get_setting("drive_folder_name") or CUSTOM_ROOT_FOLDER_NAME).strip()
        return name or CUSTOM_ROOT_FOLDER_NAME

    @staticmethod
    def parse_folder_id(text: str) -> str:
        """Extract a Drive folder id from a URL or raw id string."""
        text = (text or "").strip()
        if not text:
            return ""
        m = _FOLDER_ID_RE.search(text)
        if m:
            return m.group(1)
        # Raw id (no URL)
        if re.fullmatch(r'[a-zA-Z0-9_-]{10,}', text):
            return text
        return ""

    def location_description(self) -> str:
        """Short human-readable where files are stored."""
        return f"My Drive / {self.configured_folder_name()}"

    def folder_web_link(self) -> str:
        """Browser link to the sync folder in My Drive."""
        folder_id = (get_setting("drive_folder_id") or "").strip()
        if not folder_id and self._root_id and self._root_id != "appDataFolder":
            folder_id = self._root_id
        if not folder_id:
            return ""
        return f"https://drive.google.com/drive/folders/{folder_id}"

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def client_configured(self) -> bool:
        path = oauth_client_path()
        if not path.is_file():
            return False
        # Tighten perms on user-copied Desktop OAuth client JSON
        try:
            from core.security import tighten_file_permissions
            tighten_file_permissions(path)
        except Exception:
            pass
        return True

    def is_connected(self) -> bool:
        with self._lock:
            return bool(self._creds and self._creds.valid)

    def connected_email(self) -> str:
        return self._email or ""

    def _load_token(self, scopes: List[str]) -> Optional[Credentials]:
        path = token_path()
        if not path.exists() or Credentials is None:
            return None
        try:
            # Tighten perms on older installs
            try:
                import os
                import stat as stat_mod
                os.chmod(path, stat_mod.S_IRUSR | stat_mod.S_IWUSR)
            except Exception:
                pass
            creds = Credentials.from_authorized_user_file(str(path), scopes)
            if not self._creds_have_scopes(creds, scopes):
                # Old token from hidden app-data mode — force a fresh browser login
                print(
                    "Drive token is missing required scopes "
                    f"(have={list(creds.scopes or [])}, need={scopes}). "
                    "Clearing token so Connect can re-authorize."
                )
                try:
                    path.unlink()
                except Exception:
                    pass
                return None
            return creds
        except Exception:
            return None

    @staticmethod
    def _creds_have_scopes(creds, scopes: List[str]) -> bool:
        if not creds:
            return False
        have = set(creds.scopes or [])
        if not have:
            # Some token files omit scopes; treat as unknown / stale
            return False
        return set(scopes).issubset(have)

    def _save_token(self, creds: Credentials):
        try:
            from core.security import write_secret_file
            write_secret_file(token_path(), creds.to_json())
        except Exception:
            pass

    def _format_api_error(self, err: Exception) -> str:
        """Turn Google API errors into something readable in the UI/log."""
        try:
            if HttpError is not None and isinstance(err, HttpError):
                status = getattr(err.resp, "status", "?")
                body = err.content.decode("utf-8", errors="replace") if err.content else ""
                print(f"Drive API error {status}: {body}")
                if status in (401, 403):
                    return (
                        f"Google Drive access denied (HTTP {status}).\n"
                        "Click Disconnect, then Connect again and approve Drive access "
                        f"so files can be saved to My Drive → {CUSTOM_ROOT_FOLDER_NAME}."
                    )
                return f"Google Drive API error (HTTP {status}): {body[:300]}"
        except Exception:
            pass
        return str(err)

    def _ensure_google_libs(self):
        if _GOOGLE_IMPORT_ERROR or build is None:
            raise DriveSyncError(
                "Google Drive libraries are not installed.\n"
                "Run: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
            )

    def login(self, location: Optional[str] = None) -> str:
        """
        Browser OAuth login. Returns connected email (may be empty if about
        call fails). Raises DriveSyncError on failure.
        """
        self._ensure_google_libs()
        if not self.client_configured():
            raise DriveSyncError(oauth_setup_instructions())

        with self._lock:
            creds = self._load_token(SCOPES)
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    self._save_token(creds)
                except Exception:
                    creds = None

            if not creds or not creds.valid:
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(oauth_client_path()), SCOPES
                    )
                    # Opens the system browser; loopback redirect
                    creds = flow.run_local_server(
                        port=0,
                        prompt="consent",
                        open_browser=True,
                    )
                    self._save_token(creds)
                except Exception as e:
                    raise DriveSyncError(f"Google sign-in failed: {e}") from e

            self._creds = creds
            self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
            self._root_id = None
            self._books_id = None
            self._email = self._fetch_email()
            return self._email

    def try_restore_session(self) -> bool:
        """Silently restore credentials from token if still valid."""
        if _GOOGLE_IMPORT_ERROR or not self.client_configured():
            return False
        with self._lock:
            creds = self._load_token(SCOPES)
            if not creds:
                return False
            try:
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    self._save_token(creds)
                if not creds.valid:
                    return False
                self._creds = creds
                self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
                self._root_id = None
                self._books_id = None
                self._email = self._fetch_email()
                return True
            except Exception:
                self._creds = None
                self._service = None
                return False

    def logout(self) -> None:
        with self._lock:
            self._creds = None
            self._service = None
            self._root_id = None
            self._books_id = None
            self._email = ""
            try:
                if token_path().exists():
                    token_path().unlink()
            except Exception:
                pass

    def _fetch_email(self) -> str:
        try:
            about = self._service.about().get(fields="user(emailAddress,displayName)").execute()
            user = about.get("user") or {}
            return user.get("emailAddress") or user.get("displayName") or ""
        except Exception:
            return ""

    def _require_service(self):
        self._ensure_google_libs()
        with self._lock:
            if not self._service:
                if not self.try_restore_session():
                    raise DriveSyncError("Not connected to Google Drive. Click Connect first.")
            return self._service

    # ------------------------------------------------------------------
    # Folder layout
    # ------------------------------------------------------------------

    def _escape_query_name(self, name: str) -> str:
        return (name or "").replace("\\", "\\\\").replace("'", "\\'")

    def _files_list(self, **kwargs) -> dict:
        """files().list with flags that make cross-device drive.file more reliable."""
        kwargs.setdefault("spaces", "drive")
        kwargs.setdefault("supportsAllDrives", True)
        kwargs.setdefault("includeItemsFromAllDrives", True)
        return self._require_service().files().list(**kwargs).execute()

    def _find_child(self, name: str, parent_id: str) -> Optional[str]:
        safe_name = self._escape_query_name(name)
        q = (
            f"name = '{safe_name}' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        results = self._files_list(q=q, fields="files(id, name)", pageSize=10)
        files = results.get("files") or []
        return files[0]["id"] if files else None

    def _list_folders_named(self, name: str) -> List[dict]:
        """All folders with this name still visible to drive.file (any parent)."""
        safe_name = self._escape_query_name(name)
        q = (
            f"name = '{safe_name}' "
            f"and mimeType = 'application/vnd.google-apps.folder' "
            f"and trashed = false"
        )
        results = self._files_list(
            q=q,
            fields="files(id, name, parents, modifiedTime)",
            pageSize=25,
            orderBy="modifiedTime desc",
        )
        return list(results.get("files") or [])

    def _find_library_file_global(self) -> Optional[Tuple[str, str]]:
        """
        Find an accessible library.json anywhere Drive lets this app see.
        Returns (file_id, parent_folder_id) or None.
        """
        safe_name = self._escape_query_name(LIBRARY_FILE)
        results = self._files_list(
            q=f"name = '{safe_name}' and trashed = false",
            fields="files(id, name, parents, modifiedTime)",
            pageSize=25,
            orderBy="modifiedTime desc",
        )
        for f in results.get("files") or []:
            parents = f.get("parents") or []
            if parents:
                return f["id"], parents[0]
        return None

    def _folder_list_accessible(self, folder_id: str) -> bool:
        """True if drive.file can list children of this folder."""
        try:
            self._files_list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="files(id)",
                pageSize=1,
            )
            return True
        except Exception as e:
            print(f"Cannot list Drive folder {folder_id}: {e}")
            return False

    def _folder_has_library(self, folder_id: str) -> bool:
        return bool(self._find_child(LIBRARY_FILE, folder_id))

    def _pick_best_sync_folder(self, candidates: List[dict]) -> Optional[str]:
        """Prefer a folder that already has library.json (cross-device sync)."""
        if not candidates:
            return None
        with_lib = [f for f in candidates if self._folder_has_library(f["id"])]
        pool = with_lib or candidates
        # Already ordered by modifiedTime desc from the API
        return pool[0]["id"]

    def _create_folder(self, name: str, parent_id: str) -> str:
        service = self._require_service()
        meta = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        created = service.files().create(
            body=meta, fields="id", supportsAllDrives=True
        ).execute()
        return created["id"]

    def _resolve_root_folder(self) -> str:
        """Find or create the configured My Drive sync folder; return its id."""
        from core.branding import LEGACY_DRIVE_FOLDER_NAME

        folder_name = self.configured_folder_name()
        folder_id = (get_setting("drive_folder_id") or "").strip()
        if folder_id:
            # Verify it still exists / is accessible
            try:
                meta = self._require_service().files().get(
                    fileId=folder_id,
                    fields="id,name,mimeType,trashed",
                    supportsAllDrives=True,
                ).execute()
                if meta.get("trashed"):
                    raise DriveSyncError("Configured Drive folder is in trash")
                # Keep display name in sync when we know it
                if meta.get("name"):
                    set_setting("drive_folder_name", meta["name"])
                # Empty stored folder with no library — try to rediscover a better one
                # (only when we can list; otherwise keep the id and let ensure error)
                if self._folder_list_accessible(folder_id) and not self._folder_has_library(folder_id):
                    better = self._discover_existing_sync_folder(folder_name)
                    if better and better != folder_id and self._folder_has_library(better):
                        print(
                            f"Stored Drive folder {folder_id} has no library.json; "
                            f"switching to {better} which does."
                        )
                        set_setting("drive_folder_id", better)
                        return better
                return folder_id
            except DriveSyncError:
                raise
            except Exception as e:
                print(f"Stored drive_folder_id unusable ({folder_id}): {e}")
                set_setting("drive_folder_id", "")

        root_id = self._discover_existing_sync_folder(folder_name)
        if not root_id:
            # Last chance: any library.json this app can see (e.g. legacy name)
            found = self._find_library_file_global()
            if found:
                _, parent_id = found
                print(f"Reusing Drive folder that already contains {LIBRARY_FILE}: {parent_id}")
                root_id = parent_id
        if not root_id:
            # Also try legacy folder name before creating a duplicate
            if folder_name != LEGACY_DRIVE_FOLDER_NAME:
                root_id = self._discover_existing_sync_folder(LEGACY_DRIVE_FOLDER_NAME)
                if root_id:
                    set_setting("drive_folder_name", LEGACY_DRIVE_FOLDER_NAME)
            if not root_id:
                print(f"No existing Drive sync folder found; creating '{folder_name}'")
                root_id = self._create_folder(folder_name, "root")
        set_setting("drive_folder_id", root_id)
        return root_id

    def _discover_existing_sync_folder(self, folder_name: str) -> Optional[str]:
        """
        Locate an existing sync folder without creating one.

        drive.file can miss 'root' parent queries across devices, so search by
        folder name app-wide and prefer any copy that already has library.json.
        """
        # 1) Direct child of My Drive root (fast path when visible)
        root_child = self._find_child(folder_name, "root")
        # 2) Any folder with this name the app can see
        named = self._list_folders_named(folder_name)
        ids = []
        if root_child:
            ids.append({"id": root_child, "name": folder_name})
        for f in named:
            if not any(x["id"] == f["id"] for x in ids):
                ids.append(f)
        picked = self._pick_best_sync_folder(ids)
        if picked:
            return picked
        # 3) library.json orphaned / under unexpected parent
        found = self._find_library_file_global()
        if found:
            return found[1]
        return None

    def ensure_folder_layout(self) -> Tuple[str, str]:
        """
        Ensure configured My Drive folder + books/ exist. Returns (root_id, books_id).

        Never silently abandons a user-selected folder_id to create a new empty
        HuaEPUB elsewhere — that made Sync report success while the real Drive
        folder never updated.
        """
        with self._lock:
            if self._root_id and self._books_id:
                return self._root_id, self._books_id

            explicit_id = (get_setting("drive_folder_id") or "").strip()
            try:
                root_id = self._resolve_root_folder()
                if not self._folder_list_accessible(root_id):
                    raise DriveSyncError(
                        "HuaEPUB can see this Drive folder but cannot list files "
                        "inside it (Google drive.file permission).\n\n"
                        "Usually the folder was not created by this app / OAuth client.\n"
                        "Fix: copy the same google_oauth_client.json from your other PC, "
                        "Disconnect + Connect again, then Change folder to the HuaEPUB "
                        "folder that the app created (Library → Open folder on that PC)."
                    )
                books_id = self._find_child(BOOKS_FOLDER_NAME, root_id)
                if not books_id:
                    try:
                        books_id = self._create_folder(BOOKS_FOLDER_NAME, root_id)
                    except Exception as e:
                        raise DriveSyncError(
                            f"Could not create books/ in the Drive folder:\n"
                            f"{self._format_api_error(e)}"
                        ) from e
            except DriveSyncError:
                raise
            except Exception as e:
                traceback.print_exc()
                # Do NOT clear drive_folder_id — user may have picked it via Change folder
                msg = self._format_api_error(e)
                if explicit_id:
                    raise DriveSyncError(
                        f"Cannot use the selected Drive folder.\n{msg}\n\n"
                        "Your folder choice was kept. Fix access (same OAuth client / "
                        "Connect again) or pick another folder."
                    ) from e
                raise DriveSyncError(msg) from e

            self._root_id = root_id
            self._books_id = books_id
            return root_id, books_id

    def inspect_sync_folder(self, folder_id: Optional[str] = None) -> dict:
        """
        Diagnose the current (or given) sync folder for the UI.
        Keys: folder_id, name, can_list, library_file_id, books_id,
        library_novels, epub_count, web_link, error
        """
        info = {
            "folder_id": "",
            "name": "",
            "can_list": False,
            "library_file_id": "",
            "books_id": "",
            "library_novels": 0,
            "epub_count": 0,
            "web_link": "",
            "error": "",
        }
        try:
            service = self._require_service()
            if folder_id:
                root_id = folder_id
            else:
                root_id, _ = self.ensure_folder_layout()
            info["folder_id"] = root_id
            info["web_link"] = f"https://drive.google.com/drive/folders/{root_id}"
            meta = service.files().get(
                fileId=root_id,
                fields="id,name,mimeType,trashed",
                supportsAllDrives=True,
            ).execute()
            info["name"] = meta.get("name") or ""
            if meta.get("trashed"):
                info["error"] = "Folder is in trash"
                return info
            info["can_list"] = self._folder_list_accessible(root_id)
            if not info["can_list"]:
                info["error"] = "Cannot list folder contents (drive.file access)"
                return info
            info["library_file_id"] = self._find_library_file(root_id) or ""
            info["books_id"] = self._find_child(BOOKS_FOLDER_NAME, root_id) or ""
            if info["library_file_id"]:
                try:
                    content = service.files().get_media(
                        fileId=info["library_file_id"]
                    ).execute()
                    raw = json.loads(
                        content.decode("utf-8") if isinstance(content, bytes) else content
                    )
                    data = library_data_from_dict(raw if isinstance(raw, dict) else {})
                    info["library_novels"] = len(data.library)
                except Exception as e:
                    info["error"] = f"library.json unreadable: {e}"
            if info["books_id"]:
                try:
                    # Temporarily use books id via list
                    resp = self._files_list(
                        q=(
                            f"'{info['books_id']}' in parents and trashed = false "
                            f"and name contains '.epub'"
                        ),
                        fields="files(id, name)",
                        pageSize=100,
                    )
                    info["epub_count"] = len(resp.get("files") or [])
                except Exception:
                    pass
        except Exception as e:
            info["error"] = self._format_api_error(e)
        return info

    def set_custom_folder(self, folder_name: str = "", folder_url_or_id: str = "") -> str:
        """
        Point sync at a custom My Drive folder.
        Prefer folder_url_or_id when provided; otherwise create/reuse folder_name under My Drive.
        Returns the folder id. Clears layout cache.
        Raises DriveSyncError if a pasted folder URL/ID is not usable.
        """
        folder_url_or_id = (folder_url_or_id or "").strip()
        folder_name = (folder_name or "").strip() or CUSTOM_ROOT_FOLDER_NAME

        parsed_id = self.parse_folder_id(folder_url_or_id) if folder_url_or_id else ""
        self.reset_layout_cache()

        if parsed_id:
            service = self._require_service()
            try:
                meta = service.files().get(
                    fileId=parsed_id,
                    fields="id,name,mimeType,trashed",
                    supportsAllDrives=True,
                ).execute()
            except Exception as e:
                raise DriveSyncError(
                    "Cannot open that Drive folder with this app.\n"
                    f"{self._format_api_error(e)}\n\n"
                    "Paste the folder URL from Library → Open folder on the PC "
                    "that already syncs, and use the same google_oauth_client.json."
                ) from e
            if meta.get("trashed"):
                raise DriveSyncError("That Drive folder is in the trash.")
            if meta.get("mimeType") != "application/vnd.google-apps.folder":
                raise DriveSyncError("That ID is not a folder.")
            if not self._folder_list_accessible(parsed_id):
                raise DriveSyncError(
                    f"Folder “{meta.get('name') or parsed_id}” is visible but "
                    "HuaEPUB cannot list files inside it.\n\n"
                    "Google only allows this app to manage folders it created "
                    "(drive.file scope). Use the HuaEPUB folder created by the app "
                    "on your other device, with the same OAuth client JSON."
                )
            set_setting("drive_folder_id", parsed_id)
            set_setting("drive_folder_name", meta.get("name") or folder_name)
            # Ensure books/ exists inside the chosen folder (do not switch away)
            books_id = self._find_child(BOOKS_FOLDER_NAME, parsed_id)
            if not books_id:
                books_id = self._create_folder(BOOKS_FOLDER_NAME, parsed_id)
            self._root_id = parsed_id
            self._books_id = books_id
            return parsed_id

        set_setting("drive_folder_id", "")
        set_setting("drive_folder_name", folder_name)
        root_id, _ = self.ensure_folder_layout()
        return root_id

    # ------------------------------------------------------------------
    # Library JSON
    # ------------------------------------------------------------------

    def _find_library_file(self, root_id: str) -> Optional[str]:
        return self._find_child(LIBRARY_FILE, root_id)

    def pull_library(self) -> Optional[LibraryData]:
        """Download remote library.json, or None if it does not exist."""
        service = self._require_service()
        root_id, _ = self.ensure_folder_layout()
        file_id = self._find_library_file(root_id)
        if not file_id:
            # Folder layout may be a fresh empty duplicate — find library.json app-wide
            found = self._find_library_file_global()
            if found:
                file_id, parent_id = found
                if parent_id != root_id:
                    print(
                        f"library.json is under folder {parent_id}, not current "
                        f"root {root_id}; switching sync folder."
                    )
                    set_setting("drive_folder_id", parent_id)
                    self.reset_layout_cache()
            else:
                set_setting("drive_library_revision", "")
                return None
        try:
            try:
                meta = service.files().get(
                    fileId=file_id,
                    fields="id,headRevisionId",
                    supportsAllDrives=True,
                ).execute()
                set_setting(
                    "drive_library_revision",
                    str(meta.get("headRevisionId") or ""),
                )
            except Exception as e:
                print(f"Warning: could not read library.json revision: {e}")
            content = service.files().get_media(fileId=file_id).execute()
            if isinstance(content, bytes):
                raw = json.loads(content.decode("utf-8"))
            else:
                raw = json.loads(content)
            data = library_data_from_dict(raw if isinstance(raw, dict) else {})
            print(f"Pulled library.json from Drive: {len(data.library)} novel(s)")
            return data
        except Exception as e:
            raise DriveSyncError(
                f"Failed to download library.json: {self._format_api_error(e)}"
            ) from e

    def push_library(self, data: LibraryData) -> str:
        """Upload/overwrite library.json. Returns content hash."""
        from googleapiclient.http import MediaIoBaseUpload

        service = self._require_service()
        root_id, _ = self.ensure_folder_layout()
        payload = library_data_to_dict(data)
        content_hash = library_payload_hash(payload)
        body_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

        file_id = self._find_library_file(root_id)
        media = MediaIoBaseUpload(
            io.BytesIO(body_bytes),
            mimetype="application/json",
            resumable=False,
        )

        try:
            if file_id:
                try:
                    meta = service.files().get(
                        fileId=file_id,
                        fields="id,headRevisionId",
                        supportsAllDrives=True,
                    ).execute()
                    remote_rev = str(meta.get("headRevisionId") or "")
                    expected = str(get_setting("drive_library_revision") or "")
                    if expected and remote_rev and remote_rev != expected:
                        raise DriveRevisionConflict(
                            "library.json changed on Drive since the last pull. "
                            "Sync again so tombstones are not overwritten."
                        )
                except DriveRevisionConflict:
                    raise
                except Exception as e:
                    print(f"Warning: could not compare library.json revision: {e}")
                updated = service.files().update(
                    fileId=file_id,
                    media_body=media,
                    fields="id,modifiedTime,size,headRevisionId",
                    supportsAllDrives=True,
                ).execute()
                set_setting(
                    "drive_library_revision",
                    str(updated.get("headRevisionId") or ""),
                )
                print(
                    f"Updated Drive library.json id={updated.get('id')} "
                    f"modified={updated.get('modifiedTime')} "
                    f"novels={len(data.library)}"
                )
            else:
                meta = {
                    "name": LIBRARY_FILE,
                    "parents": [root_id],
                    "mimeType": "application/json",
                }
                created = service.files().create(
                    body=meta,
                    media_body=media,
                    fields="id,modifiedTime,headRevisionId",
                    supportsAllDrives=True,
                ).execute()
                set_setting(
                    "drive_library_revision",
                    str(created.get("headRevisionId") or ""),
                )
                print(
                    f"Created Drive library.json id={created.get('id')} "
                    f"in folder {root_id} novels={len(data.library)}"
                )
        except DriveRevisionConflict:
            raise
        except Exception as e:
            raise DriveSyncError(
                f"Failed to upload library.json: {self._format_api_error(e)}"
            ) from e

        set_setting("drive_library_hash", content_hash)
        return content_hash

    def sync_library_with_store(self, store) -> LibraryData:
        """
        Pull remote (if any), merge with local store, write local, push merged.
        Returns merged LibraryData.
        """
        last_conflict: DriveRevisionConflict | None = None
        for attempt in range(2):
            local = store.get_data()
            remote = self.pull_library()
            if remote is None:
                merged = local
                print(
                    f"No remote library.json yet; keeping local "
                    f"({len(local.library)} novel(s))"
                )
            else:
                merged = merge_library(local, remote)
                print(
                    f"Merged library: local={len(local.library)} "
                    f"remote={len(remote.library)} → {len(merged.library)}"
                )
                # Never let an empty local wipe a populated remote due to bad merge
                if (
                    not merged.library
                    and remote.library
                    and not getattr(local, "removed", None)
                ):
                    print("Merge produced empty library; keeping remote copy")
                    merged = remote

            # Attach Drive EPUB ids so Download EPUB works on other devices
            try:
                remote_books = self.list_remote_books()
                if remote_books:
                    for entry in merged.library:
                        name = entry.epub_filename or (
                            Path(entry.output_path).name if entry.output_path else ""
                        )
                        if name and name in remote_books:
                            entry.drive_file_id = remote_books[name].id
                            if not entry.epub_filename:
                                entry.epub_filename = name
            except Exception as e:
                print(f"Warning: could not attach remote EPUB ids: {e}")

            store.replace_data(merged)
            try:
                self.purge_removed_epubs(merged)
            except Exception as e:
                print(f"Warning: could not delete removed Drive EPUBs: {e}")
            # Avoid uploading an empty library over a non-empty remote we failed to read
            try:
                if (
                    merged.library
                    or getattr(merged, "removed", None)
                    or remote is None
                    or not getattr(remote, "library", None)
                ):
                    self.push_library(merged)
                else:
                    print("Skipping push of empty library over known remote novels")
                return merged
            except DriveRevisionConflict as e:
                last_conflict = e
                print(
                    "Drive library.json changed during sync; "
                    f"merging again ({attempt + 1}/2)"
                )
        raise last_conflict or DriveRevisionConflict(
            "library.json changed on Drive; sync again."
        )

    # ------------------------------------------------------------------
    # EPUBs
    # ------------------------------------------------------------------

    def list_remote_books(self) -> Dict[str, RemoteEpubInfo]:
        """Map filename → Drive metadata for books/ contents."""
        _, books_id = self.ensure_folder_layout()
        result: Dict[str, RemoteEpubInfo] = {}
        page_token = None
        while True:
            kwargs = {
                "q": f"'{books_id}' in parents and trashed = false",
                "fields": "nextPageToken, files(id, name, size, modifiedTime)",
                "pageSize": 100,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = self._files_list(**kwargs)
            for f in resp.get("files") or []:
                name = f.get("name") or ""
                if name.lower().endswith(".epub"):
                    try:
                        size = int(f.get("size") or 0)
                    except (TypeError, ValueError):
                        size = 0
                    result[name] = RemoteEpubInfo(
                        id=f["id"],
                        size=size,
                        modified_time=f.get("modifiedTime") or "",
                    )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return result

    def upload_epub(self, local_path: str, remote_name: Optional[str] = None) -> str:
        """Upload or update an EPUB. Returns Drive file id."""
        service = self._require_service()
        path = Path(local_path)
        if not path.is_file():
            raise DriveSyncError(f"EPUB not found: {local_path}")
        remote_name = remote_name or path.name
        _, books_id = self.ensure_folder_layout()

        existing_id = self._find_child(remote_name, books_id)
        media = MediaFileUpload(str(path), mimetype="application/epub+zip", resumable=True)
        try:
            if existing_id:
                updated = service.files().update(
                    fileId=existing_id,
                    media_body=media,
                    fields="id",
                ).execute()
                return updated["id"]
            meta = {
                "name": remote_name,
                "parents": [books_id],
                "mimeType": "application/epub+zip",
            }
            created = service.files().create(
                body=meta,
                media_body=media,
                fields="id",
            ).execute()
            return created["id"]
        except Exception as e:
            raise DriveSyncError(
                f"Failed to upload EPUB: {self._format_api_error(e)}"
            ) from e

    def delete_epub(self, file_id: str = "", remote_name: str = "") -> bool:
        """Trash a Drive EPUB (by id or filename under books/)."""
        service = self._require_service()
        _, books_id = self.ensure_folder_layout()
        target = (file_id or "").strip()
        name = (remote_name or "").strip()
        if not target and name:
            target = self._find_child(name, books_id) or ""
        if not target:
            return False
        try:
            service.files().update(
                fileId=target,
                body={"trashed": True},
                supportsAllDrives=True,
            ).execute()
            print(f"Trashed Drive EPUB id={target} name={name}")
            return True
        except Exception as e:
            err = str(e).lower()
            if "404" in err or "not found" in err:
                return True
            print(f"Failed to trash Drive EPUB {name or target}: {e}")
            return False

    def purge_removed_epubs(self, data: LibraryData) -> int:
        """Trash Drive EPUBs listed on tombstones and not used by a live entry."""
        removed = list(getattr(data, "removed", None) or [])
        if not removed:
            return 0
        live_ids = {
            (e.drive_file_id or "").strip()
            for e in data.library
            if (e.drive_file_id or "").strip()
        }
        live_names = set()
        for e in data.library:
            name = (e.epub_filename or "").strip()
            if not name and e.output_path:
                try:
                    name = Path(e.output_path).name
                except Exception:
                    name = ""
            if name:
                live_names.add(name)
        deleted = 0
        for tomb in removed:
            fid = (tomb.drive_file_id or "").strip()
            name = (tomb.epub_filename or "").strip()
            if fid and fid in live_ids:
                continue
            if name and name in live_names:
                continue
            if self.delete_epub(file_id=fid, remote_name=name):
                deleted += 1
        return deleted

    def download_epub(
        self, file_id: str, dest_path: str, *, allowed_root: Optional[Path] = None
    ) -> str:
        """Download a Drive EPUB to dest_path. Returns dest_path."""
        from core.security import path_is_under, safe_epub_basename

        service = self._require_service()
        dest = Path(dest_path)
        name = safe_epub_basename(dest.name)
        if not name:
            raise DriveSyncError("Refusing Drive save: destination must be a .epub file")
        try:
            parent = dest.parent.resolve()
            dest = (parent / name).resolve()
            dest.relative_to(parent)
        except (ValueError, OSError) as e:
            raise DriveSyncError(f"Refusing Drive save: invalid path ({e})") from e
        if allowed_root is not None and not path_is_under(dest, Path(allowed_root)):
            raise DriveSyncError("Refusing Drive save outside the books folder")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            request = service.files().get_media(fileId=file_id)
            with open(dest, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            return str(dest)
        except DriveSyncError:
            raise
        except Exception as e:
            raise DriveSyncError(
                f"Failed to download EPUB: {self._format_api_error(e)}"
            ) from e


# Module-level singleton used by the app
_drive_sync: Optional[DriveSync] = None
_drive_lock = threading.Lock()


def get_drive_sync() -> DriveSync:
    global _drive_sync
    with _drive_lock:
        if _drive_sync is None:
            _drive_sync = DriveSync()
        return _drive_sync
