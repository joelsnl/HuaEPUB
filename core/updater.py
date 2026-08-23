# Author: joelsnl and Anthropic Claude
"""
Auto-updater for HuaEPUB
Checks GitHub releases for updates and can download/install them.
Supports both source installations and compiled (PyInstaller onefile) executables.

Security: updates require a matching SHA256SUMS entry from the same GitHub
release; zip members are path-checked; install paths are not shell-interpolated.
Never fall back to unsigned main.zip.

Relaunch (all OSes): the GUI must quit so a helper can swap files and start
the new binary. Helpers strip _PYI_* / stale _MEI env and set
PYINSTALLER_RESET_ENVIRONMENT=1 — otherwise PyInstaller 6.9+ treats the new
onefile process as a worker of the dying extract and the window never returns.
Windows helper: ShellExecute a hidden powershell.exe -File so it is not a
child of the onefile bootloader (Popen children die when the GUI exits).
POSIX: /bin/sh or /bin/bash — never sys.executable when frozen. Relaunch a
bare Mach-O / ELF with double-fork + exec — never /usr/bin/open (that opens
Terminal.app for Unix executables).
"""

import os
import sys
import json
import shutil
import hashlib
import tempfile
import subprocess
import threading
import stat
import time
from pathlib import Path
from typing import Optional, Tuple, Callable, Set

from core.branding import (
    EXE_BASENAME,
    LEGACY_EXE_BASENAME,
    LEGACY_SOURCE_ASSET_NAME,
    SOURCE_ASSET_NAME,
    UPDATER_USER_AGENT,
)
from core.security import (
    UnsafeURLError,
    safe_extract_zip,
    safe_http_request,
    validate_github_asset_host,
    write_secret_file,
    write_update_helper_config,
)

SOURCE_UPDATE_ITEMS = [
    'app.py', 'core', 'gui', 'parsers', 'requirements.txt', 'README.md', 'build.py',
]

# Current version - UPDATE THIS WITH EACH RELEASE
__version__ = "2.12.0"

# GitHub repository (renamed from joelsnl/novelDownloader; GitHub redirects the old path)
GITHUB_REPO = "joelsnl/HuaEPUB"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Set when a frozen Windows update already swapped the on-disk exe and the
# GUI should relaunch that path after showing the success dialog.
_pending_relaunch_exe: Optional[Path] = None

# Inherited from the old frozen process; they make python3/powershell fail or
# make the new onefile instance reuse a _MEI folder that is about to be deleted.
_RELAUNCH_DROP_ENV = frozenset({
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONNOUSERSITE",
    "_MEIPASS2",
})

# CREATE_BREAKAWAY_FROM_JOB — keep the helper alive if a job would kill children.
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008
_SW_HIDE = 0

# Inlined into POSIX helpers (system python3 cannot import this module).
# Double-fork + exec so the GUI is not a child of a shell/python session.
_SPAWN_DETACHED_PY = r'''
def spawn_detached(argv, cwd, env):
    argv = [str(a) for a in argv]
    cwd = str(cwd) if cwd else None
    try:
        pid = os.fork()
    except (AttributeError, OSError):
        kw = dict(
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        if sys.platform == "win32":
            kw["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            kw["start_new_session"] = True
        subprocess.Popen(argv, **kw)
        return
    if pid > 0:
        os.waitpid(pid, 0)
        return
    try:
        os.setsid()
    except OSError:
        pass
    try:
        pid2 = os.fork()
    except OSError:
        os._exit(1)
    if pid2 > 0:
        os._exit(0)
    if cwd:
        try:
            os.chdir(cwd)
        except OSError:
            pass
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if devnull > 2:
            os.close(devnull)
    except OSError:
        pass
    try:
        os.execvpe(argv[0], argv, env)
    except OSError:
        os._exit(127)
'''


def get_pending_relaunch() -> Optional[Path]:
    """Executable path to start after a successful in-place Windows update."""
    return _pending_relaunch_exe


def clear_pending_relaunch():
    global _pending_relaunch_exe
    _pending_relaunch_exe = None


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


