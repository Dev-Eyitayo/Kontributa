from typing import Generic, Sequence, TypeVar

from pydantic import BaseModel

T = TypeVar("T")

DEFAULT_LIMIT = 20
MAX_LIMIT = 200


class Paginated(BaseModel, Generic[T]):
    items: Sequence[T]
    total: int
    limit: int
    offset: int
