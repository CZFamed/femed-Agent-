"""发布任务状态机（契约 §3.3）。

**本项目最容易出事故的一条**：平台返回"已受理"只能进 `pending_finalize`，
绝不能直接置 `published`。本模块是这条约束的唯一判定点。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pulse.shared.enums import ErrorClass, PublishJobStatus
from pulse.shared.models import PublishResult

#: `pending_finalize` 默认超时（契约 §3.3：默认 30 分钟）
DEFAULT_FINALIZE_TIMEOUT_S = 30 * 60

#: Adapter 可能返回的"已受理但未终结"状态
ACCEPTED_STATUSES: frozenset[str] = frozenset({"publishing", "pending_finalize"})


def job_status_for(result: PublishResult) -> PublishJobStatus:
    """`PublishResult` → `PublishJobStatus`（契约 §3.3）。

    关键规则：

    * `ok=True` 且 `status == "published"` → PUBLISHED
    * `ok=True` 且 `status` 为 publishing / pending_finalize → **PENDING_FINALIZE**
    * `ok=False` 且 policy_rejected → REJECTED（终态 + 告警）
    * `ok=False` 且可重试 → RETRYING；不可重试 → FAILED
    """

    status = (result.status or "").lower()
    error_class = ErrorClass(result.error_class) if result.error_class else None

    if result.ok:
        if status == "published":
            return PublishJobStatus.PUBLISHED
        if status in ACCEPTED_STATUSES:
            # 受理 ≠ 发布：绝不上抛为 published
            return PublishJobStatus.PENDING_FINALIZE
        return PublishJobStatus.FAILED

    if status == "rejected" or error_class is ErrorClass.POLICY_REJECTED:
        return PublishJobStatus.REJECTED
    if error_class is ErrorClass.MEDIA_PROCESSING or status == "pending_finalize":
        return PublishJobStatus.PENDING_FINALIZE
    if error_class in (
        ErrorClass.RATE_LIMITED,
        ErrorClass.TRANSIENT,
        ErrorClass.AUTH_EXPIRED,
    ):
        return PublishJobStatus.RETRYING
    return PublishJobStatus.FAILED


@dataclass(frozen=True, slots=True)
class PendingWindow:
    """`pending_finalize` 的时间窗。超时**只告警**，不自动判成功也不自动判失败。"""

    deadline: datetime
    timeout_s: int = DEFAULT_FINALIZE_TIMEOUT_S

    def expired(self, now: datetime) -> bool:
        """是否已超时。"""

        return now >= self.deadline

    def alert_message(self, now: datetime) -> str | None:
        """超时则返回给人工的告警文案，否则返回 None。"""

        if not self.expired(now):
            return None
        started = self.deadline - timedelta(seconds=self.timeout_s)
        elapsed = int((now - started).total_seconds())
        return (
            f"pending_finalize 已超过 {self.timeout_s // 60} 分钟"
            f"（已 {elapsed}s）仍未收敛，需人工到平台后台核对后手工收敛"
            "（既不自动置 published，也不自动判 failed）"
        )