def _env_for_external_helper() -> dict:
    """
    Environment for powershell / system python3 / /bin/sh helpers.

    A frozen onefile process points SSL, PYTHON*, LD_LIBRARY_PATH, and PATH at
    its `_MEI*` extract. Helpers must not inherit that or they crash, and the
    new app must not inherit `_PYI_*` or PyInstaller 6.9+ treats it as a worker
    of the dying extract (the window never comes back).
    """
    env = dict(os.environ)
    for key in list(env):
        if key in _RELAUNCH_DROP_ENV or key.startswith("_PYI_"):
            env.pop(key, None)
    orig_ld = os.environ.get("LD_LIBRARY_PATH_ORIG")
    if orig_ld is not None:
        env["LD_LIBRARY_PATH"] = orig_ld
    else:
        env.pop("LD_LIBRARY_PATH", None)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mei = os.path.normcase(os.path.abspath(str(meipass)))
        sep = os.sep
        for key in ("PATH", "DYLD_LIBRARY_PATH"):
            val = env.get(key)
            if not val:
                continue
            kept = []
            for part in val.split(os.pathsep):
                if not part:
                    continue
                try:
                    normalized = os.path.normcase(os.path.abspath(part))
                except OSError:
                    kept.append(part)
                    continue
                if normalized == mei or normalized.startswith(mei + sep):
                    continue
                kept.append(part)
            if kept:
                env[key] = os.pathsep.join(kept)
            else:
                env.pop(key, None)
    return env


def _env_for_app_relaunch() -> dict:
    """Env for the replacement HuaEPUB process (independent onefile instance)."""
    env = _env_for_external_helper()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _exe_basenames() -> Tuple[str, ...]:
    """Preferred then legacy executable basenames for this platform."""
    if sys.platform == 'win32':
        return (f"{EXE_BASENAME}.exe", f"{LEGACY_EXE_BASENAME}.exe")
    return (EXE_BASENAME, LEGACY_EXE_BASENAME)


def _allowed_exe_names() -> Set[str]:
    return set(_exe_basenames())


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
                'User-Agent': UPDATER_USER_AGENT,
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


def _create_replacement_helper(
    new_exe: Path,
    old_exe: Path,
    app_dir: Path,
    pid: int,
    sha256: Optional[str] = None,
) -> Path:
    """
    Write a small helper + JSON config (paths validated, never shell-interpolated
    from untrusted text), then return the helper path to launch.

    Windows: rename-swap with retries + re-hash of staged binary.
    POSIX: /bin/sh launcher that runs an embedded python3 replace (paths never
    assigned into shell variables for mv/exec). Falls back to a strict shell
    path only when python3 is missing.
    """
    config_path = app_dir / '_update_helper.json'
    if not sha256:
        sha256 = hashlib.sha256(Path(new_exe).read_bytes()).hexdigest()
    write_update_helper_config(
        config_path, new_exe=new_exe, old_exe=old_exe, pid=pid, sha256=sha256
    )

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
    $expected = ([string]$cfg.sha256).ToLowerInvariant()
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
    if ($expected.Length -eq 64) {
        $actual = (Get-FileHash -LiteralPath $newExe -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) {
            throw "Staged binary checksum mismatch (expected $expected, got $actual)"
        }
        Write-UpdateLog "Staged binary checksum OK"
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
    foreach ($key in @(
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "_MEIPASS2"
    )) {
        Remove-Item -LiteralPath "Env:$key" -ErrorAction SilentlyContinue
    }
    Get-ChildItem Env: -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -like "_PYI_*"
    } | ForEach-Object {
        Remove-Item -LiteralPath "Env:$($_.Name)" -ErrorAction SilentlyContinue
    }
    $env:PYINSTALLER_RESET_ENVIRONMENT = "1"
    if (Test-Path -LiteralPath $oldExe) {
        try { Unblock-File -LiteralPath $oldExe } catch {}
    }
    $work = Split-Path -Parent $oldExe
    Write-UpdateLog "Launching $oldExe"
    Start-Process -FilePath $oldExe -WorkingDirectory $work -ErrorAction Stop
    Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
} catch {
    Write-UpdateLog "FATAL: $($_.Exception.Message)"
}
'''
        script_path.write_text(script_content, encoding='utf-8')
        return script_path

    # POSIX (macOS + Linux): never launch via frozen sys.executable.
    # Prefer python3 for replace so paths never enter shell variables.
    script_path = app_dir / '_update_helper.sh'
    script_content = r'''#!/bin/sh
set +e
# Parent GUI is about to die — ignore SIGHUP so replace + relaunch still run.
trap '' HUP
DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
LOG="$DIR/_update_helper.log"
CFG="$DIR/_update_helper.json"
SELF="$0"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" 2>/dev/null
}

