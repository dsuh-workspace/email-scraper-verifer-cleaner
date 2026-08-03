"""
Central logging setup for the pipeline.

Usage in a module:

    import logging
    from app.logging_config import setup_logging
    logger = logging.getLogger(__name__)
    logger.info("something happened")

Level is controlled by the LOG_LEVEL env var (default INFO). Set to
DEBUG for verbose runs. Format is human-readable to stderr; add a
FileHandler here if we ever want persistent logs.
"""

import logging
import os
import sys


def setup_logging(level: str | None = None) -> None:
    """Idempotent root-logger configuration. Call at process start."""
    resolved_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    numeric_level = getattr(logging, resolved_level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Wipe any handlers Python may have attached automatically (e.g. from
    # a subprocess parent) so we don't get duplicated lines.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(handler)

    # Silence noisy 3rd-party loggers at INFO.
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
