"""调度与发布任务的状态机（契约 §3.2 / §3.3）。

本模块是 scheduler 侧的**唯一**状态判定点，三件事都在这里：

1. 合法迁移表（`pending → scheduled → publishing → published | failed`，`cancelled` 从任意非终态进入）；
2. `PublishResult` → `PublishJobStatus` 的收敛（**受理 ≠ 发布成功**）；
3. 结果对象的宽松收敛（A2 的 `PublishResult` / `DispatchOutcome` / 字典都能吃）。

> `PublishResult → PublishJobStatus` 这一段在 A2 的 `publish/state.py` 里也有一份。
> 之所以重复 15 行，是因为派工单 §1 要求两个域各自独立可测；两边都以契约 §3.3 为准。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from pulse.services.scheduler.errors import InvalidTransition, SchedulerError
from pulse.shared.enums import (
    TERMINAL_JOB_STATUSES,
    ErrorClass,
    PublishJobStatus,
    ScheduleStatus,
)
from pulse.shared.models import PublishResult

#: 排期状态迁移（契约 §3.2）。终态无出边。
ALLOWED_SCHEDULE_TRANSITIONS: Mapping[ScheduleStatus, frozenset[ScheduleStatus]] = {
    ScheduleStatus.PENDING: frozenset({ScheduleStatus.SCHEDULED, ScheduleStatus.CANCELLED}),
    ScheduleStatus.SCHEDULED: frozenset(
        {ScheduleStatus.PUBLISHING, ScheduleStatus.FAILED, ScheduleStatus.CANCELLED}
    ),
    ScheduleStatus.PUBLISHING: frozenset(
        {ScheduleStatus.PUBLISHED, ScheduleStatus.FAILED, ScheduleStatus.CANCELLED}
    ),
    ScheduleStatus.PUBLISHED: frozenset(),
    ScheduleStatus.FAILED: frozenset(),
    ScheduleStatus.CANCELLED: frozenset(),
}

#: 发布任务状态迁移（契约 §3.3）。
ALLOWED_JOB_TRANSITIONS: Mapping[PublishJobStatus, frozenset[PublishJobStatus]] = {
    PublishJobStatus.QUEUED: frozenset(
        {
            PublishJobStatus.DISPATCHING,
            PublishJobStatus.CANCELLED,
            PublishJobStatus.FAILED,
        }
    ),
    PublishJobStatus.DISPATCHING: frozenset(
        {
            PublishJobStatus.PUBLISHING,
            PublishJobStatus.PENDING_FINALIZE,
            PublishJobStatus.RETRYING,
            PublishJobStatus.FAILED,
            PublishJobStatus.REJECTED,
            PublishJobStatus.CANCELLED,
        }
    ),
    PublishJobStatus.PUBLISHING: frozenset(
        {
            PublishJobStatus.PUBLISHED,
            PublishJobStatus.PENDING_FINALIZE,
            PublishJobStatus.RETRYING,
            PublishJobStatus.FAILED,
            PublishJobStatus.REJECTED,
            PublishJobStatus.CANCELLED,
        }
    ),
    PublishJobStatus.PENDING_FINALIZE: frozenset(
        {
            PublishJobStatus.PUBLISHED,
            PublishJobStatus.FAILED,
            PublishJobStatus.CANCELLED,
        }
    ),
    # `failed → retrying` 对应契约 §3.3 的 `failed → retrying → publishing`：
    # 自动重试走 RETRYING；人工重投才从 FAILED 出发（见 Dispatcher.retry_job）。
    PublishJobStatus.FAILED: frozenset(
        {PublishJobStatus.RETRYING, PublishJobStatus.CANCELLED}
    ),
    PublishJobStatus.RETRYING: frozenset(
        {
            PublishJobStatus.DISPATCHING,
            PublishJobStatus.PUBLISHING,
            PublishJobStatus.FAILED,
            PublishJobStatus.CANCELLED,
        }
    ),
    PublishJobStatus.PUBLISHED: frozenset(),
    PublishJobStatus.REJECTED: frozenset(),
    PublishJobStatus.CANCELLED: frozenset(),
}

#: "已受理但未终结"的平台状态（**绝不能**直接当 published）
ACCEPTED_STATUSES: frozenset[str] = frozenset({"publishing", "pending_finalize"})


def assert_schedule_transition(
    current: ScheduleStatus | str, target: ScheduleStatus | str
) -> ScheduleStatus:
    """校验排期状态迁移；不合法抛 `InvalidTransition`。"""

    src = ScheduleStatus(str(current))
    dst = ScheduleStatus(str(target))
    if dst in ALLOWED_SCHEDULE_TRANSITIONS[src]:
        return dst
    allowed = ", ".join(sorted(s.value for s in ALLOWED_SCHEDULE_TRANSITIONS[src])) or "无（终态）"
    raise InvalidTransition(f"排期状态不允许 {src.value} → {dst.value}（允许：{allowed}）")


def assert_job_transition(
    current: PublishJobStatus | str, target: PublishJobStatus | str
) -> PublishJobStatus:
    """校验发布任务状态迁移；不合法抛 `InvalidTransition`。"""

    src = PublishJobStatus(str(current))
    dst = PublishJobStatus(str(target))
    if dst in ALLOWED_JOB_TRANSITIONS[src]:
        return dst
    allowed = ", ".join(sorted(s.value for s in ALLOWED_JOB_TRANSITIONS[src])) or "无（终态）"
    raise InvalidTransition(f"发布任务状态不允许 {src.value} → {dst.value}（允许：{allowed}）")


def is_terminal_job(status: PublishJobStatus | str) -> bool:
    """是否终态（`published` / `failed` / `rejected` / `cancelled`）。"""

    return PublishJobStatus(str(status)) in TERMINAL_JOB_STATUSES


def _status_literal(value: Any) -> str:
    """把状态值（枚举 / 字符串）统一成小写字面量。"""

    if isinstance(value, Enum):
        return str(value.value).lower()
    return str(value or "").lower()


def job_status_for_result(result: PublishResult) -> PublishJobStatus:
    """`PublishResult` → `PublishJobStatus`（契约 §3.3）。

    关键规则：`ok=True` 但状态是 `publishing` / `pending_finalize` 时，
    只能进 `PENDING_FINALIZE` —— **受理不等于发布成功**。
    """

    status = _status_literal(result.status)
    error_class = None
    if result.error_class:
        try:
            error_class = ErrorClass(_status_literal(result.error_class))
        except ValueError:
            error_class = None

    if result.ok:
        if status == PublishJobStatus.PUBLISHED.value:
            return PublishJobStatus.PUBLISHED
        if status in ACCEPTED_STATUSES:
            return PublishJobStatus.PENDING_FINALIZE
        return PublishJobStatus.FAILED

    if status == PublishJobStatus.REJECTED.value or error_class is ErrorClass.POLICY_REJECTED:
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


def schedule_status_for_job(job_status: PublishJobStatus | str) -> ScheduleStatus | None:
    """发布任务状态 → 排期状态；`None` 表示排期状态不变。"""

    status = PublishJobStatus(str(job_status))
    if status is PublishJobStatus.PUBLISHED:
        return ScheduleStatus.PUBLISHED
    if status in (PublishJobStatus.FAILED, PublishJobStatus.REJECTED):
        return ScheduleStatus.FAILED
    if status is PublishJobStatus.CANCELLED:
        return ScheduleStatus.CANCELLED
    if status in (PublishJobStatus.DISPATCHING, PublishJobStatus.PUBLISHING):
        return ScheduleStatus.PUBLISHING
    return None


def coerce_publish_result(value: Any) -> PublishResult:
    """把各种"结果形态"收敛成 `PublishResult`。

    接受：

    * `PublishResult` 本身；
    * 消息/JSON 形态的 `Mapping`；
    * 带 `.ok` / `.status` / `.error_class` / `.error_message` 的对象；
    * 带 `.result`（隐藏着 `PublishResult`）的对象，如 A2 的 `DispatchOutcome`。
    """

    if isinstance(value, PublishResult):
        return value

    inner = getattr(value, "result", None)
    if isinstance(inner, PublishResult):
        return inner

    if isinstance(value, Mapping):
        return PublishResult(
            ok=bool(value.get("ok")),
            status=_status_literal(value.get("status", "failed")),
            platform_post_id=value.get("platform_post_id"),
            post_url=value.get("post_url"),
            error_class=(
                _status_literal(value["error_class"]) if value.get("error_class") else None
            ),
            error_message=value.get("error_message"),
        )

    if hasattr(value, "ok") and hasattr(value, "status"):
        error_class = getattr(value, "error_class", None)
        return PublishResult(
            ok=bool(value.ok),
            status=_status_literal(value.status),
            platform_post_id=getattr(value, "platform_post_id", None),
            post_url=getattr(value, "post_url", None),
            error_class=_status_literal(error_class) if error_class else None,
            error_message=getattr(value, "error_message", None),
        )

    raise SchedulerError(
        f"无法识别的发布结果类型：{type(value).__name__}"
        "（期望 PublishResult / dict / 带 result 的对象）"
    )


def backoff_delay_s(attempt: int, *, base_s: float = 30.0, max_delay_s: float = 900.0) -> float:
    """指数退避（`attempt` 从 1 开始计数）。"""

    if attempt < 1:
        raise ValueError(f"attempt 必须 >= 1，当前为 {attempt}")
    return min(base_s * (2 ** (attempt - 1)), max_delay_s)


def iso(value: datetime | None) -> str | None:
    """ISO8601 序列化（带偏移）；`None` 原样返回。"""

    if value is None:
        return None
    if value.tzinfo is None:
        raise SchedulerError("内部时间出现了无偏移值，拒绝序列化（会让排期漂移）")
    return value.isoformat()


def utcnow() -> datetime:
    """带偏移的当前时间。"""

    return datetime.now(timezone.utc)


__all__ = [
    "ACCEPTED_STATUSES",
    "ALLOWED_JOB_TRANSITIONS",
    "ALLOWED_SCHEDULE_TRANSITIONS",
    "assert_job_transition",
    "assert_schedule_transition",
    "backoff_delay_s",
    "coerce_publish_result",
    "is_terminal_job",
    "iso",
    "job_status_for_result",
    "schedule_status_for_job",
    "utcnow",
]
