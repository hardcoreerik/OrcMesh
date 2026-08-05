"""MeshChat – services: application logging setup."""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import platformdirs

APP_NAME = "MeshChat"
_configured = False


def configure_logging(debug: bool = False) -> None:
    """Set up rotating file log + stderr handler."""
    global _configured
    if _configured:
        return
    _configured = True

    log_dir = Path(platformdirs.user_log_dir(APP_NAME, appauthor=False))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "meshchat.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    # Rotating file handler (5 MB × 3 files)
    fh = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root.addHandler(fh)

    # Console handler
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING if not debug else logging.DEBUG)
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(sh)

    log = logging.getLogger(__name__)
    log.info("MeshChat logging started. Log file: %s", log_file)


def get_log_dir() -> Path:
    return Path(platformdirs.user_log_dir(APP_NAME, appauthor=False))
