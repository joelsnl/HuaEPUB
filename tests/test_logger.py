"""Rotating file logging (no Qt, no process-wide stdio capture)."""

from core.logger import (
    LOGGER_NAME,
    MAX_LOG_BYTES,
    get_logger,
    log_path,
    setup_logging,
)


def _close_handlers():
    logger = get_logger()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass


def test_setup_logging_writes_timestamped_records(tmp_path):
    path = setup_logging(tmp_path, install_hooks=False)
    try:
        assert path == log_path(tmp_path)
        logger = get_logger()
        logger.info("phase3-log-line")
        for handler in logger.handlers:
            handler.flush()
        text = path.read_text(encoding="utf-8")
        assert "phase3-log-line" in text
        assert "[INFO]" in text
        assert LOGGER_NAME == "huaepub"
        assert "[MainThread]" in text or "Thread" in text
    finally:
        _close_handlers()


def test_rotates_when_log_exceeds_cap(tmp_path):
    path = setup_logging(tmp_path, install_hooks=False)
    try:
        logger = get_logger()
        chunk = "x" * 80_000
        for _ in range(20):
            logger.info(chunk)
        for handler in logger.handlers:
            handler.flush()
        backup = path.with_name(path.name + ".1")
        assert backup.is_file()
        assert path.stat().st_size <= MAX_LOG_BYTES + 100_000
    finally:
        _close_handlers()
