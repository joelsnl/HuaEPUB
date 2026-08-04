# Author: joelsnl and Anthropic Claude
"""
Optional Google Drive sync for library metadata and EPUB files.

Offline-first: local ~/.noveldownloader/ is always usable. Drive is opt-in.
Auth uses browser OAuth (loopback) via google-auth-oauthlib.

Client config: ~/.noveldownloader/google_oauth_client.json
  (Desktop OAuth client JSON downloaded from Google Cloud Console)
Token cache: ~/.noveldownloader/google_token.json
"""

from __future__ import annotations

import io
import json
import re
import threading
import traceback
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

OAUTH_CLIENT_FILE = "google_oauth_client.json"
TOKEN_FILE = "google_token.json"
BOOKS_FOLDER_NAME = "books"
CUSTOM_ROOT_FOLDER_NAME = "NovelDownloader"

# Always sync into a visible My Drive folder (not hidden appDataFolder)
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# https://drive.google.com/drive/folders/ID or /folderview?id=ID
_FOLDER_ID_RE = re.compile(
    r'(?:/folders/|id=|/drive/u/\d+/folders/)([a-zA-Z0-9_-]{10,})'
)

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
        "Then click Connect again. Sync stays optional and offline-first."
    )


class DriveSync:
    """Thread-safe Google Drive sync helper."""

    def __init__(self):
        self._lock = threading.RLock()
        self._creds: Optional[Credentials] = None
        self._service = None
        self._root_id: Optional[str] = None  # NovelDownloader folder id
        self._books_id: Optional[str] = None
        self._email: str = ""

    def reset_layout_cache(self):
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
        return oauth_client_path().is_file()

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
                        "so files can be saved to My Drive → NovelDownloader."
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

    def logout(self):
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

    def _find_child(self, name: str, parent_id: str) -> Optional[str]:
        service = self._require_service()
        safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
        q = (
            f"name = '{safe_name}' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        results = service.files().list(
            q=q,
            spaces="drive",
            fields="files(id, name)",
            pageSize=10,
        ).execute()
        files = results.get("files") or []
        return files[0]["id"] if files else None

    def _create_folder(self, name: str, parent_id: str) -> str:
        service = self._require_service()
        meta = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        created = service.files().create(body=meta, fields="id").execute()
        return created["id"]

    def _resolve_root_folder(self) -> str:
        """Find or create the configured My Drive sync folder; return its id."""
        folder_name = self.configured_folder_name()
        folder_id = (get_setting("drive_folder_id") or "").strip()
        if folder_id:
            # Verify it still exists / is accessible
            try:
                meta = self._require_service().files().get(
                    fileId=folder_id, fields="id,name,mimeType,trashed"
                ).execute()
                if meta.get("trashed"):
                    raise DriveSyncError("Configured Drive folder is in trash")
                # Keep display name in sync when we know it
                if meta.get("name"):
                    set_setting("drive_folder_name", meta["name"])
                return folder_id
            except DriveSyncError:
                raise
            except Exception as e:
                print(f"Stored drive_folder_id unusable ({folder_id}): {e}")
                set_setting("drive_folder_id", "")

        root_id = self._find_child(folder_name, "root")
        if not root_id:
            root_id = self._create_folder(folder_name, "root")
        set_setting("drive_folder_id", root_id)
        return root_id

    def ensure_folder_layout(self) -> Tuple[str, str]:
        """
        Ensure configured My Drive folder + books/ exist. Returns (root_id, books_id).
        """
        with self._lock:
            if self._root_id and self._books_id:
                return self._root_id, self._books_id

            try:
                root_id = self._resolve_root_folder()
                books_id = self._find_child(BOOKS_FOLDER_NAME, root_id)
                if not books_id:
                    books_id = self._create_folder(BOOKS_FOLDER_NAME, root_id)
            except Exception as e:
                print(f"Drive folder layout retry after error: {e}")
                traceback.print_exc()
                try:
                    set_setting("drive_folder_id", "")
                    self._root_id = None
                    self._books_id = None
                    root_id = self._resolve_root_folder()
                    books_id = self._find_child(BOOKS_FOLDER_NAME, root_id)
                    if not books_id:
                        books_id = self._create_folder(BOOKS_FOLDER_NAME, root_id)
                except Exception as e2:
                    raise DriveSyncError(self._format_api_error(e2)) from e2

            self._root_id = root_id
            self._books_id = books_id
            return root_id, books_id

    def set_custom_folder(self, folder_name: str = "", folder_url_or_id: str = "") -> str:
        """
        Point sync at a custom My Drive folder.
        Prefer folder_url_or_id when provided; otherwise create/reuse folder_name under My Drive.
        Returns the folder id. Clears layout cache.
        """
        folder_url_or_id = (folder_url_or_id or "").strip()
        folder_name = (folder_name or "").strip() or CUSTOM_ROOT_FOLDER_NAME

        parsed_id = self.parse_folder_id(folder_url_or_id) if folder_url_or_id else ""
        self.reset_layout_cache()

        if parsed_id:
            set_setting("drive_folder_id", parsed_id)
            # Name will be refreshed on next ensure/get
            if folder_name and folder_name != CUSTOM_ROOT_FOLDER_NAME:
                set_setting("drive_folder_name", folder_name)
            root_id, _ = self.ensure_folder_layout()
            return root_id

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
            return None
        try:
            content = service.files().get_media(fileId=file_id).execute()
            if isinstance(content, bytes):
                raw = json.loads(content.decode("utf-8"))
            else:
                raw = json.loads(content)
            return library_data_from_dict(raw if isinstance(raw, dict) else {})
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
                service.files().update(
                    fileId=file_id,
                    media_body=media,
                    fields="id",
                ).execute()
            else:
                meta = {
                    "name": LIBRARY_FILE,
                    "parents": [root_id],
                    "mimeType": "application/json",
                }
                service.files().create(
                    body=meta,
                    media_body=media,
                    fields="id",
                ).execute()
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
        local = store.get_data()
        remote = self.pull_library()
        if remote is None:
            merged = local
        else:
            merged = merge_library(local, remote)
        store.replace_data(merged)
        self.push_library(merged)
        return merged

    # ------------------------------------------------------------------
    # EPUBs
    # ------------------------------------------------------------------

    def list_remote_books(self) -> Dict[str, str]:
        """Map filename → file id for books/ contents."""
        service = self._require_service()
        _, books_id = self.ensure_folder_layout()
        result: Dict[str, str] = {}
        page_token = None
        while True:
            kwargs = {
                "q": f"'{books_id}' in parents and trashed = false",
                "spaces": "drive",
                "fields": "nextPageToken, files(id, name)",
                "pageSize": 100,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = service.files().list(**kwargs).execute()
            for f in resp.get("files") or []:
                name = f.get("name") or ""
                if name.lower().endswith(".epub"):
                    result[name] = f["id"]
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

    def download_epub(self, file_id: str, dest_path: str) -> str:
        """Download a Drive EPUB to dest_path. Returns dest_path."""
        service = self._require_service()
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            request = service.files().get_media(fileId=file_id)
            with open(dest, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            return str(dest)
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
