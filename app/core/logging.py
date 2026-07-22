import logging
import sys

from app.core.config import settings


def configure_logging() -> None:
    """Every module in this codebase logs through logging.getLogger("kontributa.xxx")
    but nothing was ever configuring a handler for it -- INFO-level records
    (most of what's actually logged: webhook processing, reconciliation
    runs, notification sends) were silently dropped by Python's default
    "handler of last resort", which only shows WARNING and above with no
    formatting. force=True guarantees this wins regardless of whether
    anything else (e.g. uvicorn) touched the root logger first."""
    logging.basicConfig(
        level=settings.LOG_LEVEL,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
