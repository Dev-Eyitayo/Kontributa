from typing import Literal, Optional

from pydantic import BaseModel


class AnalyticsOverviewResponse(BaseModel):
    # Money is always a decimal string on the wire, never a bare JSON
    # number -- see known-limitations.md. The rest of these fields are
    # already-precomputed percentages/counts; the frontend renders them
    # as-is, no client-side math. See AdminAnalyticsService's own
    # docstring for each field's exact definition.
    total_fee_revenue: str
    conversion_rate: float
    active_groups_count: int
    new_groups_count: int
    retention_rate: float


class RevenueTrendPoint(BaseModel):
    label: str
    amount: str


class GrowthTrendPoint(BaseModel):
    label: str
    new_groups: int
    new_members: int


class NeedsAttentionResponse(BaseModel):
    flagged_backlog_count: int
    flagged_backlog_trend: Literal["increasing", "decreasing", "stable"]
    webhook_rescue_rate: float


class ProviderSplitItem(BaseModel):
    provider: Literal["monnify", "paystack"]
    volume: str
    success_rate: float


class GroupHealthItem(BaseModel):
    id: str
    name: str
    last_activity_at: Optional[str] = None
    purse_count: int
    member_count: int
