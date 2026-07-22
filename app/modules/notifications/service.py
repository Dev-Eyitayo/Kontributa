import logging
from pathlib import Path
from uuid import UUID

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.auth.models import User
from app.modules.contributions.models import Contribution, ContributionStatus
from app.modules.members.models import Member
from app.modules.notifications.models import NotificationLog, NotificationStatus
from app.modules.purses.models import Purse

logger = logging.getLogger("kontributa.notifications")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR), autoescape=select_autoescape(["html"])
)


def render_template(template_name: str, context: dict) -> str:
    template = _jinja_env.get_template(template_name)
    return template.render(**context)


class SendByteError(AppException):
    status_code = 502
    code = "sendbyte_error"


class SendByteClient:
    """
    Thin wrapper around SendByte's transactional email API (sandbox key
    first, live key swapped in later purely via env var -- same pattern as
    MonnifyClient).

    Request/response shape (POST {base_url}/v1/emails, Bearer auth, JSON
    body with from/to/subject/html, 201 with {id, status: "queued"} on
    success) confirmed directly against SendByte's own quickstart docs.
    Key modes (sk_test_/sk_live_) and scopes are a config-only concern --
    SENDBYTE_API_KEY is the only thing that changes between sandbox and
    live, never this client's code.
    """

    def __init__(self, base_url: str, api_key: str, from_email: str, from_name: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._from_email = from_email
        self._from_name = from_name

    async def send(self, to_email: str, to_name: str, subject: str, html: str) -> str:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=15) as http:
            resp = await http.post(
                "/v1/emails",
                json={
                    "from": f"{self._from_name} <{self._from_email}>",
                    "to": f"{to_name} <{to_email}>" if to_name else to_email,
                    "subject": subject,
                    "html": html,
                },
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        if resp.status_code >= 400:
            raise SendByteError(f"SendByte API error: HTTP {resp.status_code}: {resp.text}")
        return resp.json().get("id", "")


sendbyte_client = SendByteClient(
    base_url=settings.SENDBYTE_BASE_URL,
    api_key=settings.SENDBYTE_API_KEY,
    from_email=settings.SENDBYTE_FROM_EMAIL,
    from_name=settings.SENDBYTE_FROM_NAME,
)


def get_sendbyte_client() -> SendByteClient:
    return sendbyte_client


class NotificationService:
    """The one place that sends transactional email. Deliberately never
    raises: any failure (template error, network error, SendByte 4xx/5xx,
    even a failure to write the log row) is caught and logged here, so a
    notification problem can never block or roll back the business state
    change that triggered it. Every attempt, success or failure, gets a
    NotificationLog row -- an operational record for debugging delivery,
    not a Phase 6 AuditLog entry."""

    def __init__(self, db: AsyncSession, client: SendByteClient):
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
    purse_id: UUID, session_factory: async_sessionmaker, sendbyte: SendByteClient
) -> None:
    """Runs as a background task after POST /purses/{id}/remind responds --
    a purse can have many still-pending members, so the batch send happens
    off the request path, the same way webhook/payout processing does.
    The weekly-cooldown gate and kill switch are already enforced by the
    router before this is even scheduled; this only sends."""
    async with session_factory() as db:
        notifications = NotificationService(db, sendbyte)
        purse = await db.get(Purse, purse_id)
        if purse is None:
            return

        stmt = (
            select(Contribution, Member, User)
            .join(Member, Contribution.member_id == Member.id)
            .join(User, Member.user_id == User.id)
            .where(Contribution.purse_id == purse_id, Contribution.status == ContributionStatus.PENDING)
        )
        rows = (await db.execute(stmt)).all()

        for contribution, member, user in rows:
            await notifications.send(
                to_email=user.email,
                to_name=f"{user.first_name} {user.last_name}",
                template_name="purse_reminder.html",
                subject=f"Reminder: {purse.title} is still pending",
                context={
                    "first_name": user.first_name,
                    "purse_title": purse.title,
                    "amount": str(contribution.amount_expected - contribution.amount_received),
                    "deadline": purse.deadline.isoformat(),
                },
            )

        logger.info("purse %s: queued %d reminder emails", purse_id, len(rows))
