"""Logging: console + rotating file, a run id on every line, and a collector
that remembers WARNING+ messages so they can be written to the Run Info sheet."""
from __future__ import annotations

import logging
import sys
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

LOGGER_NAME = "cm_power_merge"


class WarningCollector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.levelname, record.getMessage()))


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


def setup_logging(log_dir: Optional[Path], run_id: Optional[str] = None,
                  level: int = logging.INFO, console: bool = True):
    """Configure the package logger. Returns (run_id, WarningCollector)."""
    run_id = run_id or uuid.uuid4().hex[:8]
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    shutdown_logging()                        # idempotent: no duplicate lines, no leaked file handles

    fmt = logging.Formatter("%(asctime)s | %(run_id)s | %(levelname)-7s | %(name)s | %(message)s")
    flt = _RunIdFilter(run_id)
    handlers: list[logging.Handler] = []
    if console:
        handlers.append(logging.StreamHandler(sys.stdout))
    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_dir / "cm_power_merge.log",
                                            maxBytes=10_000_000, backupCount=12,
                                            encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(flt)
        logger.addHandler(h)

    collector = WarningCollector()
    collector.addFilter(flt)
    logger.addHandler(collector)
    return run_id, collector


def shutdown_logging() -> None:
    """Detach and close every handler so the log file is released (Windows cannot delete open files)."""
    logger = logging.getLogger(LOGGER_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
