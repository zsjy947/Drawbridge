"""Unified error codes and exception hierarchy.

Error codes are part of the external MCP contract (see MVP spec §7).  Tool
errors are returned as structured results, never as HTTP status codes.
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    """Stable string error codes surfaced to MCP clients."""

    UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
    FORBIDDEN_OPERATION = "FORBIDDEN_OPERATION"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    NO_BASELINE = "NO_BASELINE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
    STALE_PLAN = "STALE_PLAN"
    DRIFT_DETECTED = "DRIFT_DETECTED"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    DISK_BUDGET_EXCEEDED = "DISK_BUDGET_EXCEEDED"
    BUSY = "BUSY"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED = "UNSUPPORTED"
    BUILD_FAILED = "BUILD_FAILED"
    BUILD_UNSUPPORTED_FRONTEND = "BUILD_UNSUPPORTED_FRONTEND"
    VERIFY_FAILED = "VERIFY_FAILED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    RATE_LIMITED = "RATE_LIMITED"
    MAINTENANCE = "MAINTENANCE"
    UNKNOWN_APP = "UNKNOWN_APP"
    UNKNOWN_ENVIRONMENT = "UNKNOWN_ENVIRONMENT"
    UNKNOWN_PLAN = "UNKNOWN_PLAN"
    UNKNOWN_JOB = "UNKNOWN_JOB"
    UNKNOWN_RELEASE = "UNKNOWN_RELEASE"
    UNKNOWN_REF = "UNKNOWN_REF"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL = "INTERNAL"
    CONFIG_INVALID = "CONFIG_INVALID"
    SOURCE_INVALID = "SOURCE_INVALID"
    UNREACHABLE_REF = "UNREACHABLE_REF"
    AUTH_FAILED = "AUTH_FAILED"
    FORBIDDEN_SOURCE = "FORBIDDEN_SOURCE"


class DrawbridgeError(Exception):
    """Base class for all tool-level errors with a stable code."""

    code = ErrorCode.INTERNAL
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        retry_after_seconds: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }
        if self.retry_after_seconds is not None:
            payload["retry_after_seconds"] = self.retry_after_seconds
        if self.details:
            payload["details"] = self.details
        return payload


class InvalidParameterError(DrawbridgeError):
    code = ErrorCode.INVALID_PARAMETER


class UnknownOperationError(DrawbridgeError):
    code = ErrorCode.UNKNOWN_OPERATION


class ForbiddenOperationError(DrawbridgeError):
    code = ErrorCode.FORBIDDEN_OPERATION


class MaintenanceError(DrawbridgeError):
    code = ErrorCode.MAINTENANCE


class BusyError(DrawbridgeError):
    code = ErrorCode.BUSY
    retryable = True


class RateLimitedError(DrawbridgeError):
    code = ErrorCode.RATE_LIMITED
    retryable = True


class IdempotencyConflictError(DrawbridgeError):
    code = ErrorCode.IDEMPOTENCY_CONFLICT


class StalePlanError(DrawbridgeError):
    code = ErrorCode.STALE_PLAN


class QueueTimeoutError(DrawbridgeError):
    code = ErrorCode.QUEUE_TIMEOUT


class UnknownPlanError(DrawbridgeError):
    code = ErrorCode.UNKNOWN_PLAN


class UnknownJobError(DrawbridgeError):
    code = ErrorCode.UNKNOWN_JOB


class UnknownReleaseError(DrawbridgeError):
    code = ErrorCode.UNKNOWN_RELEASE


class UnknownAppError(DrawbridgeError):
    code = ErrorCode.UNKNOWN_APP


class UnknownEnvironmentError(DrawbridgeError):
    code = ErrorCode.UNKNOWN_ENVIRONMENT


class UnknownRefError(DrawbridgeError):
    code = ErrorCode.UNKNOWN_REF


class UnreachableRefError(DrawbridgeError):
    code = ErrorCode.UNREACHABLE_REF


class ConfigInvalidError(DrawbridgeError):
    code = ErrorCode.CONFIG_INVALID
