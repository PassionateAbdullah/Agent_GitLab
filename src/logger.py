from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_initialized = False


def setup_logging(level: str = "INFO", file: str | None = None) -> logging.Logger:
    global _initialized
    logger = logging.getLogger("readme_agent")
    if _initialized:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if file:
        log_path = Path(file)
        if not log_path.is_absolute():
            log_path = Path(__file__).resolve().parent.parent / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.propagate = False
    _initialized = True
    return logger
