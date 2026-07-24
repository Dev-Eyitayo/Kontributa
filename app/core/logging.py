import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from app.core.config import settings

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "kontributa.log")
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5


def configure_logging() -> None:
    """Every module in this codebase logs through logging.getLogger("kontributa.xxx")
    but nothing was ever configuring a handler for it -- INFO-level records
    (most of what's actually logged: webhook processing, reconciliation
    runs, notification sends) were silently dropped by Python's default
    "handler of last resort", which only shows WARNING and above with no
    formatting. force=True guarantees this wins regardless of whether
    anything else (e.g. uvicorn) touched the root logger first.

    In development, records also go to a size-rotated file under logs/ so a
    stdout that's scrolled past (or a terminal that got closed) isn't the
    only copy -- stdout stays the primary sink everywhere else."""
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [stream_handler]

    if settings.ENV == "development":
        os.makedirs(LOG_DIR, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUP_COUNT
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    logging.basicConfig(level=settings.LOG_LEVEL, handlers=handlers, force=True)
