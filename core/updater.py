# Author: joelsnl and Anthropic Claude
"""
Auto-updater for Novel Downloader
Checks GitHub releases for updates and can download/install them.
Supports both source installations and compiled executables.

Security: updates require a matching SHA256SUMS entry from the same GitHub
release; zip members are path-checked; install paths are not shell-interpolated.
"""

import os
import sys
import shutil
import hashlib
import tempfile
import subprocess
import threading
import stat
from pathlib import Path
from typing import Optional, Tuple, Callable

from core.security import safe_extract_zip, write_update_helper_config

# Current version - UPDATE THIS WITH EACH RELEASE
__version__ = "2.2.2"

# GitHub repository info
GITHUB_REPO = "joelsnl/novelDownloader"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

SOURCE_ASSET_NAME = "novelDownloader-source.zip"


def get_current_version() -> str:
    """Get the current application version."""
    return __version__


def is_frozen() -> bool:
    """Check if running as a compiled executable (PyInstaller)."""
    return getattr(sys, 'frozen', False)


def get_app_dir() -> Path:
    """Get the application directory."""
    if is_frozen():
        return Path(sys.executable).parent
    else:
        return Path(os.path.dirname(os.path.abspath(__file__))).parent


def get_executable_path() -> Optional[Path]:
    """Get the path to the current executable (if frozen)."""
    if is_frozen():
        return Path(sys.executable)
    return None


def check_for_updates(callback: Optional[Callable[[bool, str, str], None]] = None) -> Tuple[bool, str, str]:
    """
    Check GitHub for updates.

    Returns:
        Tuple of (has_update: bool, latest_version: str, message: str)
    """
    try:
        try:
            from curl_cffi.requests import Session
            session = Session(impersonate="chrome120")
        except ImportError:
            import requests
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'NovelDownloader-Updater/1.0',
                'Accept': 'application/vnd.github.v3+json'
            })

        response = session.get(GITHUB_API_URL, timeout=15)

        if response.status_code == 404:
            return (False, __version__, "No releases found. You may be on the latest development version.")

        response.raise_for_status()
        release_data = response.json()

        latest_version = (release_data.get('tag_name') or '').lstrip('v')
        release_notes = (release_data.get('body') or '').strip() or 'No release notes available.'

        if not latest_version:
            return (False, __version__, "Could not determine latest version.")

        try:
            from packaging import version
            has_update = version.parse(latest_version) > version.parse(__version__)
        except Exception:
            has_update = latest_version != __version__

        if has_update:
            notes_preview = (
                release_notes if len(release_notes) <= 500
                else release_notes[:500] + '...'
            )
            message = f"New version {latest_version} available!\n\nRelease notes:\n{notes_preview}"
            if callback:
                callback(True, latest_version, message)
            return (True, latest_version, message)

        message = f"You're running the latest version ({__version__})."
        if callback:
            callback(False, latest_version, message)
        return (False, latest_version, message)

    except Exception as e:
        message = f"Failed to check for updates: {str(e)}"
        if callback:
            callback(False, __version__, message)
        return (False, __version__, message)


