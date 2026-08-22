# Author: joelsnl and Anthropic Claude
"""
File logging for the app.

The codebase reports progress and errors with print(); in the packaged
(windowed) executable there is no console, so failures were invisible.
This module configures a rotating file log at ~/.huaepub/logs/huaepub.log
(1 MB, keep one previous file) and, outside pytest, tees stdout/stderr
into that logger. File → Open log file in the GUI.

Rotation happens during a long session (RotatingFileHandler), not only
at startup. Uncaught exceptions and faulthandler dumps go next to the
same logs folder.
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

from core.branding import FAULT_LOG_FILE_NAME, LOG_FILE_NAME

MAX_LOG_BYTES = 1024 * 1024  # rotate at 1 MB, keep one previous file
BACKUP_COUNT = 1
LOGGER_NAME = "huaepub"
_LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured_stdio = False
_fault_fp: TextIO | None = None
_prev_excepthook = None
_prev_thread_excepthook = None


class _StreamToLogger:
    """Write to the original stream and emit complete lines as log records."""

    def __init__(self, logger: logging.Logger, level: int, fallback):
        self.logger = logger
        self.level = level
        self.fallback = fallback
        self._buf = ""
        self._writing = False

    def write(self, data):
        if not isinstance(data, str):
            data = str(data)
        if self.fallback is not None:
            try:
                self.fallback.write(data)
            except Exception:
                pass
        if self._writing:
            return len(data)
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._writing = True
                try:
                    self.logger.log(self.level, line)
                except Exception:
                    pass
                finally:
                    self._writing = False
        return len(data)

    def flush(self):
        if self.fallback is not None:
            try:
                self.fallback.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_path(data_dir: Path) -> Path:
    return Path(data_dir) / "logs" / LOG_FILE_NAME


def fault_log_path(data_dir: Path) -> Path:
    return Path(data_dir) / "logs" / FAULT_LOG_FILE_NAME


def _replace_file_handler(logger: logging.Logger, path: Path) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
        delay=False,
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False


def _install_excepthooks(logger: logging.Logger) -> None:
    global _prev_excepthook, _prev_thread_excepthook
    if _prev_excepthook is None:
        _prev_excepthook = sys.excepthook

    def hook(exc_type, exc, tb):
        try:
            logger.error("Uncaught exception", exc_info=(exc_type, exc, tb))
        except Exception:
            pass
        prev = _prev_excepthook
        if prev is not None and prev is not hook:
            prev(exc_type, exc, tb)

    sys.excepthook = hook

    if hasattr(threading, "excepthook"):
        if _prev_thread_excepthook is None:
            _prev_thread_excepthook = threading.excepthook

        def thread_hook(args):
            try:
                logger.error(
                    "Uncaught thread exception",
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                )
            except Exception:
                pass
            prev = _prev_thread_excepthook
            if prev is not None and prev is not thread_hook:
                try:
                    prev(args)
                except Exception:
                    pass

        threading.excepthook = thread_hook


def _enable_faulthandler(data_dir: Path) -> None:
    global _fault_fp
    if _fault_fp is not None:
        return
    path = fault_log_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _fault_fp = open(path, "a", encoding="utf-8", errors="replace")
        _fault_fp.write(
            f"\n===== faulthandler enabled {time.strftime(_DATE_FORMAT)} =====\n"
        )
        _fault_fp.flush()
        faulthandler.enable(file=_fault_fp, all_threads=True)
    except Exception:
        _fault_fp = None


def setup_logging(data_dir: Path, *, install_hooks: bool | None = None) -> Path:
    """
    Configure rotating file logging under data_dir/logs/.

    install_hooks: tee stdout/stderr, sys.excepthook, and faulthandler.
    Default is on unless pytest is already imported (so the test suite
    does not capture the process streams).
    Returns the log file path. Never raises.
    """
    global _configured_stdio
    path = log_path(data_dir)
    if install_hooks is None:
        install_hooks = "pytest" not in sys.modules
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        logger = get_logger()
        _replace_file_handler(logger, path)
        logger.info("===== Session started %s =====", time.strftime(_DATE_FORMAT))
        if install_hooks and not _configured_stdio:
            sys.stdout = _StreamToLogger(logger, logging.INFO, sys.stdout)
            sys.stderr = _StreamToLogger(logger, logging.ERROR, sys.stderr)
            _install_excepthooks(logger)
            _enable_faulthandler(Path(data_dir))
            _configured_stdio = True
    except Exception:
        pass
    return path
