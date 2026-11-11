"""Application error types with a single, consistent API error envelope."""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None, details: Any = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_error"


class QuotaExceeded(AppError):
    status_code = 402
    code = "quota_exceeded"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class NodeError(Exception):
    """Raised by workflow node handlers.

    ``retryable`` marks transient failures (timeouts, 429, 5xx). The executor
    only retries when the node is also safe to repeat.
    """

    def __init__(self, message: str, *, code: str = "node_error", retryable: bool = False, details: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.details = details

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}