def _create_replacement_helper(new_exe: Path, old_exe: Path, app_dir: Path, pid: int) -> Path:
    """
    Write a small helper + JSON config (no shell-interpolated paths), then return
    the helper path to launch. Waits on the specific PID of the running app.

    Windows: rename-swap with retries. A hard Remove-Item right after exit often
    fails (AV / handle release) and previously aborted the whole script because
    of $ErrorActionPreference=Stop — leaving the old exe in place.
    """
    config_path = app_dir / '_update_helper.json'
    write_update_helper_config(config_path, new_exe=new_exe, old_exe=old_exe, pid=pid)

    if sys.platform == 'win32':
        script_path = app_dir / '_update_helper.ps1'
        # NOTE: do not use $pid — it is a PowerShell automatic variable.
        script_content = r'''$ErrorActionPreference = "Continue"
$logPath = Join-Path $PSScriptRoot "_update_helper.log"
function Write-UpdateLog([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}
try {
    $cfgPath = Join-Path $PSScriptRoot "_update_helper.json"
    $cfg = Get-Content -Raw -Encoding UTF8 $cfgPath | ConvertFrom-Json
    $pidWait = [int]$cfg.pid
    $newExe = [string]$cfg.new_exe
    $oldExe = [string]$cfg.old_exe
    $backupExe = Join-Path $PSScriptRoot "_update_backup.exe"
    Write-UpdateLog "Waiting for PID $pidWait to exit"
    Start-Sleep -Seconds 2
    while (Get-Process -Id $pidWait -ErrorAction SilentlyContinue) {
        Start-Sleep -Seconds 1
    }
    # Extra settle time: Windows / Defender often still holds the exe briefly.
    Start-Sleep -Seconds 2
    Write-UpdateLog "Replacing '$oldExe' with '$newExe'"
    if (-not (Test-Path -LiteralPath $newExe)) {
        throw "New executable missing: $newExe"
    }
    $replaced = $false
    for ($i = 1; $i -le 90; $i++) {
        try {
            if (Test-Path -LiteralPath $backupExe) {
                Remove-Item -LiteralPath $backupExe -Force -ErrorAction Stop
            }
            if (Test-Path -LiteralPath $oldExe) {
                # Rename works more reliably than delete on a just-exited exe.
                Move-Item -LiteralPath $oldExe -Destination $backupExe -Force -ErrorAction Stop
            }
            Move-Item -LiteralPath $newExe -Destination $oldExe -Force -ErrorAction Stop
            $replaced = $true
            Write-UpdateLog "Replace succeeded on attempt $i"
            break
        } catch {
            Write-UpdateLog "Attempt $i failed: $($_.Exception.Message)"
            # Roll back rename if we moved old aside but could not place new.
            if (-not (Test-Path -LiteralPath $oldExe) -and (Test-Path -LiteralPath $backupExe)) {
                Move-Item -LiteralPath $backupExe -Destination $oldExe -Force -ErrorAction SilentlyContinue
            }
            Start-Sleep -Seconds 1
        }
    }
    if (-not $replaced) {
        throw "Failed to replace executable after retries"
    }
    Remove-Item -LiteralPath $backupExe -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $cfgPath -Force -ErrorAction SilentlyContinue
    Write-UpdateLog "Launching updated app"
    Start-Process -FilePath $oldExe
    Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $logPath -Force -ErrorAction SilentlyContinue
} catch {
    Write-UpdateLog "FATAL: $($_.Exception.Message)"
}
'''
        script_path.write_text(script_content, encoding='utf-8')
        return script_path

    script_path = app_dir / '_update_helper.py'
    script_content = '''#!/usr/bin/env python3
import json, os, time, sys, traceback
from pathlib import Path
cfg_path = Path(__file__).with_name("_update_helper.json")
log_path = Path(__file__).with_name("_update_helper.log")

def log(msg):
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\\n")
    except OSError:
        pass

try:
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    pid = int(cfg["pid"])
    new_exe = Path(cfg["new_exe"])
    old_exe = Path(cfg["old_exe"])
    backup = Path(__file__).with_name("_update_backup")
    log(f"Waiting for PID {pid} to exit")
    time.sleep(2)
    while True:
        try:
            os.kill(pid, 0)
            time.sleep(1)
        except OSError:
            break
    time.sleep(2)
    if not new_exe.exists():
        raise FileNotFoundError(f"New executable missing: {new_exe}")
    replaced = False
    last_err = None
    for i in range(1, 91):
        try:
            if backup.exists():
                backup.unlink()
            if old_exe.exists():
                old_exe.replace(backup)
            new_exe.replace(old_exe)
            replaced = True
            log(f"Replace succeeded on attempt {i}")
            break
        except OSError as e:
            last_err = e
            log(f"Attempt {i} failed: {e}")
            if not old_exe.exists() and backup.exists():
                try:
                    backup.replace(old_exe)
                except OSError:
                    pass
            time.sleep(1)
    if not replaced:
        raise RuntimeError(f"Failed to replace executable after retries: {last_err}")
    os.chmod(old_exe, os.stat(old_exe).st_mode | 0o111)
    try:
        backup.unlink()
    except OSError:
        pass
    try:
        cfg_path.unlink()
    except OSError:
        pass
    log("Launching updated app")
    try:
        os.spawnv(os.P_NOWAIT, str(old_exe), [str(old_exe)])
    except Exception as e:
        log(f"Relaunch failed: {e}")
    try:
        Path(__file__).unlink()
    except OSError:
        pass
    try:
        log_path.unlink()
    except OSError:
        pass
except Exception:
    log("FATAL:\\n" + traceback.format_exc())
'''
    script_path.write_text(script_content, encoding='utf-8')
    os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC)
    return script_path


