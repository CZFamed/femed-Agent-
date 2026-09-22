"""账号停用熔断（派工单 §3 W1-A3-9）。

账号变成 `paused` / `revoked` 时，**该账号全部"待发"任务必须即时挂起**：

* `queued` / `retrying` —— 尚未投递出去 → 置 `cancelled` 并撤销队列里的任务；
* `dispatching` / `publishing` / `pending_finalize` —— 已经在平台上"在飞"，
  **不能声称撤销**（可能已经发出去了）；只统计为 `in_flight_jobs` 并在结果里说明；
* 终态任务不动。

本类实现 identity 域 `AccountStatusListener` 协议（`on_account_status_changed`），
但**不 import identity**：靠结构匹配接线，两个域各自可测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pulse.services.scheduler.state import assert_job_transition, assert_schedule_transition
from pulse.services.scheduler.store import (
    IN_FLIGHT_JOB_STATUSES,
    JobStore,
    ScheduleStore,
)
from pulse.shared.enums import PublishJobStatus, ScheduleStatus

#: 触发熔断的账号状态（与 identity `SUSPEND_STATUSES` 一致）
SUSPEND_STATUSES: frozenset[str] = frozenset({"paused", "revoked"})


@dataclass(frozen=True, slots=True)
class CircuitResult:
    """一次熔断的执行结果。"""

    account_id: str
    status: str
    cancelled_jobs: int = 0
    cancelled_schedules: int = 0
    revoked_tasks: int = 0
    in_flight_jobs: int = 0
    reason: str = ""
    triggered: bool = True

    def to_dict(self) -> dict[str, Any]:
        """序列化（可安全入日志）。"""

        return {
            "account_id": self.account_id,
            "status": self.status,
            "cancelled_jobs": self.cancelled_jobs,
            "cancelled_schedules": self.cancelled_schedules,
            "revoked_tasks": self.revoked_tasks,
            "in_flight_jobs": self.in_flight_jobs,
            "reason": self.reason,
            "triggered": self.triggered,
        }


class AccountCircuitBreaker:
    """把"账号停用"翻译成"停止后续投递"的执行器。"""

    def __init__(self, *, schedules: ScheduleStore, jobs: JobStore, queue: Any) -> None:
        self._schedules = schedules
        self._jobs = jobs
        self._queue = queue
        self._suspended: dict[str, str] = {}

    def is_suspended(self, account_id: str) -> bool:
        """该账号当前是否处于熔断状态。"""

        return account_id in self._suspended

    def suspended_accounts(self) -> tuple[str, ...]:
        """已熔断的账号列表。"""

        return tuple(sorted(self._suspended))

    def suspend_account(
        self, account_id: str, status: str, *, reason: str = ""
    ) -> CircuitResult:
        """按目标账号状态挂起该账号的待发任务。"""

        status_value = str(status)
        if status_value not in SUSPEND_STATUSES:
            return CircuitResult(
                account_id=account_id,
                status=status_value,
                reason=f"账号状态 {status_value} 不是熔断条件（paused / revoked），无需挂起",
                triggered=False,
            )

        self._suspended[account_id] = status_value
        jobs = self._jobs.list_jobs(account_id=account_id)
        in_flight_schedule_ids = {
            job.schedule_id
            for job in jobs
            if job.status_value in IN_FLIGHT_JOB_STATUSES and job.schedule_id
        }

        cancelled_jobs = 0
        revoked_task_ids: set[str] = set()
        in_flight = 0
        touched_schedule_ids: set[str] = set()

        for job in jobs:
            if job.status_value in IN_FLIGHT_JOB_STATUSES:
                in_flight += 1
                continue
            if job.is_terminal:
                continue

            assert_job_transition(job.status_value, PublishJobStatus.CANCELLED)
            if job.task_id and self._queue.cancel(job.task_id):
                revoked_task_ids.add(job.task_id)
            self._jobs.save(
                job.evolve(
                    status=PublishJobStatus.CANCELLED,
                    error_message=reason or f"账号 {status_value}，熔断挂起",
                    next_retry_at=None,
                )
            )
            cancelled_jobs += 1
            if job.schedule_id:
                touched_schedule_ids.add(job.schedule_id)

        cancelled_schedules = 0
        for schedule in self._schedules.list_schedules(account_id=account_id):
            if schedule.is_terminal or schedule.id in in_flight_schedule_ids:
                continue

            assert_schedule_transition(schedule.status_value, ScheduleStatus.CANCELLED)
            if schedule.task_id and self._queue.cancel(schedule.task_id):
                revoked_task_ids.add(schedule.task_id)
            self._schedules.save(
                schedule.evolve(
                    status=ScheduleStatus.CANCELLED,
                    task_id=None,
                    reason=reason or f"账号 {status_value}，熔断挂起",
                )
            )
            cancelled_schedules += 1

        return CircuitResult(
            account_id=account_id,
            status=status_value,
            cancelled_jobs=cancelled_jobs,
            cancelled_schedules=cancelled_schedules,
            revoked_tasks=len(revoked_task_ids),
            in_flight_jobs=in_flight,
            reason=reason or f"账号状态变为 {status_value}，已挂起待发任务",
        )

    def on_account_status_changed(self, account_id: str, status: str) -> CircuitResult:
        """identity 状态监听回调（结构匹配 `AccountStatusListener`）。"""

        return self.suspend_account(account_id, status)

    def resume_account(self, account_id: str) -> None:
        """账号恢复时清掉熔断标记。

        **已取消的任务不会自动复活**（避免"暂停期间内容过期却又被发出去"），
        需要重发的内容由运营重新入排期。
        """

        self._suspended.pop(account_id, None)


__all__ = ["SUSPEND_STATUSES", "AccountCircuitBreaker", "CircuitResult"]
