"""Central logging setup: rotating file log + optional console echo.

The telemetry client logs full JSON payloads here when the endpoint is
unreachable, so the log is the source of truth for the payload schema.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import LOG_FILE, ensure_dirs

_configured = False


def get_logger(console: bool = False, level: int = logging.INFO) -> logging.Logger:
    """Return the shared 'warden' logger, configuring handlers once."""
    global _configured
    logger = logging.getLogger("warden")
    if _configured:
        return logger

    ensure_dirs()
    logger.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    logger.propagate = False
    _configured = True
    return logger
