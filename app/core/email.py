import logging

logger = logging.getLogger("kontributa.email")


async def send_email(to: str, subject: str, body: str) -> None:
    """Stub email sender. Replaced by a real SendByte integration in Phase 7."""
    logger.info("EMAIL to=%s subject=%s body=%s", to, subject, body)
