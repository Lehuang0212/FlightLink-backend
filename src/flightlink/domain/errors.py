"""Typed domain exceptions mapped to standardized HTTP status codes and error codes."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ErrorDetailItem:
    field: str
    message: str


@dataclass
class DomainError(Exception):
    status_code: int
    code: str
    message: str
    retryable: bool = False
    details: list[ErrorDetailItem] = field(default_factory=list)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class UnauthorizedError(DomainError):
    def __init__(self, code: str = "AUTH_REQUIRED", message: str = "Authentication required.") -> None:
        super().__init__(status_code=401, code=code, message=message, retryable=False)


class ForbiddenError(DomainError):
    def __init__(self, message: str = "Admin role is required for write operations.") -> None:
        super().__init__(status_code=403, code="INSUFFICIENT_ROLE", message=message, retryable=False)


class NotFoundError(DomainError):
    def __init__(
        self,
        code: str = "NOT_FOUND",
        message: str = "Resource not found.",
        details: list[ErrorDetailItem] | None = None,
    ) -> None:
        super().__init__(
            status_code=404,
            code=code,
            message=message,
            retryable=False,
            details=details or [],
        )


class ConflictError(DomainError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: list[ErrorDetailItem] | None = None,
    ) -> None:
        super().__init__(
            status_code=409,
            code=code,
            message=message,
            retryable=retryable,
            details=details or [],
        )


class GoneError(DomainError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: list[ErrorDetailItem] | None = None,
    ) -> None:
        super().__init__(
            status_code=410,
            code=code,
            message=message,
            retryable=False,
            details=details or [],
        )


class PreconditionFailedError(DomainError):
    def __init__(
        self,
        message: str = "Configuration version mismatch.",
        details: list[ErrorDetailItem] | None = None,
    ) -> None:
        super().__init__(
            status_code=412,
            code="CONFIG_VERSION_MISMATCH",
            message=message,
            retryable=False,
            details=details or [],
        )


class ValidationDomainError(DomainError):
    def __init__(
        self,
        message: str = "Request validation failed.",
        details: list[ErrorDetailItem] | None = None,
    ) -> None:
        super().__init__(
            status_code=422,
            code="VALIDATION_ERROR",
            message=message,
            retryable=False,
            details=details or [],
        )
