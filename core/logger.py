# Author: joelsnl and Anthropic Claude
"""
File logging for the app.

The codebase reports progress and errors with print(); in the packaged
(windowed) executable there is no console, so failures were invisible.
This module tees stdout/stderr to ~/.huaepub/logs/huaepub.log (1 MB
startup rotate, keep .log.1). File → Open log file in the GUI.
"""

import sys
import time
from pathlib import Path

from core.branding import LOG_FILE_NAME

MAX_LOG_BYTES = 1024 * 1024  # rotate at 1 MB, keep one previous file


class _Tee:
    """Write to a log file and (if present) the original stream."""

    def __init__(self, stream, logfile):
        self.stream = stream
        self.logfile = logfile

    def write(self, data):
        try:
            self.logfile.write(data)
        except Exception:
            pass
        if self.stream is not None:
            try:
                self.stream.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self):
        try:
            self.logfile.flush()
        except Exception:
            pass
        if self.stream is not None:
            try:
                self.stream.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def setup_logging(data_dir: Path) -> Path:
    """
    Tee stdout/stderr to data_dir/logs/huaepub.log.
    Returns the log file path. Never raises.
    """
    log_path = data_dir / 'logs' / LOG_FILE_NAME
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Simple rotation: keep one previous log
        if log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES:
            backup = log_path.with_suffix('.log.1')
            if backup.exists():
                backup.unlink()
            log_path.rename(backup)

        logfile = open(log_path, 'a', encoding='utf-8', errors='replace', buffering=1)
        logfile.write(f"\n===== Session started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")

        sys.stdout = _Tee(sys.stdout, logfile)
        sys.stderr = _Tee(sys.stderr, logfile)
    except Exception:
        pass
    return log_path
