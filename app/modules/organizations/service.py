from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.modules.organizations.models import Group, Organization
from app.modules.organizations.schemas import (
    AdminCreateGroupRequest,
    AdminCreateOrganizationRequest,
    AdminUpdateOrganizationRequest,
)


class OrganizationService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_organizations(self, q: Optional[str] = None) -> list[Organization]:
        stmt = select(Organization).where(Organization.active.is_(True))
        if q:
            stmt = stmt.where(Organization.name.ilike(f"%{q}%"))
        result = await self.db.execute(stmt.order_by(Organization.name))
        return list(result.scalars().all())

    async def list_all_organizations(self) -> list[Organization]:
        result = await self.db.execute(select(Organization).order_by(Organization.name))
        return list(result.scalars().all())

    async def get_organization(self, organization_id: UUID) -> Organization:
        org = await self.db.get(Organization, organization_id)
        if org is None:
            raise NotFoundError("organization not found")
        return org

    async def list_groups(self, organization_id: UUID) -> list[Group]:
        await self.get_organization(organization_id)
        result = await self.db.execute(
            select(Group).where(Group.organization_id == organization_id).order_by(Group.name)
        )
        return list(result.scalars().all())

    async def create_organization(self, payload: AdminCreateOrganizationRequest) -> Organization:
        existing = await self.db.execute(
            select(Organization).where(Organization.short_code == payload.short_code)
        )
        if existing.scalar_one_or_none() is not None:
            raise ConflictError("short_code already in use", code="duplicate_short_code")

        org = Organization(
            name=payload.name,
            short_code=payload.short_code,
            org_type=payload.org_type,
            member_id_format=payload.member_id_format,
        )
        self.db.add(org)
        await self.db.commit()
        await self.db.refresh(org)
        return org

    async def update_organization(
        self, organization_id: UUID, payload: AdminUpdateOrganizationRequest
    ) -> Organization:
        org = await self.get_organization(organization_id)

        if payload.name is not None:
            org.name = payload.name
        if payload.short_code is not None:
            org.short_code = payload.short_code
        if payload.active is not None:
            org.active = payload.active
        if payload.member_id_format is not None:
            org.member_id_format = payload.member_id_format

        await self.db.commit()
        await self.db.refresh(org)
        return org

    async def create_group(self, payload: AdminCreateGroupRequest) -> Group:
        await self.get_organization(payload.organization_id)

        group = Group(
            organization_id=payload.organization_id,
            name=payload.name,
            short_code=payload.short_code,
        )
        self.db.add(group)
        await self.db.commit()
        await self.db.refresh(group)
        return group
