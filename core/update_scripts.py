# Author: joelsnl and Anthropic Claude
"""Helper scripts the updater writes next to the app and runs after it exits.

They are plain text kept apart from core/updater.py so that file only holds update logic.
Nothing here is formatted with values: paths and the expected hash reach the scripts
through a JSON file (see core.security.write_update_helper_config), never through
string interpolation.
"""

# Inlined into the POSIX helpers (the system python3 cannot import this module).
# Double-fork + exec so the GUI is not a child of a shell/python session.
SPAWN_DETACHED_PY = r'''
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

# Windows helper: waits for the app to exit, swaps the staged exe in with retries, relaunches.
WINDOWS_REPLACE_HELPER_PS1 = r'''$ErrorActionPreference = "Continue"
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

# macOS / Linux helper: /bin/sh launcher around an embedded python3 replace, with a strict
# shell fallback when python3 is missing.
POSIX_REPLACE_HELPER_SH = r'''#!/bin/sh
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
''' + SPAWN_DETACHED_PY + r'''
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

# Windows helper that runs after the running exe was already swapped: waits, tidies up, relaunches.
WINDOWS_RELAUNCH_HELPER_PS1 = r'''$ErrorActionPreference = "Continue"
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

# Relaunches a source install (python app.py) after an update, detached from this process.
SOURCE_RELAUNCH_PY = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path

try:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
except Exception:
    pass
''' + SPAWN_DETACHED_PY + r'''

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
