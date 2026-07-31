import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.auth.models import User
from app.modules.contributions.models import Contribution, ContributionStatus
from app.modules.group_admins.models import GroupAdmin
from app.modules.members.models import Member
from app.modules.notifications.models import NotificationLog, NotificationStatus
from app.modules.purses.models import Purse

logger = logging.getLogger("kontributa.notifications")


def format_datetime(value: Any) -> str:
    """Format a datetime, date, or ISO date string into a human-readable string:
    'Jul 27, 2026 at 6:25 PM' for datetimes, or 'Jul 27, 2026' for dates.
    """
    if not value:
        return ""

    dt: datetime | None = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return value.strftime("%b %d, %Y")
    elif isinstance(value, str):
        val_str = value.strip()
        if not val_str:
            return ""
        try:
            dt = datetime.fromisoformat(val_str.replace("Z", "+00:00"))
        except ValueError:
            try:
                d = date.fromisoformat(val_str)
                return d.strftime("%b %d, %Y")
            except ValueError:
                return val_str

    if dt is not None:
        time_str = dt.strftime("%I:%M %p").lstrip("0")
        date_str = dt.strftime("%b %d, %Y")
        return f"{date_str} at {time_str}"

    return str(value)


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR), autoescape=select_autoescape(["html"])
)
_jinja_env.filters["format_datetime"] = format_datetime
_jinja_env.filters["format_date"] = format_datetime
_jinja_env.filters["datetime"] = format_datetime
_jinja_env.filters["date"] = format_datetime


def render_template(template_name: str, context: dict) -> str:
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    branding = {
        "app_url": base,
        "logo_url": f"{base}/logo/lockup-light.svg",
        "icon_url": f"{base}/logo/icon-light.svg",
    }
    template = _jinja_env.get_template(template_name)
    return template.render(**{**branding, **context})


class EmailServiceError(AppException):
    status_code = 502
    code = "email_service_error"


class EmailClient:
    def __init__(self, base_url: str, api_key: str, from_email: str, from_name: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._from_email = from_email
        self._from_name = from_name

    async def send(self, to_email: str, to_name: str, subject: str, html: str) -> str:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=15) as http:
            sender = f"{self._from_name} <{self._from_email}>" if self._from_name else self._from_email
            recipient = f"{to_name} <{to_email}>" if to_name else to_email
            resp = await http.post(
                "/v1/emails",
                json={
                    "from": sender,
                    "to": recipient,
                    "subject": subject,
                    "html": html,
                },
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        if resp.status_code >= 400:
            raise EmailServiceError(f"Email API error: HTTP {resp.status_code}: {resp.text}")
        return resp.json().get("id", "")


email_client = EmailClient(
    base_url=settings.EMAIL_BASE_URL,
    api_key=settings.EMAIL_API_KEY,
    from_email=settings.EMAIL_FROM_EMAIL,
    from_name=settings.EMAIL_FROM_NAME,
)

# Backward-compatible alias
sendbyte_client = email_client


def get_email_client() -> EmailClient:
    return email_client


def get_sendbyte_client() -> EmailClient:
    return get_email_client()


class NotificationService:
    """The one place that sends transactional email. Deliberately never
    raises: any failure (template error, network error, SendByte 4xx/5xx,
    even a failure to write the log row) is caught and logged here, so a
    notification problem can never block or roll back the business state
    change that triggered it. Every attempt, success or failure, gets a
    NotificationLog row -- an operational record for debugging delivery,
    not an AuditLog entry."""

    def __init__(self, db: AsyncSession, client: EmailClient):
        self.db = db
        self.client = client

    async def send(
        self,
        to_email: str,
        to_name: str,
        template_name: str,
        subject: str,
        context: dict,
    ) -> None:
        try:
            html = render_template(template_name, context)
            message_id = await self.client.send(to_email, to_name, subject, html)
            log = NotificationLog(
                to_email=to_email,
                template_name=template_name,
                status=NotificationStatus.SENT,
                provider_message_id=message_id,
            )
            logger.info("email sent to=%s template=%s id=%s", to_email, template_name, message_id)
        except Exception as exc:
            log = NotificationLog(
                to_email=to_email,
                template_name=template_name,
                status=NotificationStatus.FAILED,
                error=str(exc)[:2000],
            )
            logger.warning("email send FAILED to=%s template=%s error=%s", to_email, template_name, exc)

        try:
            self.db.add(log)
            await self.db.commit()
        except Exception:
            logger.exception(
                "failed to persist notification log for to=%s template=%s", to_email, template_name
            )


async def send_purse_reminders(
    purse_id: UUID, session_factory: async_sessionmaker, email_client: EmailClient
) -> None:
    """Runs as a background task after POST /purses/{id}/remind responds --
    a purse can have many still-pending members, so the batch send happens
    off the request path, the same way webhook/payout processing does.
    The weekly-cooldown gate and kill switch are already enforced by the
    router before this is even scheduled; this only sends."""
    async with session_factory() as db:
        notifications = NotificationService(db, email_client)
        purse = await db.get(Purse, purse_id)
        if purse is None:
            return

        # Two queries, not one join -- a contribution belongs to either a
        # Member or a GroupAdmin, never both (see
        # Contribution.ck_contribution_exactly_one_owner). An admin's own
        # pending contribution to their group's purse is just another row
        # here, same as any member's, so it gets the exact same reminder.
        member_stmt = (
            select(Contribution, User)
            .join(Member, Contribution.member_id == Member.id)
            .join(User, Member.user_id == User.id)
            .where(Contribution.purse_id == purse_id, Contribution.status == ContributionStatus.PENDING)
        )
        admin_stmt = (
            select(Contribution, User)
            .join(GroupAdmin, Contribution.group_admin_id == GroupAdmin.id)
            .join(User, GroupAdmin.user_id == User.id)
            .where(Contribution.purse_id == purse_id, Contribution.status == ContributionStatus.PENDING)
        )
        member_rows = (await db.execute(member_stmt)).all()
        admin_rows = (await db.execute(admin_stmt)).all()
        rows = [*member_rows, *admin_rows]

        for contribution, user in rows:
            await notifications.send(
                to_email=user.email,
                to_name=f"{user.first_name} {user.last_name}",
                template_name="purse_reminder.html",
                subject=f"Reminder: {purse.title} is still pending",
                context={
                    "first_name": user.first_name,
                    "purse_title": purse.title,
                    "amount": str(contribution.amount_expected - contribution.amount_received),
                    "deadline": format_datetime(purse.deadline),
                },
            )

        logger.info("purse %s: queued %d reminder emails", purse_id, len(rows))
