"""错误分级与重试策略（契约 §3.4）。

三级：可重试（`rate_limited` / `transient` / `auth_expired`）、
轮询（`media_processing`）、不可重试（`policy_rejected` / `validation_error`）。
"""

from __future__ import annotations

from dataclasses import dataclass

from pulse.shared.enums import NON_RETRYABLE_ERRORS, RETRYABLE_ERRORS, ErrorClass

#: 平台业务错误码 → ErrorClass。**先看 reason 再看 HTTP 状态码**：
#: 例如 YouTube 用 403 同时表达"配额超限"与"政策拒绝"，只看状态码必然误判。
_REASON_TO_ERROR_CLASS: dict[str, ErrorClass] = {
    # 限流 / 配额
    "quotaexceeded": ErrorClass.RATE_LIMITED,
    "ratelimitexceeded": ErrorClass.RATE_LIMITED,
    "userratelimitexceeded": ErrorClass.RATE_LIMITED,
    "toomanyrequests": ErrorClass.RATE_LIMITED,
    # 认证
    "invalidcredentials": ErrorClass.AUTH_EXPIRED,
    "unauthorized": ErrorClass.AUTH_EXPIRED,
    "expiredtoken": ErrorClass.AUTH_EXPIRED,
    # 政策 / 版权
    "policyviolation": ErrorClass.POLICY_REJECTED,
    "communityguidelines": ErrorClass.POLICY_REJECTED,
    "copyright": ErrorClass.POLICY_REJECTED,
    "forbidden": ErrorClass.POLICY_REJECTED,
    # 异步处理
    "processingfailure": ErrorClass.MEDIA_PROCESSING,
    "mediaprocessing": ErrorClass.MEDIA_PROCESSING,
}


def classify_http_status(
    status_code: int,
    *,
    reason: str | None = None,
    message: str = "",
) -> ErrorClass:
    """把平台响应翻译成 `ErrorClass`（契约 §3.4）。

    Args:
        status_code: HTTP 状态码。
        reason: 平台业务错误码（如 `quotaExceeded`），**优先于**状态码。
        message: 人类可读消息，仅用于兜底判断。
    """

    if reason:
        # 平台写法不统一：quotaExceeded / QUOTA_EXCEEDED / quota-exceeded
        normalized = reason.strip().lower().replace("_", "").replace("-", "")
        mapped = _REASON_TO_ERROR_CLASS.get(normalized)
        if mapped is not None:
            return mapped

    if status_code == 429:
        return ErrorClass.RATE_LIMITED
    if status_code == 401:
        return ErrorClass.AUTH_EXPIRED
    if status_code == 403:
        # 403 默认按政策拒绝；配额场景必须由 reason 命中上面的表。
        return ErrorClass.POLICY_REJECTED
    if status_code in (400, 404, 409, 422):
        return ErrorClass.VALIDATION_ERROR
    if status_code >= 500 or status_code in (408, 504):
        return ErrorClass.TRANSIENT

    lowered = message.lower()
    if "timeout" in lowered or "temporarily" in lowered:
        return ErrorClass.TRANSIENT
    return ErrorClass.VALIDATION_ERROR


def result_status_for(error_class: ErrorClass | str | None) -> str:
    """`ErrorClass` → `PublishResult.status`（契约 §3.3 / §3.4）。"""

    if error_class is None:
        return "failed"
    ec = ErrorClass(error_class)
    if ec is ErrorClass.POLICY_REJECTED:
        return "rejected"
    if ec is ErrorClass.MEDIA_PROCESSING:
        return "pending_finalize"
    return "failed"


@dataclass(frozen=True, slots=True)
class RetryPlan:
    """重试决策。`delay_s` 仅当 `retry=True` 时有意义。"""

    retry: bool
    delay_s: float | None
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """指数退避重试策略。

    `auth_expired` 的"先刷新 Token 再重试"归 **A3 identity 域**；
    网关只标记"可重试"，不自己刷新凭据（避免两处同时改 token 互相打架）。
    """

    base_delay_s: float = 30.0
    max_delay_s: float = 900.0
    max_attempts: int = 5

    def plan(self, error_class: ErrorClass | str | None, attempt: int) -> RetryPlan:
        """给定错误类别与已失败次数（1 = 第一次已失败），产出重试决策。"""

        if error_class is None:
            return RetryPlan(False, None, "failed", "未知错误，转人工")

        ec = ErrorClass(error_class)

        if ec in NON_RETRYABLE_ERRORS:
            return RetryPlan(False, None, result_status_for(ec), f"{ec.value} 不可重试")

        if ec is ErrorClass.MEDIA_PROCESSING:
            return RetryPlan(False, None, "pending_finalize", "平台异步处理中，转轮询")

        if ec not in RETRYABLE_ERRORS:  # pragma: no cover - 枚举已穷尽
            return RetryPlan(False, None, "failed", f"{ec.value} 未归类，保守转人工")

        if attempt >= self.max_attempts:
            return RetryPlan(
                False,
                None,
                "failed",
                f"{ec.value} 已达最大重试次数 {self.max_attempts}，转人工",
            )

        delay = min(self.base_delay_s * (2 ** (attempt - 1)), self.max_delay_s)
        return RetryPlan(
            True,
            delay,
            "retrying",
            f"{ec.value} 第 {attempt} 次失败，退避 {delay:.0f}s",
        )
