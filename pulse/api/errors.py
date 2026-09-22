"""接口层的统一错误体与异常映射（所有者：A5，契约 §7）。

契约规定的统一错误体：

    {"error": {"code": "...", "message": "...", "details": {}}}

各域的异常在 `map_exception()` 里集中翻译成 HTTP 状态码，**不把异常直接吐给调用方**
（否则会出现栈信息与内部类名泄漏）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """接口层的统一返回（框架无关：测试直接调 `ApiApp.handle` 拿它）。"""

    status: int
    body: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def as_payload(self) -> dict[str, Any]:
        return dict(self.body)


class ApiError(Exception):
    """接口层自己抛的错（参数缺失、越权、状态冲突等）。"""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = dict(details or {})
        super().__init__(message)

    def response(self) -> ApiResponse:
        return ApiResponse(
            status=self.status,
            body={
                "error": {
                    "code": self.code,
                    "message": self.message,
                    "details": dict(self.details),
                }
            },
        )


def bad_request(message: str, **details: Any) -> ApiError:
    return ApiError(400, "bad_request", message, details=details)


def forbidden(message: str, **details: Any) -> ApiError:
    return ApiError(403, "forbidden", message, details=details)


def not_found(message: str, **details: Any) -> ApiError:
    return ApiError(404, "not_found", message, details=details)


def conflict(message: str, **details: Any) -> ApiError:
    return ApiError(409, "conflict", message, details=details)


def too_many(message: str, **details: Any) -> ApiError:
    return ApiError(429, "rate_limited", message, details=details)


def unsupported(message: str, **details: Any) -> ApiError:
    return ApiError(501, "not_implemented", message, details=details)


def _code_of(exc: Exception) -> str:
    """取域内异常自带的 code（内容域与合规域都提供了 `code` 属性）。"""
    code = getattr(exc, "code", "")
    return str(code) if code else type(exc).__name__


def _details_of(exc: Exception) -> dict[str, Any]:
    details = getattr(exc, "details", None)
    return dict(details) if isinstance(details, Mapping) else {}


def map_exception(exc: Exception) -> ApiResponse:
    """把域内异常翻译成统一错误体 + 合适的 HTTP 状态码。

    映射原则：

    * 找不到 → 404；参数不合法 → 400；状态机不允许 → 409；越权/硬拦截 → 403；限流配额 → 429；
    * 其余未识别的异常统一 500，但**不暴露异常类型与栈信息**给调用方（`details` 里只放 code）。
    """
    if isinstance(exc, ApiError):
        return exc.response()

    # 延迟导入：接口层不希望在 import 阶段就把所有域拉起来
    from pulse.services.compliance import errors as compliance_errors
    from pulse.services.content import errors as content_errors
    from pulse.services.identity import errors as identity_errors
    from pulse.services.scheduler import errors as scheduler_errors

    if isinstance(exc, content_errors.BriefValidationError):
        return ApiError(
            400, "brief_invalid", str(exc), details={"errors": list(exc.errors)}
        ).response()
    if isinstance(exc, (content_errors.UnknownPlatformError, content_errors.UnknownHashtagError)):
        return ApiError(400, _code_of(exc), str(exc), details=_details_of(exc)).response()
    if isinstance(exc, content_errors.ContentNotFoundError):
        return ApiError(404, _code_of(exc), str(exc), details=_details_of(exc)).response()
    if isinstance(exc, content_errors.TokenBudgetExceeded):
        return ApiError(429, _code_of(exc), str(exc), details=_details_of(exc)).response()
    if isinstance(exc, content_errors.ContentError):
        return ApiError(409, _code_of(exc), str(exc), details=_details_of(exc)).response()

    if isinstance(exc, compliance_errors.FindingNotFoundError):
        return ApiError(404, _code_of(exc), str(exc), details=_details_of(exc)).response()
    if isinstance(exc, compliance_errors.WaiverNotAllowedError):
        return ApiError(403, _code_of(exc), str(exc), details=_details_of(exc)).response()
    if isinstance(exc, compliance_errors.ComplianceError):
        return ApiError(500, _code_of(exc), str(exc), details=_details_of(exc)).response()

    if isinstance(
        exc,
        (scheduler_errors.ScheduleNotFound, scheduler_errors.JobNotFound),
    ):
        return ApiError(404, _code_of(exc), str(exc)).response()
    if isinstance(
        exc,
        (scheduler_errors.RateLimitExceeded, scheduler_errors.QuotaExceeded),
    ):
        return ApiError(429, _code_of(exc), str(exc)).response()
    if isinstance(
        exc,
        (
            scheduler_errors.AccountUnavailable,
            scheduler_errors.CircuitOpen,
            scheduler_errors.InvalidSchedule,
            scheduler_errors.InvalidTransition,
            scheduler_errors.DuplicateJob,
            scheduler_errors.DuplicateSchedule,
        ),
    ):
        return ApiError(409, _code_of(exc), str(exc)).response()
    if isinstance(exc, scheduler_errors.SchedulerError):
        return ApiError(500, _code_of(exc), str(exc)).response()

    if isinstance(
        exc,
        (identity_errors.AccountNotFound, identity_errors.CredentialNotFound),
    ):
        return ApiError(404, _code_of(exc), str(exc)).response()
    if isinstance(exc, identity_errors.AccountNotPublishable):
        return ApiError(409, _code_of(exc), str(exc)).response()
    if isinstance(
        exc,
        (identity_errors.InvalidAccount, identity_errors.InvalidCredential),
    ):
        return ApiError(400, _code_of(exc), str(exc)).response()
    if isinstance(
        exc,
        (
            identity_errors.TokenExpired,
            identity_errors.TokenRevoked,
            identity_errors.RefreshFailed,
            identity_errors.VaultError,
        ),
    ):
        return ApiError(403, _code_of(exc), str(exc)).response()
    if isinstance(exc, identity_errors.IdentityError):
        return ApiError(500, _code_of(exc), str(exc)).response()

    # 未识别异常：不泄漏类型与栈，只留一个可检索的 code
    return ApiError(
        500,
        "internal_error",
        "服务内部错误，请把 code 与时间点交给技术排查",
        details={"exception": type(exc).__name__},
    ).response()