def _launch_replacement_script(script_path: Path):
    """Run the replacement helper in the background."""
    if sys.platform == 'win32':
        # DETACHED_PROCESS keeps the helper alive after the GUI exits.
        creationflags = subprocess.CREATE_NO_WINDOW | getattr(
            subprocess, "DETACHED_PROCESS", 0x00000008
        )
        subprocess.Popen(
            [
                'powershell',
                '-NoProfile',
                '-ExecutionPolicy', 'Bypass',
                '-File', str(script_path),
            ],
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            cwd=str(script_path.parent),
            close_fds=True,
        )
    else:
        python = sys.executable or 'python3'
        subprocess.Popen(
            [python, str(script_path)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(script_path.parent),
        )


def _find_asset(release_data: dict, name: str) -> Optional[dict]:
    wanted = name.lower()
    for asset in release_data.get('assets', []):
        if (asset.get('name') or '').lower() == wanted:
            return asset
    return None


def _find_platform_asset(release_data: dict) -> Optional[dict]:
    """Find the prebuilt executable zip for this platform in a release."""
    if sys.platform == 'win32':
        wanted = 'windows'
    elif sys.platform == 'darwin':
        wanted = 'macos'
    else:
        wanted = 'linux'

    for asset in release_data.get('assets', []):
        name = asset.get('name', '').lower()
        if wanted in name and name.endswith('.zip'):
            return asset
    return None


def _get_expected_checksum(session, release_data: dict, asset_name: str) -> Optional[str]:
    """Download the SHA256SUMS asset and return the expected hash for asset_name."""
    sums = _find_asset(release_data, 'SHA256SUMS.txt') or _find_asset(release_data, 'SHA256SUMS')
    if not sums:
        for asset in release_data.get('assets', []):
            if (asset.get('name') or '').upper().startswith('SHA256SUMS'):
                sums = asset
                break
    if not sums:
        return None

    url = sums.get('browser_download_url') or ''
    if not url:
        return None
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        for line in resp.text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].lstrip('*') == asset_name:
                return parts[0].lower()
    except Exception:
        return None
    return None


def _require_checksum(session, release_data: dict, asset_name: str, data: bytes) -> Tuple[bool, str]:
    """Fail closed unless SHA256 matches the release SUMS entry."""
    expected = _get_expected_checksum(session, release_data, asset_name)
    if not expected:
        return False, (
            "Update refused: this release has no SHA256SUMS entry for "
            f"'{asset_name}'.\nRefusing to install an unverified download."
        )
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        return False, (
            "Update failed: checksum mismatch.\n"
            "The downloaded file may be corrupted or tampered with."
        )
    print(f"  Checksum verified: {actual}")
    return True, actual


def _download_release_asset(session, asset: dict) -> Tuple[Optional[bytes], str]:
    url = asset.get('browser_download_url') or ''
    name = asset.get('name') or 'asset'
    if not url:
        return None, f"Release asset '{name}' has no download URL."
    response = session.get(url, timeout=300)
    response.raise_for_status()
    return response.content, name


