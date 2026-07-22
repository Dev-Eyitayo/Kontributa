from typing import Any, Generic, Optional, TypeVar

from fastapi.responses import JSONResponse
from pydantic import BaseModel

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[Any] = None


class StandardResponse(BaseModel, Generic[T]):
    success: bool
    data: Optional[T] = None
    error: Optional[ErrorDetail] = None


def success_response(data: Any = None, status_code: int = 200) -> JSONResponse:
    body = StandardResponse(success=True, data=data, error=None)
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
    )


def error_response(
    code: str,
    message: str,
    status_code: int = 400,
    details: Any = None,
) -> JSONResponse:
    body = StandardResponse(
        success=False,
        data=None,
        error=ErrorDetail(code=code, message=message, details=details),
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
    )
