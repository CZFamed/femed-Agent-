"""`schedules` / `publish_jobs` 的存储（契约 §6）。

两个存储都是"协议 + 内存实现"：单测不连 PostgreSQL，生产实现由部署层注入。

**幂等的第一层防线**在 `InMemoryJobStore.claim()`：它复刻
`publish_jobs.unified_post_id` 的**唯一索引**语义（第二层是 A2 Adapter 的 `find_existing()`）。

关于两个反规范化字段（`account_id` / `platform`）：
契约 §6 的 `publish_jobs` 没有这两列（账号可从 `schedule_id` JOIN 出来）。
这里存下来是为了让"账号熔断时定位该账号全部待发任务"变成一次索引查询，
落库实现可以改回 JOIN 并删掉它们；字段名不与契约冲突（详见交付报告的契约对齐声明）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from pulse.services.scheduler.errors import (
    DuplicateSchedule,
    JobNotFound,
    ScheduleNotFound,
)
from pulse.services.scheduler.state import is_terminal_job
from pulse.shared.enums import PublishJobStatus, ScheduleStatus

#: 尚未投递出去（可被熔断撤销）的任务状态
PENDING_JOB_STATUSES: frozenset[PublishJobStatus] = frozenset(
    {PublishJobStatus.QUEUED, PublishJobStatus.RETRYING}
)

#: 已经在平台上"在飞"的任务状态（熔断不能声称撤销了它们）
IN_FLIGHT_JOB_STATUSES: frozenset[PublishJobStatus] = frozenset(
    {
        PublishJobStatus.DISPATCHING,
        PublishJobStatus.PUBLISHING,
        PublishJobStatus.PENDING_FINALIZE,
    }
)


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    """`schedules` 表的一行（字段名与契约 §6 一致）。"""

    id: str
    variant_id: str
    account_id: str
    timezone: str
    scheduled_at: datetime
    status: ScheduleStatus | str = ScheduleStatus.PENDING
    #: 运维字段（非契约列）：当前挂起的队列任务 ID，用于取消与重排
    task_id: str | None = None
    #: 审计字段：最近一次状态变更的原因（best-time 命中/回退也写在这里）
    reason: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def status_value(self) -> ScheduleStatus:
        """当前状态（枚举形式）。"""

        return ScheduleStatus(str(self.status))

    @property
    def is_terminal(self) -> bool:
        """是否终态（`published` / `failed` / `cancelled`）。"""

        return self.status_value in (
            ScheduleStatus.PUBLISHED,
            ScheduleStatus.FAILED,
            ScheduleStatus.CANCELLED,
        )

    def validate(self) -> list[str]:
        """返回全部字段错误（空列表 = 通过）。"""

        errors: list[str] = []
        if not self.id or not str(self.id).startswith("sched_"):
            errors.append(f"schedules.id 必须以 sched_ 开头：{self.id!r}")
        if not self.variant_id:
            errors.append("schedules.variant_id 不能为空")
        if not self.account_id:
            errors.append("schedules.account_id 不能为空")
        if not self.timezone:
            errors.append("schedules.timezone 不能为空")
        if self.scheduled_at is None:
            errors.append("schedules.scheduled_at 不能为空")
        elif self.scheduled_at.tzinfo is None:
            errors.append("schedules.scheduled_at 必须携带时区偏移（否则排期会漂移）")
        try:
            ScheduleStatus(str(self.status))
        except ValueError:
            allowed = ", ".join(s.value for s in ScheduleStatus)
            errors.append(f"schedules.status 非法：{self.status!r}（允许：{allowed}）")
        return errors

    def evolve(self, **changes: Any) -> "ScheduleRecord":
        """返回替换了部分字段的新记录（本类型不可变）。"""

        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        """序列化（时间带偏移）。"""

        return {
            "id": self.id,
            "variant_id": self.variant_id,
            "account_id": self.account_id,
            "timezone": self.timezone,
            "scheduled_at": self.scheduled_at.isoformat(),
            "status": self.status_value.value,
            "task_id": self.task_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PublishJobRecord:
    """`publish_jobs` 表的一行（+ 两个反规范化查询键，见模块 docstring）。"""

    id: str
    unified_post_id: str
    schedule_id: str | None = None
    status: PublishJobStatus | str = PublishJobStatus.QUEUED
    attempts: int = 0
    next_retry_at: datetime | None = None
    error_class: str | None = None
    error_message: str | None = None
    #: 反规范化：账号与平台（便于按账号熔断查询）
    account_id: str = ""
    platform: str = ""
    #: 运维字段（非契约列）：当前挂起的队列任务 ID
    task_id: str | None = None
    #: `pending_finalize` 的收敛截止时间（超时 → 告警，不自动判定）
    finalize_deadline: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def status_value(self) -> PublishJobStatus:
        """当前状态（枚举形式）。"""

        return PublishJobStatus(str(self.status))

    @property
    def is_terminal(self) -> bool:
        """是否终态。"""

        return is_terminal_job(self.status_value)

    def validate(self) -> list[str]:
        """返回全部字段错误（空列表 = 通过）。"""

        errors: list[str] = []
        if not self.id or not str(self.id).startswith("job_"):
            errors.append(f"publish_jobs.id 必须以 job_ 开头：{self.id!r}")
        if not self.unified_post_id or not str(self.unified_post_id).startswith("up_"):
            errors.append(
                f"publish_jobs.unified_post_id 必须以 up_ 开头：{self.unified_post_id!r}"
            )
        try:
            PublishJobStatus(str(self.status))
        except ValueError:
            allowed = ", ".join(s.value for s in PublishJobStatus)
            errors.append(f"publish_jobs.status 非法：{self.status!r}（允许：{allowed}）")
        if self.attempts < 0:
            errors.append(f"publish_jobs.attempts 不能为负数：{self.attempts}")
        return errors

    def evolve(self, **changes: Any) -> "PublishJobRecord":
        """返回替换了部分字段的新记录（本类型不可变）。"""

        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        """序列化（时间带偏移）。"""

        return {
            "id": self.id,
            "schedule_id": self.schedule_id,
            "unified_post_id": self.unified_post_id,
            "status": self.status_value.value,
            "attempts": self.attempts,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "error_class": self.error_class,
            "error_message": self.error_message,
            "account_id": self.account_id,
            "platform": self.platform,
            "task_id": self.task_id,
            "finalize_deadline": (
                self.finalize_deadline.isoformat() if self.finalize_deadline else None
            ),
        }


class ScheduleStore(Protocol):
    """`schedules` 存储协议。"""

    def add(self, record: ScheduleRecord) -> ScheduleRecord:
        """插入新排期；主键冲突抛 `DuplicateSchedule`。"""

    def get(self, schedule_id: str) -> ScheduleRecord | None:
        """按 ID 取排期。"""

    def save(self, record: ScheduleRecord) -> ScheduleRecord:
        """写回排期；不存在抛 `ScheduleNotFound`。"""

    def list_schedules(
        self,
        *,
        account_id: str | None = None,
        status: ScheduleStatus | str | None = None,
    ) -> tuple[ScheduleRecord, ...]:
        """按账号/状态过滤。"""


class JobStore(Protocol):
    """`publish_jobs` 存储协议（含唯一索引语义）。"""

    def claim(self, record: PublishJobRecord) -> bool:
        """按 `unified_post_id` 唯一索引占位；已存在返回 False。"""

    def get(self, job_id: str) -> PublishJobRecord | None:
        """按 job_id 取任务。"""

    def get_by_schedule(self, schedule_id: str) -> PublishJobRecord | None:
        """按排期取任务（一条排期最多一个任务）。"""

    def save(self, record: PublishJobRecord) -> PublishJobRecord:
        """写回任务；不存在抛 `JobNotFound`。"""

    def list_jobs(
        self,
        *,
        account_id: str | None = None,
        statuses: tuple[PublishJobStatus, ...] | None = None,
    ) -> tuple[PublishJobRecord, ...]:
        """按账号/状态集合过滤。"""


class InMemoryScheduleStore:
    """进程内实现（单测与本地开发用）。"""

    def __init__(self, records: tuple[ScheduleRecord, ...] = ()) -> None:
        self._rows: dict[str, ScheduleRecord] = {}
        for record in records:
            self.add(record)

    def add(self, record: ScheduleRecord) -> ScheduleRecord:
        errors = record.validate()
        if errors:
            raise ValueError("; ".join(errors))
        if record.id in self._rows:
            raise DuplicateSchedule(f"排期已存在（schedules.id 主键冲突）：{record.id}")
        self._rows[record.id] = record
        return record

    def get(self, schedule_id: str) -> ScheduleRecord | None:
        return self._rows.get(schedule_id)

    def save(self, record: ScheduleRecord) -> ScheduleRecord:
        if record.id not in self._rows:
            raise ScheduleNotFound(f"排期不存在，无法更新：{record.id}")
        self._rows[record.id] = record
        return record

    def list_schedules(
        self,
        *,
        account_id: str | None = None,
        status: ScheduleStatus | str | None = None,
    ) -> tuple[ScheduleRecord, ...]:
        wanted = ScheduleStatus(str(status)) if status is not None else None
        rows = [
            row
            for row in self._rows.values()
            if (account_id is None or row.account_id == account_id)
            and (wanted is None or row.status_value is wanted)
        ]
        return tuple(sorted(rows, key=lambda r: (r.scheduled_at, r.id)))

    def all_records(self) -> tuple[ScheduleRecord, ...]:
        """全部排期（无序）。"""

        return tuple(self._rows.values())


class InMemoryJobStore:
    """进程内实现，复刻 `publish_jobs.unified_post_id` 的唯一索引语义。"""

    def __init__(self) -> None:
        self._rows: dict[str, PublishJobRecord] = {}
        self._by_unified: dict[str, str] = {}
        self._by_schedule: dict[str, str] = {}

    def claim(self, record: PublishJobRecord) -> bool:
        errors = record.validate()
        if errors:
            raise ValueError("; ".join(errors))
        if record.unified_post_id in self._by_unified:
            return False
        self._rows[record.id] = record
        self._by_unified[record.unified_post_id] = record.id
        if record.schedule_id:
            self._by_schedule[record.schedule_id] = record.id
        return True

    def get(self, job_id: str) -> PublishJobRecord | None:
        return self._rows.get(job_id)

    def get_by_schedule(self, schedule_id: str) -> PublishJobRecord | None:
        job_id = self._by_schedule.get(schedule_id)
        return self._rows.get(job_id) if job_id else None

    def get_by_unified_post_id(self, unified_post_id: str) -> PublishJobRecord | None:
        """按幂等键取任务（唯一索引的直接查询）。"""

        job_id = self._by_unified.get(unified_post_id)
        return self._rows.get(job_id) if job_id else None

    def save(self, record: PublishJobRecord) -> PublishJobRecord:
        if record.id not in self._rows:
            raise JobNotFound(f"发布任务不存在，无法更新：{record.id}")
        previous = self._rows[record.id]
        if previous.unified_post_id != record.unified_post_id:
            self._by_unified.pop(previous.unified_post_id, None)
            self._by_unified[record.unified_post_id] = record.id
        self._rows[record.id] = record
        return record

    def list_jobs(
        self,
        *,
        account_id: str | None = None,
        statuses: tuple[PublishJobStatus, ...] | None = None,
    ) -> tuple[PublishJobRecord, ...]:
        wanted = (
            {PublishJobStatus(str(s)) for s in statuses} if statuses is not None else None
        )
        rows = [
            row
            for row in self._rows.values()
            if (account_id is None or row.account_id == account_id)
            and (wanted is None or row.status_value in wanted)
        ]
        return tuple(sorted(rows, key=lambda r: r.id))

    def all_jobs(self) -> tuple[PublishJobRecord, ...]:
        """全部任务（无序）。"""

        return tuple(self._rows.values())


__all__ = [
    "IN_FLIGHT_JOB_STATUSES",
    "PENDING_JOB_STATUSES",
    "InMemoryJobStore",
    "InMemoryScheduleStore",
    "JobStore",
    "PublishJobRecord",
    "ScheduleRecord",
    "ScheduleStore",
]