def download_update(
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Tuple[bool, str]:
    """
    Download the latest version from GitHub and install it.

    Frozen builds: platform zip + required SHA256 verification.
    Source installs: novelDownloader-source.zip + required SHA256 verification.
    Never falls back to an unsigned main-branch zip.
    """
    try:
        if progress_callback:
            progress_callback(0, 100, "Connecting to GitHub...")

        try:
            from curl_cffi.requests import Session
            session = Session(impersonate="chrome120")
        except ImportError:
            import requests
            session = requests.Session()
            session.headers.update({'User-Agent': 'NovelDownloader-Updater/1.0'})

        api_response = session.get(GITHUB_API_URL, timeout=15)
        api_response.raise_for_status()
        release_data = api_response.json()

        app_dir = get_app_dir()

        if is_frozen():
            asset = _find_platform_asset(release_data)
            if not asset:
                return (False,
                    "No prebuilt asset for this platform in the latest release.\n"
                    "Install manually from GitHub Releases."
                )
            return _update_frozen_from_asset(
                session, release_data, asset, app_dir, progress_callback
            )

        source_asset = _find_asset(release_data, SOURCE_ASSET_NAME)
        if not source_asset:
            return (False,
                f"No '{SOURCE_ASSET_NAME}' in the latest release.\n"
                "Source auto-update requires a checksummed source archive.\n"
                "Pull from git or download the release manually."
            )
        return _update_source_from_asset(
            session, release_data, source_asset, app_dir, progress_callback
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return (False, f"Update failed: {str(e)}")


def _update_frozen_from_asset(
    session,
    release_data: dict,
    asset: dict,
    app_dir: Path,
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Tuple[bool, str]:
    """Update a compiled app from a checksum-verified prebuilt release asset."""
    if progress_callback:
        progress_callback(20, 100, f"Downloading {asset.get('name', '')}...")

    data, asset_name = _download_release_asset(session, asset)
    if data is None:
        return (False, asset_name)

    if progress_callback:
        progress_callback(60, 100, "Verifying download...")

    ok, msg = _require_checksum(session, release_data, asset_name, data)
    if not ok:
        return (False, msg)

    exe_name = 'NovelDownloader.exe' if sys.platform == 'win32' else 'NovelDownloader'

    old_exe = get_executable_path()
    if not old_exe:
        return (False, "Could not determine current executable path.")

    if progress_callback:
        progress_callback(80, 100, "Extracting update...")

    temp_new_exe = app_dir / f'_new_{exe_name}'
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        zip_path = temp_path / (asset_name or 'update.zip')
        zip_path.write_bytes(data)

        safe_extract_zip(zip_path, temp_path, allowed_names={exe_name})

        new_exe = None
        for candidate in temp_path.rglob(exe_name):
            if candidate.is_file():
                new_exe = candidate
                break
        if new_exe is None:
            return (False, f"Executable '{exe_name}' not found inside {asset_name}")

        shutil.copy2(new_exe, temp_new_exe)

    if sys.platform != 'win32':
        os.chmod(temp_new_exe, os.stat(temp_new_exe).st_mode | stat.S_IEXEC)

    if progress_callback:
        progress_callback(90, 100, "Scheduling replacement...")

    script_path = _create_replacement_helper(temp_new_exe, old_exe, app_dir, os.getpid())
    _launch_replacement_script(script_path)

    if progress_callback:
        progress_callback(100, 100, "Update ready!")

    return (True,
        "Update downloaded and verified!\n\n"
        "The application will now close to apply the update,\n"
        "then reopen automatically."
    )


def _update_source_from_asset(
    session,
    release_data: dict,
    asset: dict,
    app_dir: Path,
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Tuple[bool, str]:
    """Update a source install from a checksum-verified source zip asset."""
    if progress_callback:
        progress_callback(20, 100, f"Downloading {SOURCE_ASSET_NAME}...")

    data, asset_name = _download_release_asset(session, asset)
    if data is None:
        return (False, asset_name)

    if progress_callback:
        progress_callback(50, 100, "Verifying download...")

    ok, msg = _require_checksum(session, release_data, asset_name, data)
    if not ok:
        return (False, msg)

    if progress_callback:
        progress_callback(70, 100, "Extracting files...")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        zip_path = temp_path / asset_name
        zip_path.write_bytes(data)
        extract_root = temp_path / 'src'
        safe_extract_zip(zip_path, extract_root)

        extracted_dirs = [d for d in extract_root.iterdir() if d.is_dir()]
        if len(extracted_dirs) == 1 and (extracted_dirs[0] / 'app.py').exists():
            extracted_dir = extracted_dirs[0]
        elif (extract_root / 'app.py').exists():
            extracted_dir = extract_root
        else:
            return (False, "Source archive layout not recognized (app.py missing).")

        return _update_source_app(extracted_dir, app_dir, progress_callback)


def _update_source_app(
    extracted_dir: Path,
    app_dir: Path,
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Tuple[bool, str]:
    """Replace source files from an already-verified extracted tree."""
    if progress_callback:
        progress_callback(80, 100, "Installing update...")

    items_to_update = ['app.py', 'core', 'parsers', 'requirements.txt', 'README.md']

    backup_dir = app_dir / '.update_backup'
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(exist_ok=True)

    for item in items_to_update:
        src = extracted_dir / item
        dst = app_dir / item

        if not src.exists():
            continue

        if dst.exists():
            backup_dst = backup_dir / item
            if dst.is_dir():
                shutil.copytree(dst, backup_dst)
            else:
                shutil.copy2(dst, backup_dst)

        if progress_callback:
            progress_callback(90, 100, f"Updating {item}...")

        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)

    if progress_callback:
        progress_callback(100, 100, "Update complete!")

    return (True, "Update installed successfully!\nPlease restart the application.")


def check_for_updates_async(callback: Callable[[bool, str, str], None]):
    """Check for updates in a background thread."""
    thread = threading.Thread(target=check_for_updates, args=(callback,))
    thread.daemon = True
    thread.start()


def download_update_async(
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    completion_callback: Optional[Callable[[bool, str], None]] = None
):
    """Download and install update in a background thread."""
    def _download():
        success, message = download_update(progress_callback)
        if completion_callback:
            completion_callback(success, message)

    thread = threading.Thread(target=_download)
    thread.daemon = True
    thread.start()


def get_auto_check_updates() -> bool:
    """Get the auto-check updates preference."""
    from core.settings import get_setting
    return bool(get_setting('auto_check_updates'))


def set_auto_check_updates(enabled: bool):
    """Set the auto-check updates preference."""
    from core.settings import set_setting
    set_setting('auto_check_updates', enabled)