# Primary path: python3 owns wait / hash verify / replace / relaunch.
# Paths stay inside Python — never assigned to shell vars for mv/exec.
# If python3 is the Xcode stub or otherwise fails, fall through to the shell.
if command -v python3 >/dev/null 2>&1; then
  python3 - "$CFG" "$LOG" "$SELF" <<'PY'
import hashlib, json, os, signal, subprocess, sys, time
from pathlib import Path
try:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
except Exception:
    pass
''' + _SPAWN_DETACHED_PY + r'''
cfg_path, log_path, self_path = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])

def log(msg: str) -> None:
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass

try:
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    pid = int(cfg["pid"])
    new_exe = Path(cfg["new_exe"])
    old_exe = Path(cfg["old_exe"])
    expected = str(cfg.get("sha256") or "").strip().lower()
    backup = old_exe.parent / "_update_backup"
    app_dir = old_exe.parent.resolve()
    for p in (new_exe, old_exe):
        p.resolve().relative_to(app_dir)
    log(f"Waiting for PID {pid} to exit (new={new_exe} old={old_exe})")
    time.sleep(2)
    while True:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(1)
    time.sleep(2)
    if not new_exe.is_file():
        raise FileNotFoundError(f"New executable missing: {new_exe}")
    if expected:
        actual = hashlib.sha256(new_exe.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"Staged binary checksum mismatch ({actual} != {expected})")
        log("Staged binary checksum OK")
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
    mode = old_exe.stat().st_mode
    os.chmod(old_exe, mode | 0o111)
    try:
        backup.unlink()
    except OSError:
        pass
    try:
        cfg_path.unlink()
    except OSError:
        pass
    # Clear Gatekeeper quarantine when xattr exists (macOS)
    if sys.platform == "darwin":
        for args in (
            ["xattr", "-dr", "com.apple.quarantine", str(old_exe)],
            ["xattr", "-cr", str(old_exe)],
        ):
            try:
                import subprocess
                subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    log("Launching updated app")
    drop = {
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "_MEIPASS2",
    }
    clean = {
        k: v for k, v in os.environ.items()
        if k not in drop and not k.startswith("_PYI_")
    }
    orig_ld = os.environ.get("LD_LIBRARY_PATH_ORIG")
    if orig_ld is not None:
        clean["LD_LIBRARY_PATH"] = orig_ld
    else:
        clean.pop("LD_LIBRARY_PATH", None)
    clean["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    # Never /usr/bin/open on a bare Mach-O: Launch Services starts Terminal.app
    # and the GUI dies when that session is closed.
    spawn_detached([str(old_exe)], str(old_exe.parent), clean)
    log("Launched detached")
    try:
        self_path.unlink()
    except OSError:
        pass
except Exception:
    import traceback
    log("FATAL:\n" + traceback.format_exc())
    sys.exit(1)
PY
  py_status=$?
  if [ "$py_status" -eq 0 ]; then
    exit 0
  fi
  log "python3 helper failed ($py_status); trying shell fallback"
fi

# Fallback without python3: reject metacharacters, then quoted mv only.
log "using restricted shell fallback"
PID=$(sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$CFG" | head -1)
NEW_EXE=$(sed -n 's/.*"new_exe"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CFG" | head -1)
OLD_EXE=$(sed -n 's/.*"old_exe"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CFG" | head -1)
EXPECTED=$(sed -n 's/.*"sha256"[[:space:]]*:[[:space:]]*"\([0-9a-fA-F]*\)".*/\1/p' "$CFG" | head -1)
BACKUP="$DIR/_update_backup"

# Reject shell-metacharacters in paths before any use (python3 path already validated).
bad=0
for p in "$NEW_EXE" "$OLD_EXE"; do
  case "$p" in
    *\$*|*\`*|*\;*|*\|*|*\&*) bad=1 ;;
  esac
done
case "$NEW_EXE$OLD_EXE" in
  *"	"*) bad=1 ;;
esac
if [ "$bad" -ne 0 ] || [ -z "$NEW_EXE" ] || [ -z "$OLD_EXE" ] || [ -z "$PID" ]; then
  log "FATAL: invalid helper paths or pid"
  exit 1
fi

log "Waiting for PID $PID to exit (new=$NEW_EXE old=$OLD_EXE)"
sleep 2
while kill -0 "$PID" 2>/dev/null; do
  sleep 1
done
sleep 2

if [ ! -f "$NEW_EXE" ]; then
  log "FATAL: new executable missing: $NEW_EXE"
  exit 1
fi

# Best-effort re-hash when sha256sum/shasum exists
if [ -n "$EXPECTED" ]; then
  if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL=$(sha256sum "$NEW_EXE" | awk '{print tolower($1)}')
  elif command -v shasum >/dev/null 2>&1; then
    ACTUAL=$(shasum -a 256 "$NEW_EXE" | awk '{print tolower($1)}')
  else
    ACTUAL=""
  fi
  EXP_LC=$(printf '%s' "$EXPECTED" | tr 'A-F' 'a-f')
  if [ -n "$ACTUAL" ] && [ "$ACTUAL" != "$EXP_LC" ]; then
    log "FATAL: staged binary checksum mismatch"
    exit 1
  fi
fi

replaced=0
i=0
while [ "$i" -lt 90 ]; do
  i=$((i + 1))
  rm -f "$BACKUP"
  if [ -e "$OLD_EXE" ]; then
    if ! mv -f "$OLD_EXE" "$BACKUP" 2>>"$LOG"; then
      log "Attempt $i: could not move old aside"
      sleep 1
      continue
    fi
  fi
  if mv -f "$NEW_EXE" "$OLD_EXE" 2>>"$LOG"; then
    replaced=1
    log "Replace succeeded on attempt $i"
    break
  fi
  log "Attempt $i: could not place new exe"
  if [ ! -e "$OLD_EXE" ] && [ -e "$BACKUP" ]; then
    mv -f "$BACKUP" "$OLD_EXE" 2>>"$LOG"
  fi
  sleep 1
done

if [ "$replaced" -ne 1 ]; then
  log "FATAL: failed to replace executable after retries"
  exit 1
fi

chmod a+x "$OLD_EXE" 2>/dev/null
rm -f "$BACKUP" "$CFG"
if command -v xattr >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine "$OLD_EXE" 2>/dev/null
  xattr -cr "$OLD_EXE" 2>/dev/null
fi

log "Launching updated app"
# Drop PyInstaller IPC so the new onefile instance unpacks itself.
for _pyi_k in $(env | sed -n 's/^\(_PYI_[^=]*\)=.*/\1/p'); do
  unset "$_pyi_k"
done
if [ -n "${LD_LIBRARY_PATH_ORIG+x}" ]; then
  LD_LIBRARY_PATH="$LD_LIBRARY_PATH_ORIG"
  export LD_LIBRARY_PATH
else
  unset LD_LIBRARY_PATH
fi
export PYINSTALLER_RESET_ENVIRONMENT=1
# Bare executable — never `open` (macOS would start Terminal.app).
if command -v nohup >/dev/null 2>&1; then
  env \
    -u SSL_CERT_FILE -u REQUESTS_CA_BUNDLE -u CURL_CA_BUNDLE \
    -u PYTHONHOME -u PYTHONPATH -u PYTHONNOUSERSITE -u _MEIPASS2 \
    PYINSTALLER_RESET_ENVIRONMENT=1 \
    nohup "$OLD_EXE" >/dev/null 2>&1 &
else
  env \
    -u SSL_CERT_FILE -u REQUESTS_CA_BUNDLE -u CURL_CA_BUNDLE \
    -u PYTHONHOME -u PYTHONPATH -u PYTHONNOUSERSITE -u _MEIPASS2 \
    PYINSTALLER_RESET_ENVIRONMENT=1 \
    "$OLD_EXE" >/dev/null 2>&1 &
fi

rm -f "$SELF"
'''
    script_path.write_text(script_content, encoding='utf-8', newline='\n')
    os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC)
    return script_path


def _posix_helper_interpreter() -> str:
    """
    Shell used to run the POSIX update helper.

    Must never be the frozen app binary: on macOS/Linux, sys.executable is the
    GUI itself, so launching the helper with it opens a second old-version window.
    """
    frozen_exe: Optional[Path] = None
    if is_frozen():
        try:
            frozen_exe = Path(sys.executable).resolve()
        except OSError:
            frozen_exe = Path(sys.executable)

    for candidate in (
        "/bin/bash",
        "/usr/bin/bash",
        "/bin/sh",
        "/usr/bin/sh",
    ):
        path = Path(candidate)
        if not path.is_file():
            continue
        if frozen_exe is not None:
            try:
                if path.resolve() == frozen_exe:
                    continue
            except OSError:
                pass
        return candidate
    return "sh"


def _launch_replacement_script(script_path: Path):
    """Run the replacement helper so it outlives this process."""
    if sys.platform == 'win32':
        _win_start_ps1(script_path, cwd=str(script_path.parent))
        return

    # macOS + Linux: always a real shell — never sys.executable when frozen.
    # Pass a sanitized env so system python3 is not poisoned by _MEI paths.
    shell = _posix_helper_interpreter()
    if is_frozen():
        try:
            if Path(shell).resolve() == Path(sys.executable).resolve():
                raise RuntimeError(
                    "Refusing to launch update helper via frozen app binary "
                    f"({sys.executable}); would open a second GUI instead of applying the update."
                )
        except OSError:
            pass
    subprocess.Popen(
        [shell, str(script_path)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(script_path.parent),
        close_fds=True,
        env=_env_for_external_helper(),
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

    assets = list(release_data.get('assets', []))
    # Prefer HuaEPUB-* zips; fall back to any platform zip (legacy NovelDownloader-*).
    preferred = []
    fallback = []
    for asset in assets:
        name = (asset.get('name') or '').lower()
        if wanted in name and name.endswith('.zip') and 'source' not in name:
            if EXE_BASENAME.lower() in name:
                preferred.append(asset)
            else:
                fallback.append(asset)
    return (preferred or fallback or [None])[0]


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
        resp = safe_http_request(
            session,
            "GET",
            url,
            allow_http=False,
            resolve_dns=False,
            extra_check=validate_github_asset_host,
            timeout=30,
        )
        resp.raise_for_status()
        for line in resp.text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].lstrip('*') == asset_name:
                return parts[0].lower()
    except UnsafeURLError:
        return None
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
    try:
        response = safe_http_request(
            session,
            "GET",
            url,
            allow_http=False,
            resolve_dns=False,
            extra_check=validate_github_asset_host,
            timeout=300,
        )
        response.raise_for_status()
        return response.content, name
    except UnsafeURLError as e:
        return None, f"Update download blocked: {e}"


def download_update(
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Tuple[bool, str]:
    """
    Download the latest version from GitHub and install it.

    Frozen builds: platform zip + required SHA256 verification.
    Source installs: HuaEPUB-source.zip (or legacy novelDownloader-source.zip)
    + required SHA256 verification. Never falls back to an unsigned main-branch zip.
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
            session.headers.update({'User-Agent': UPDATER_USER_AGENT})

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

        source_asset = (
            _find_asset(release_data, SOURCE_ASSET_NAME)
            or _find_asset(release_data, LEGACY_SOURCE_ASSET_NAME)
        )
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

    old_exe = get_executable_path()
    if not old_exe:
        return (False, "Could not determine current executable path.")

    if progress_callback:
        progress_callback(80, 100, "Extracting update...")

    # Stage next to the running binary using its current filename so legacy
    # NovelDownloader.exe installs still replace in place.
    temp_new_exe = app_dir / f'_new_{old_exe.name}'
    allowed = _allowed_exe_names()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        zip_path = temp_path / (asset_name or 'update.zip')
        zip_path.write_bytes(data)

        safe_extract_zip(zip_path, temp_path, allowed_names=allowed)

        new_exe = None
        for preferred in _exe_basenames():
            for candidate in temp_path.rglob(preferred):
                if candidate.is_file():
                    new_exe = candidate
                    break
            if new_exe is not None:
                break
        if new_exe is None:
            names = ", ".join(_exe_basenames())
            return (False, f"Executable ({names}) not found inside {asset_name}")

        shutil.copy2(new_exe, temp_new_exe)

    # Owner-only staged binary; re-hashed by the helper immediately before replace.
    try:
        if sys.platform == 'win32':
            os.chmod(temp_new_exe, stat.S_IRUSR | stat.S_IWUSR)
        else:
            os.chmod(temp_new_exe, stat.S_IRWXU)  # 0700 — owner rwx for later exec
    except OSError:
        pass
    staged_sha = hashlib.sha256(temp_new_exe.read_bytes()).hexdigest()

    if progress_callback:
        progress_callback(90, 100, "Installing update...")

    if sys.platform == 'win32':
        return _finalize_frozen_update_windows(
            temp_new_exe, old_exe, progress_callback, sha256=staged_sha
        )

    # POSIX: replace after this process exits
    script_path = _create_replacement_helper(
        temp_new_exe, old_exe, app_dir, os.getpid(), sha256=staged_sha
    )
    _launch_replacement_script(script_path)

    if progress_callback:
        progress_callback(100, 100, "Update ready!")

    return (True,
        "Update downloaded and verified!\n\n"
        "The application will now close to apply the update,\n"
        "then reopen automatically."
    )


def _cleanup_update_sidecars(app_dir: Path):
    for name in (
        "_update_helper.ps1",
        "_update_helper.py",
        "_update_helper.sh",
        "_update_helper.json",
        "_update_helper.log",
        "_update_relaunch.ps1",
        "_update_relaunch.json",
        "_update_relaunch.py",
        "_update_relaunch.sh",
    ):
        try:
            (app_dir / name).unlink(missing_ok=True)
        except OSError:
            pass


def _swap_running_exe_windows(new_exe: Path, old_exe: Path) -> Path:
    """
    Replace a running Windows executable in place.

    Renaming the running image is allowed on Windows; deleting it is not.
    Previous helpers that waited until after exit were often never started
    (child process torn down with the GUI), which left `_new_*.exe` behind.
    """
    app_dir = old_exe.parent
    backup = app_dir / "_update_backup.exe"
    last_err: Optional[BaseException] = None

    for _ in range(40):
        try:
            if backup.exists():
                backup.unlink()
            # Move the running binary aside, then put the new one in its place.
            os.replace(str(old_exe), str(backup))
            os.replace(str(new_exe), str(old_exe))
            return backup
        except OSError as e:
            last_err = e
            # Roll back if we moved old aside but failed to place new.
            if not old_exe.exists() and backup.exists() and not new_exe.exists():
                # new already consumed somehow — don't clobber
                pass
            elif not old_exe.exists() and backup.exists():
                try:
                    os.replace(str(backup), str(old_exe))
                except OSError:
                    pass
            time.sleep(0.25)

    raise OSError(f"Could not replace running executable: {last_err}")


def _windows_powershell() -> str:
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    for candidate in (
        os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        os.path.join(system_root, "SysWOW64", "WindowsPowerShell", "v1.0", "powershell.exe"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"


def _win_creation_flags(*, breakaway: bool) -> int:
    flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", _CREATE_NEW_PROCESS_GROUP)
    )
    if breakaway:
        flags |= getattr(
            subprocess, "CREATE_BREAKAWAY_FROM_JOB", _CREATE_BREAKAWAY_FROM_JOB
        )
    return flags


def _win_shell_execute(file: str, params: str, cwd: str) -> int:
    """
    ShellExecuteW. Return value > 32 means success.

    The new process is not a child of this one, so it survives PyInstaller
    onefile teardown (the bootloader kills Popen children when the GUI exits).
    """
    import ctypes
    return int(ctypes.windll.shell32.ShellExecuteW(
        None, "open", file, params, cwd, _SW_HIDE
    ))


def _win_start_ps1(script_path: Path, *, cwd: Optional[str] = None):
    """
    Launch a PowerShell script so it outlives this GUI.

    Prefer ShellExecute (not our child). Fall back to `cmd /c start ""`
    with DETACHED_PROCESS. Last resort is a direct Popen, which a onefile
    parent may still kill on exit.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        except Exception:
            pass
    script = str(Path(script_path).resolve())
    cwd = cwd or str(Path(script_path).parent)
    ps = _windows_powershell()
    params = (
        "-NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f'-WindowStyle Hidden -File "{script}"'
    )
    helper_env = _env_for_external_helper()
    try:
        ret = _win_shell_execute(ps, params, cwd)
        if ret > 32:
            print(f"Update helper started via ShellExecute ({ret})")
            return None
        print(f"ShellExecute helper returned {ret}; trying cmd start")
    except Exception as e:
        print(f"ShellExecute helper failed ({e}); trying cmd start")

    comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
    # One /c string: `start` treats the first quoted token as a window title.
    start_line = (
        f'start "" /min "{ps}" -NoProfile -NonInteractive '
        f'-ExecutionPolicy Bypass -WindowStyle Hidden -File "{script}"'
    )
    detached_flags = (
        getattr(subprocess, "DETACHED_PROCESS", _DETACHED_PROCESS)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", _CREATE_NEW_PROCESS_GROUP)
        | getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)
    )
    try:
        return subprocess.Popen(
            [comspec, "/c", start_line],
            shell=False,
            creationflags=detached_flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            close_fds=True,
            env=helper_env,
        )
    except OSError as e:
        print(f"cmd start helper failed ({e}); last-resort Popen")

    args = [
        ps,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", script,
    ]
    last_err: Optional[BaseException] = None
    for breakaway in (True, False):
        try:
            return subprocess.Popen(
                args,
                shell=False,
                creationflags=_win_creation_flags(breakaway=breakaway),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                close_fds=True,
                env=helper_env,
            )
        except OSError as err:
            last_err = err
    raise OSError(f"Could not start update helper: {last_err}")


def _create_post_swap_relaunch_helper(
    exe_path: Path,
    backup_path: Optional[Path],
    app_dir: Path,
    pid: int,
    extra_args: Optional[list] = None,
    cwd: Optional[Path] = None,
) -> Path:
    """
    Wait for this PID to exit, then start exe_path (optional extra_args).
    Used after an in-process Windows exe swap and for source-install relaunch.
    """
    config_path = app_dir / '_update_relaunch.json'
    payload = {
        "pid": int(pid),
        "exe": str(Path(exe_path)),
        "backup": str(Path(backup_path)) if backup_path else "",
        "cwd": str(Path(cwd) if cwd is not None else Path(exe_path).parent),
        "args": [str(a) for a in (extra_args or [])],
    }
    write_secret_file(config_path, json.dumps(payload))
    script_path = app_dir / '_update_relaunch.ps1'
    script_content = r'''$ErrorActionPreference = "Continue"
$logPath = Join-Path $PSScriptRoot "_update_helper.log"
function Write-UpdateLog([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}
try {
    $cfgPath = Join-Path $PSScriptRoot "_update_relaunch.json"
    $cfg = Get-Content -Raw -Encoding UTF8 $cfgPath | ConvertFrom-Json
    $pidWait = [int]$cfg.pid
    $exe = [string]$cfg.exe
    $backup = [string]$cfg.backup
    $workdir = [string]$cfg.cwd
    $launchArgs = @()
    if ($null -ne $cfg.args) { $launchArgs = @($cfg.args) }
    Write-UpdateLog "Relaunch helper waiting for PID $pidWait"
    Start-Sleep -Seconds 1
    while (Get-Process -Id $pidWait -ErrorAction SilentlyContinue) {
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Seconds 2
    if ($backup -and (Test-Path -LiteralPath $backup)) {
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
    }
    foreach ($key in @(
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "_MEIPASS2"
    )) {
        Remove-Item -LiteralPath "Env:$key" -ErrorAction SilentlyContinue
    }
    Get-ChildItem Env: -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -like "_PYI_*"
    } | ForEach-Object {
        Remove-Item -LiteralPath "Env:$($_.Name)" -ErrorAction SilentlyContinue
    }
    $env:PYINSTALLER_RESET_ENVIRONMENT = "1"
    if (-not $workdir) { $workdir = Split-Path -Parent $exe }
    Write-UpdateLog "Launching $exe"
    if (-not (Test-Path -LiteralPath $exe)) {
        throw "exe missing: $exe"
    }
    try { Unblock-File -LiteralPath $exe } catch {}
    if ($launchArgs.Count -gt 0) {
        Start-Process -FilePath $exe -ArgumentList $launchArgs -WorkingDirectory $workdir -ErrorAction Stop
    } else {
        Start-Process -FilePath $exe -WorkingDirectory $workdir -ErrorAction Stop
    }
    Write-UpdateLog "Start-Process OK"
    Remove-Item -LiteralPath $cfgPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
} catch {
    Write-UpdateLog "FATAL: $($_.Exception.Message)"
}
'''
    script_path.write_text(script_content, encoding='utf-8')
    return script_path


def _finalize_frozen_update_windows(
    new_exe: Path,
    old_exe: Path,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    sha256: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Swap on-disk exe while still running, then schedule a hidden post-exit
    helper to delete the backup and relaunch — never relaunch from this process.
    """
    clear_pending_relaunch()
    try:
        backup = _swap_running_exe_windows(new_exe, old_exe)
    except OSError as e:
        # Fall back to post-exit helper if in-process swap is blocked.
        print(f"In-process swap failed ({e}); scheduling post-exit helper")
        script_path = _create_replacement_helper(
            new_exe, old_exe, old_exe.parent, os.getpid(), sha256=sha256
        )
        _launch_replacement_script(script_path)
        if progress_callback:
            progress_callback(100, 100, "Update ready!")
        return (True,
            "Update downloaded and verified!\n\n"
            "The application will now close to apply the update,\n"
            "then reopen automatically."
        )

    _cleanup_update_sidecars(old_exe.parent)

    # Hidden PowerShell: wait for us to exit → delete backup → start new exe.
    # Do NOT Start-Process from the still-running GUI (causes pythonXX.dll errors
    # with PyInstaller one-file, and cmd's `timeout` flashes a console).
    try:
        script = _create_post_swap_relaunch_helper(
            old_exe, backup, old_exe.parent, os.getpid()
        )
        _win_start_ps1(script, cwd=str(old_exe.parent))
    except Exception as e:
        print(f"Failed to schedule relaunch helper: {e}")
        # Still leave the swapped exe in place; user can open it manually.
        try:
            if backup.exists():
                backup.unlink()
        except OSError:
            pass

    if progress_callback:
        progress_callback(100, 100, "Update ready!")

    return (True,
        "Update installed!\n\n"
        "The application will close and reopen on the new version."
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

    items_to_update = list(SOURCE_UPDATE_ITEMS)

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

    try:
        _schedule_relaunch_after_exit()
    except Exception as e:
        print(f"Failed to schedule relaunch: {e}")
        # Fallback only — the files are already replaced; the helper did not start.
        return (True, "Update installed successfully!\nPlease restart the application.")
    return (True,
        "Update installed!\n\n"
        "The application will now close and reopen."
    )


_SOURCE_RELAUNCH_PY = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path

try:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
except Exception:
    pass
''' + _SPAWN_DETACHED_PY + r'''

cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
pid = int(cfg["pid"])
argv = [cfg["exe"]] + list(cfg.get("args") or [])
cwd = cfg.get("cwd") or None
log_path = Path(sys.argv[1]).parent / "_update_helper.log"


def log(msg: str) -> None:
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def pid_alive(target: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, target)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(target, 0)
        return True
    except OSError:
        return False


log("Source relaunch waiting for PID %s" % pid)
time.sleep(1)
deadline = time.monotonic() + 120
while time.monotonic() < deadline and pid_alive(pid):
    time.sleep(0.3)
time.sleep(1)
log("Launching %s" % argv)
env = os.environ.copy()
drop = {
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "_MEIPASS2",
}
for k in list(env):
    if k in drop or k.startswith("_PYI_"):
        env.pop(k, None)
orig_ld = os.environ.get("LD_LIBRARY_PATH_ORIG")
if orig_ld is not None:
    env["LD_LIBRARY_PATH"] = orig_ld
else:
    env.pop("LD_LIBRARY_PATH", None)
env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
spawn_detached(argv, cwd, env)
try:
    Path(sys.argv[1]).unlink()
except OSError:
    pass
try:
    Path(__file__).unlink()
except OSError:
    pass
'''


def _schedule_relaunch_after_exit() -> None:
    """Wait for this PID to exit, then start the current app (source installs)."""
    app_dir = get_app_dir()
    pid = os.getpid()
    if is_frozen():
        launch_exe = get_executable_path()
        extra: list = []
        cwd = launch_exe.parent if launch_exe else app_dir
    else:
        launch_exe = Path(sys.executable)
        app_py = app_dir / "app.py"
        extra = [str(app_py)] if app_py.is_file() else [str(a) for a in sys.argv]
        cwd = app_dir
        if sys.platform == "win32" and launch_exe.name.lower() == "python.exe":
            pythonw = launch_exe.with_name("pythonw.exe")
            if pythonw.is_file():
                launch_exe = pythonw
    if launch_exe is None:
        raise RuntimeError("Could not determine path to relaunch")

    if sys.platform == "win32":
        script = _create_post_swap_relaunch_helper(
            launch_exe, None, app_dir, pid, extra_args=extra, cwd=cwd
        )
        _win_start_ps1(script, cwd=str(app_dir))
        return

    cfg = app_dir / "_update_relaunch.json"
    write_secret_file(cfg, json.dumps({
        "pid": int(pid),
        "exe": str(launch_exe),
        "args": extra,
        "cwd": str(cwd),
    }))
    helper_py = app_dir / "_update_relaunch.py"
    helper_py.write_text(_SOURCE_RELAUNCH_PY, encoding="utf-8", newline="\n")
    subprocess.Popen(
        [sys.executable, str(helper_py), str(cfg)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(app_dir),
        close_fds=True,
        env=_env_for_external_helper(),
    )


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
