from typing import Any, Optional


class AppException(Exception):
    status_code: int = 400
    code: str = "error"

    def __init__(self, message: str, details: Optional[Any] = None, code: Optional[str] = None):
        self.message = message
        self.details = details
        if code:
            self.code = code
        super().__init__(message)


class NotFoundError(AppException):
    status_code = 404
    code = "not_found"


class GoneError(AppException):
    status_code = 410
    code = "gone"


class ConflictError(AppException):
    status_code = 409
    code = "conflict"


class ValidationAppError(AppException):
    status_code = 400
    code = "validation_error"


class AuthError(AppException):
    status_code = 401
    code = "auth_error"


class ForbiddenError(AppException):
    status_code = 403
    code = "forbidden"


class BusinessRuleError(AppException):
    status_code = 422
    code = "business_rule_violation"
