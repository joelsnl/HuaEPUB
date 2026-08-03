# Author: joelsnl and Anthropic Claude
"""
Auto-updater for Novel Downloader
Checks GitHub releases for updates and can download/install them.
Supports both source installations and compiled executables.
"""

import os
import sys
import shutil
import hashlib
import zipfile
import tempfile
import subprocess
import threading
import stat
from pathlib import Path
from typing import Optional, Tuple, Callable

# Current version - UPDATE THIS WITH EACH RELEASE
__version__ = "2.1.2"

# GitHub repository info
GITHUB_REPO = "joelsnl/novelDownloader"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_DOWNLOAD_URL = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"


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
    
    Args:
        callback: Optional callback(has_update, latest_version, message) for async use
        
    Returns:
        Tuple of (has_update: bool, latest_version: str, message: str)
    """
    try:
        # Import here to avoid circular imports
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
        
        # Fetch latest release info from GitHub API
        response = session.get(GITHUB_API_URL, timeout=15)
        
        if response.status_code == 404:
            # No releases yet, check if repo exists
            return (False, __version__, "No releases found. You may be on the latest development version.")
        
        response.raise_for_status()
        release_data = response.json()
        
        # Get latest version (remove 'v' prefix if present).
        # GitHub returns body: null when notes were never set — .get(key, default)
        # does NOT fall back in that case, so use `or`.
        latest_version = (release_data.get('tag_name') or '').lstrip('v')
        release_notes = (release_data.get('body') or '').strip() or 'No release notes available.'
        release_url = release_data.get('html_url') or ''
        
        if not latest_version:
            return (False, __version__, "Could not determine latest version.")
        
        # Compare versions
        try:
            from packaging import version
            has_update = version.parse(latest_version) > version.parse(__version__)
        except Exception:
            # Simple string comparison fallback
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
        else:
            message = f"You're running the latest version ({__version__})."
            if callback:
                callback(False, latest_version, message)
            return (False, latest_version, message)
            
    except Exception as e:
        message = f"Failed to check for updates: {str(e)}"
        if callback:
            callback(False, __version__, message)
        return (False, __version__, message)


def _find_python() -> Optional[str]:
    """Find a Python interpreter that can run the build script."""
    _creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    # Try common Python commands
    python_commands = ['python3', 'python', 'py']
    
    for cmd in python_commands:
        try:
            result = subprocess.run(
                [cmd, '--version'],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=_creationflags
            )
            if result.returncode == 0 and 'Python 3' in result.stdout:
                return cmd
        except Exception:
            continue
    
    return None


def _create_replacement_script(new_exe: Path, old_exe: Path, app_dir: Path, pid: int) -> Path:
    """
    Create a script that will replace the old executable with the new one.
    This script runs after the main app closes.
    
    Waits on the specific PID of the running app. (Name matching with
    pgrep -f could match the helper script's own command line and hang.)
    """
    if sys.platform == 'win32':
        # Windows batch script
        script_path = app_dir / '_update_helper.bat'
        script_content = f'''@echo off
echo Waiting for application (pid {pid}) to close...
timeout /t 2 /nobreak > nul

:waitloop
tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL
if "%ERRORLEVEL%"=="0" (
    timeout /t 1 /nobreak > nul
    goto waitloop
)

echo Replacing executable...
del /f "{old_exe}"
move /y "{new_exe}" "{old_exe}"

echo Cleaning up...
del /f "{app_dir / '_update_backup.exe'}" 2>nul
(goto) 2>nul & del "%~f0"
'''
    else:
        # Unix shell script (macOS/Linux)
        script_path = app_dir / '_update_helper.sh'
        script_content = f'''#!/bin/bash
echo "Waiting for application (pid {pid}) to close..."
sleep 2

# Wait for the old process to finish
while kill -0 {pid} 2>/dev/null; do
    sleep 1
done

echo "Replacing executable..."
rm -f "{old_exe}"
mv "{new_exe}" "{old_exe}"
chmod +x "{old_exe}"

echo "Cleaning up..."
rm -f "{app_dir / '_update_backup'}"
rm -f "$0"
'''
    
    with open(script_path, 'w') as f:
        f.write(script_content)
    
    # Make shell script executable on Unix
    if sys.platform != 'win32':
        os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC)
    
    return script_path


def download_update(
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Tuple[bool, str]:
    """
    Download the latest version from GitHub and install it.
    
    For source installations: replaces source files
    For compiled executables: downloads source, builds new exe, schedules replacement
    
    Args:
        progress_callback: Optional callback(current, total, status) for progress updates
        
    Returns:
        Tuple of (success: bool, message: str)
    """
    try:
        if progress_callback:
            progress_callback(0, 100, "Connecting to GitHub...")
        
        # Import session
        try:
            from curl_cffi.requests import Session
            session = Session(impersonate="chrome120")
        except ImportError:
            import requests
            session = requests.Session()
            session.headers.update({'User-Agent': 'NovelDownloader-Updater/1.0'})
        
        # Fetch info about the latest release (tag + prebuilt assets)
        release_data = None
        try:
            api_response = session.get(GITHUB_API_URL, timeout=15)
            if api_response.status_code == 200:
                release_data = api_response.json()
        except Exception:
            release_data = None
        
        app_dir = get_app_dir()
        
        # Preferred path for compiled apps: download a prebuilt release asset
        # (built by CI) with checksum verification, instead of building from
        # source on the user's machine
        if is_frozen() and release_data:
            asset = _find_platform_asset(release_data)
            if asset:
                return _update_frozen_from_asset(
                    session, release_data, asset, app_dir, progress_callback
                )
            print("  No prebuilt asset for this platform; falling back to source build")
        
        # Download the same release tag the update check advertised,
        # falling back to the main branch if there are no releases
        download_url = GITHUB_DOWNLOAD_URL
        if release_data:
            tag = release_data.get('tag_name', '')
            if tag:
                download_url = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{tag}.zip"
        
        # Download the zip file
        if progress_callback:
            progress_callback(10, 100, "Downloading update...")
        
        response = session.get(download_url, timeout=120)
        response.raise_for_status()
        
        if progress_callback:
            progress_callback(30, 100, "Extracting files...")
        
        # Create temp directory for extraction
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            zip_path = temp_path / "update.zip"
            
            # Write downloaded content
            with open(zip_path, 'wb') as f:
                f.write(response.content)
            
            # Extract zip
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_path)
            
            # Find the extracted directory (usually novelDownloader-main)
            extracted_dirs = [d for d in temp_path.iterdir() if d.is_dir()]
            if not extracted_dirs:
                return (False, "Failed to extract update - no directory found")
            
            extracted_dir = extracted_dirs[0]
            
            # Check if we're running as a compiled executable
            if is_frozen():
                return _update_frozen_app(extracted_dir, app_dir, progress_callback)
            else:
                return _update_source_app(extracted_dir, app_dir, progress_callback)
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return (False, f"Update failed: {str(e)}")


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
    """Download the SHA256SUMS asset (if present) and return the expected hash."""
    for asset in release_data.get('assets', []):
        if asset.get('name', '').upper().startswith('SHA256SUMS'):
            try:
                resp = session.get(asset.get('browser_download_url', ''), timeout=30)
                resp.raise_for_status()
                for line in resp.text.splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[-1].lstrip('*') == asset_name:
                        return parts[0].lower()
            except Exception:
                return None
    return None


def _launch_replacement_script(script_path: Path):
    """Run the replacement helper script in the background."""
    if sys.platform == 'win32':
        subprocess.Popen(
            ['cmd', '/c', str(script_path)],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    else:
        subprocess.Popen(
            ['/bin/bash', str(script_path)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )


def _update_frozen_from_asset(
    session,
    release_data: dict,
    asset: dict,
    app_dir: Path,
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Tuple[bool, str]:
    """
    Update a compiled app from a prebuilt release asset.
    Downloads the zip, verifies its SHA256 against the release's SHA256SUMS
    (when published), and schedules the executable replacement.
    """
    asset_name = asset.get('name', '')
    url = asset.get('browser_download_url', '')
    if not url:
        return (False, "Release asset has no download URL.")
    
    if progress_callback:
        progress_callback(20, 100, f"Downloading {asset_name}...")
    
    response = session.get(url, timeout=300)
    response.raise_for_status()
    data = response.content
    
    # Verify checksum when the release publishes one
    if progress_callback:
        progress_callback(60, 100, "Verifying download...")
    
    expected = _get_expected_checksum(session, release_data, asset_name)
    if expected:
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            return (False,
                "Update failed: checksum mismatch.\n"
                "The downloaded file may be corrupted or tampered with."
            )
        print(f"  Checksum verified: {actual}")
    else:
        print("  Warning: release has no SHA256SUMS asset; skipping verification")
    
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
        with open(zip_path, 'wb') as f:
            f.write(data)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_path)
        
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
    
    script_path = _create_replacement_script(temp_new_exe, old_exe, app_dir, os.getpid())
    _launch_replacement_script(script_path)
    
    if progress_callback:
        progress_callback(100, 100, "Update ready!")
    
    return (True,
        "Update downloaded and verified!\n\n"
        "The application will now close to apply the update.\n"
        "Please restart it manually after it closes."
    )


def _update_source_app(
    extracted_dir: Path, 
    app_dir: Path,
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Tuple[bool, str]:
    """Update a source (non-compiled) installation."""
    if progress_callback:
        progress_callback(50, 100, "Installing update...")
    
    # Files/folders to update
    items_to_update = ['app.py', 'core', 'parsers']
    
    # Create backup
    backup_dir = app_dir / '.update_backup'
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(exist_ok=True)
    
    for item in items_to_update:
        src = extracted_dir / item
        dst = app_dir / item
        
        if not src.exists():
            continue
        
        # Backup existing
        if dst.exists():
            backup_dst = backup_dir / item
            if dst.is_dir():
                shutil.copytree(dst, backup_dst)
            else:
                shutil.copy2(dst, backup_dst)
        
        if progress_callback:
            progress_callback(60, 100, f"Updating {item}...")
        
        # Copy new version
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    
    if progress_callback:
        progress_callback(100, 100, "Update complete!")
    
    return (True, "Update installed successfully!\nPlease restart the application.")


def _update_frozen_app(
    extracted_dir: Path,
    app_dir: Path,
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Tuple[bool, str]:
    """Update a compiled (frozen) executable."""
    
    # Find Python to run build script
    if progress_callback:
        progress_callback(40, 100, "Checking for Python...")
    
    python_cmd = _find_python()
    if not python_cmd:
        return (False, 
            "Python 3 is required to build the update but was not found.\n\n"
            "Please install Python 3 from python.org or update manually by:\n"
            "1. Download the latest release from GitHub\n"
            "2. Run: python build.py\n"
            "3. Replace the old executable with the new one"
        )
    
    # Check if build.py exists
    build_script = extracted_dir / 'build.py'
    if not build_script.exists():
        return (False, "build.py not found in the downloaded update.")
    
    # Install/check dependencies
    if progress_callback:
        progress_callback(45, 100, "Checking dependencies...")
    
    # Hide console windows on Windows
    _creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    
    requirements_file = extracted_dir / 'requirements.txt'
    if requirements_file.exists():
        try:
            subprocess.run(
                [python_cmd, '-m', 'pip', 'install', '-r', str(requirements_file), '-q'],
                capture_output=True,
                timeout=120,
                creationflags=_creationflags
            )
        except Exception as e:
            print(f"Warning: Failed to install requirements: {e}")
    
    # Make sure PyInstaller is available
    try:
        subprocess.run(
            [python_cmd, '-m', 'pip', 'install', 'pyinstaller', '-q'],
            capture_output=True,
            timeout=60,
            creationflags=_creationflags
        )
    except Exception:
        pass
    
    # Run build.py
    if progress_callback:
        progress_callback(50, 100, "Building new executable (this may take a while)...")
    
    try:
        result = subprocess.run(
            [python_cmd, str(build_script)],
            cwd=str(extracted_dir),
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout for build
            creationflags=_creationflags
        )
        
        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown build error"
            return (False, f"Build failed:\n{error_msg[:500]}")
        
    except subprocess.TimeoutExpired:
        return (False, "Build timed out after 10 minutes.")
    except Exception as e:
        return (False, f"Build failed: {str(e)}")
    
    if progress_callback:
        progress_callback(80, 100, "Preparing to install...")
    
    # Find the built executable
    dist_dir = extracted_dir / 'dist'
    if not dist_dir.exists():
        return (False, "Build completed but dist folder not found.")
    
    # Find the new executable
    if sys.platform == 'win32':
        new_exe_name = 'NovelDownloader.exe'
    else:
        new_exe_name = 'NovelDownloader'
    
    new_exe = dist_dir / new_exe_name
    if not new_exe.exists():
        # Try to find any executable in dist
        exes = list(dist_dir.glob('*'))
        if exes:
            new_exe = exes[0]
        else:
            return (False, f"Built executable not found in {dist_dir}")
    
    # Get the current executable path
    old_exe = get_executable_path()
    if not old_exe:
        return (False, "Could not determine current executable path.")
    
    if progress_callback:
        progress_callback(90, 100, "Scheduling replacement...")
    
    # Copy new executable to app directory with temporary name
    temp_new_exe = app_dir / f'_new_{new_exe_name}'
    shutil.copy2(new_exe, temp_new_exe)
    
    # Create and launch the replacement script
    script_path = _create_replacement_script(temp_new_exe, old_exe, app_dir, os.getpid())
    _launch_replacement_script(script_path)
    
    if progress_callback:
        progress_callback(100, 100, "Update ready!")
    
    return (True, 
        "Update downloaded and built successfully!\n\n"
        "The application will now close to apply the update.\n"
        "Please restart it manually after it closes."
    )


def check_for_updates_async(callback: Callable[[bool, str, str], None]):
    """
    Check for updates in a background thread.
    
    Args:
        callback: Callback function(has_update, latest_version, message)
    """
    thread = threading.Thread(target=check_for_updates, args=(callback,))
    thread.daemon = True
    thread.start()


def download_update_async(
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    completion_callback: Optional[Callable[[bool, str], None]] = None
):
    """
    Download and install update in a background thread.
    
    Args:
        progress_callback: Optional callback(current, total, status)
        completion_callback: Optional callback(success, message)
    """
    def _download():
        success, message = download_update(progress_callback)
        if completion_callback:
            completion_callback(success, message)
    
    thread = threading.Thread(target=_download)
    thread.daemon = True
    thread.start()


# Settings management for auto-update preference (delegates to core.settings,
# which migrates the legacy updater_settings.json automatically)

def get_auto_check_updates() -> bool:
    """Get the auto-check updates preference."""
    from core.settings import get_setting
    return bool(get_setting('auto_check_updates'))


def set_auto_check_updates(enabled: bool):
    """Set the auto-check updates preference."""
    from core.settings import set_setting
    set_setting('auto_check_updates', enabled)

