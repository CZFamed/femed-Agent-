"""A3 scheduler 域异常。

设计要点：配额与限流这两类"可预期失败"必须带**可读原因**与**建议等待时长**，
这样上游（A5）能直接把原因展示给运营，而不是抛一个 "failed" 让人猜。
"""

from __future__ import annotations

from typing import Any


class SchedulerError(Exception):
    """scheduler 域所有异常的基类。"""


class ScheduleNotFound(SchedulerError, LookupError):
    """排期不存在（`schedules.id` 查不到）。"""


class JobNotFound(SchedulerError, LookupError):
    """发布任务不存在（`publish_jobs.id` 查不到）。"""


class InvalidSchedule(SchedulerError, ValueError):
    """排期字段不合法（无时区偏移的时间、未知平台、空 ID 等）。"""


class InvalidTransition(SchedulerError, ValueError):
    """状态机不允许的迁移（契约 §3.2 / §3.3）。"""


class DuplicateJob(SchedulerError, ValueError):
    """`publish_jobs.unified_post_id` 唯一索引冲突（幂等第一层防线命中）。"""


class DuplicateSchedule(SchedulerError, ValueError):
    """`schedules.id` 主键冲突。"""


class QueueError(SchedulerError, RuntimeError):
    """队列操作失败（任务不存在、重排已取消的任务等）。"""


class CircuitOpen(SchedulerError, RuntimeError):
    """账号已熔断（`paused` / `revoked`），拒绝任何投递。"""


class AccountUnavailable(CircuitOpen):
    """投递时账号不可用（不存在或非 `active`）。"""


class RateLimitExceeded(SchedulerError, RuntimeError):
    """令牌桶或发布冷却拦截。`retry_after_s` 给出建议等待秒数。"""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class QuotaExceeded(SchedulerError, RuntimeError):
    """配额预检失败：**不投递**，并把可读原因带回给调用方。"""

    def __init__(
        self,
        message: str,
        *,
        decision: Any = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.decision = decision
        self.retry_after_s = retry_after_s


__all__ = [
    "AccountUnavailable",
    "CircuitOpen",
    "DuplicateJob",
    "DuplicateSchedule",
    "InvalidSchedule",
    "InvalidTransition",
    "JobNotFound",
    "QueueError",
    "QuotaExceeded",
    "RateLimitExceeded",
    "ScheduleNotFound",
    "SchedulerError",
]
