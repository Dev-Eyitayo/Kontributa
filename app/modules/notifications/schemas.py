from uuid import UUID

from pydantic import BaseModel


class RemindPurseResponse(BaseModel):
    purse_id: UUID
    status: str
