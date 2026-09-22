"""任务队列抽象：延迟投递 / 取消 / 重排（契约 §5）。

**禁止 cron 硬排**：排期一律用 `eta` / `countdown`，因为运营需要能随时取消与重排，
而 cron 里的任务没有"这一条"的身份，取消只能停整张表。

本模块给出两种实现：

* `EagerTaskQueue` —— 记录式假队列（单测用，**不连 Redis**）；
* `CeleryTaskQueue` —— 真实投递（`apply_async` / `revoke`），由部署层注入 Celery app。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable
from uuid import uuid4

from pulse.services.scheduler.errors import QueueError


def new_task_id() -> str:
    """Celery 风格的 32 位十六进制任务 ID。"""

    return uuid4().hex


def _validate_timing(eta: datetime | None, countdown: float | None) -> None:
    """`eta` 与 `countdown` 只能二选一；`eta` 必须带偏移。"""

    if eta is not None and countdown is not None:
        raise QueueError("eta 与 countdown 只能二选一（同时给出会让延迟语义不确定）")
    if eta is not None and eta.tzinfo is None:
        raise QueueError("eta 必须携带时区偏移（无偏移的 eta 会按 worker 本地时区解释，排期漂移）")
    if countdown is not None and countdown < 0:
        raise QueueError(f"countdown 不能为负数：{countdown}")


@dataclass(frozen=True, slots=True)
class TaskSubmission:
    """一次投递的完整描述（队列里"这一条任务"的身份）。"""

    task_id: str
    task_name: str
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    eta: datetime | None = None
    countdown: float | None = None
    queue: str | None = None
    rescheduled_from: str | None = None

    @property
    def payload(self) -> tuple[Any, ...]:
        """队列载荷。**只允许是 ID**（契约 §5：队列只传 ID，不传业务对象）。"""

        return self.args


@runtime_checkable
class TaskQueue(Protocol):
    """队列协议。"""

    def submit(
        self,
        task_name: str,
        *args: Any,
        eta: datetime | None = None,
        countdown: float | None = None,
        kwargs: Mapping[str, Any] | None = None,
        task_id: str | None = None,
        queue: str | None = None,
    ) -> TaskSubmission:
        """投递任务（延迟用 `eta` 或 `countdown`）。"""

    def cancel(self, task_id: str) -> bool:
        """取消任务；返回是否取消成功。"""

    def reschedule(
        self,
        task_id: str,
        *,
        eta: datetime | None = None,
        countdown: float | None = None,
    ) -> TaskSubmission:
        """重排任务：撤销旧投递，按新时间重新投递。"""


class EagerTaskQueue:
    """记录式假队列（单测与本地演示用）。

    只记录"投递了什么"，不执行任何真实投递，因此单测**不需要 Redis**。
    `reschedule` 保留 `rescheduled_from`，方便断言"重排 = 撤销旧任务 + 新任务"。
    """

    def __init__(self, *, queue: str = "pulse") -> None:
        self._queue = queue
        self._submissions: dict[str, TaskSubmission] = {}
        self._order: list[str] = []
        self._cancelled: set[str] = set()

    def submit(
        self,
        task_name: str,
        *args: Any,
        eta: datetime | None = None,
        countdown: float | None = None,
        kwargs: Mapping[str, Any] | None = None,
        task_id: str | None = None,
        queue: str | None = None,
    ) -> TaskSubmission:
        """记录一次投递。"""

        if not task_name:
            raise QueueError("task_name 不能为空")
        _validate_timing(eta, countdown)
        tid = task_id or new_task_id()
        submission = TaskSubmission(
            task_id=tid,
            task_name=task_name,
            args=tuple(args),
            kwargs=dict(kwargs or {}),
            eta=eta,
            countdown=countdown,
            queue=queue or self._queue,
        )
        self._submissions[tid] = submission
        self._order.append(tid)
        self._cancelled.discard(tid)
        return submission

    def cancel(self, task_id: str) -> bool:
        """标记取消；任务不存在时返回 False。"""

        if task_id not in self._submissions:
            return False
        self._cancelled.add(task_id)
        return True

    def reschedule(
        self,
        task_id: str,
        *,
        eta: datetime | None = None,
        countdown: float | None = None,
    ) -> TaskSubmission:
        """重排：撤销旧任务并用新 ID 重新投递。"""

        original = self._submissions.get(task_id)
        if original is None:
            raise QueueError(f"任务不存在，无法重排：{task_id}")
        if task_id in self._cancelled:
            raise QueueError(f"任务已取消，不能重排（请重新投递）：{task_id}")

        self._cancelled.add(task_id)
        replacement = self.submit(
            original.task_name,
            *original.args,
            eta=eta,
            countdown=countdown,
            kwargs=original.kwargs,
            queue=original.queue,
        )
        self._submissions[replacement.task_id] = TaskSubmission(
            task_id=replacement.task_id,
            task_name=replacement.task_name,
            args=replacement.args,
            kwargs=replacement.kwargs,
            eta=replacement.eta,
            countdown=replacement.countdown,
            queue=replacement.queue,
            rescheduled_from=task_id,
        )
        return self._submissions[replacement.task_id]

    # -- 供测试断言 ---------------------------------------------------------

    def pending(self) -> tuple[TaskSubmission, ...]:
        """尚未取消的投递（按投递顺序）。"""

        return tuple(
            self._submissions[tid] for tid in self._order if tid not in self._cancelled
        )

    def cancelled_ids(self) -> tuple[str, ...]:
        """已取消的任务 ID。"""

        return tuple(tid for tid in self._order if tid in self._cancelled)

    def all_submissions(self) -> tuple[TaskSubmission, ...]:
        """全部投递记录（含已取消）。"""

        return tuple(self._submissions[tid] for tid in self._order)

    def submissions_for(self, task_name: str) -> tuple[TaskSubmission, ...]:
        """按任务名过滤的投递记录。"""

        return tuple(s for s in self.all_submissions() if s.task_name == task_name)


class CeleryTaskQueue:
    """基于 Celery 的真实队列（`eta` / `countdown` / `revoke`）。

    重排刻意**换新任务 ID**：Celery 对同一 ID 有去重语义，
    复用 ID 会让"重排后的任务"在某些 broker 上被静默吞掉。
    """

    def __init__(self, app: Any = None, *, default_queue: str = "pulse") -> None:
        if app is None:  # pragma: no cover - 需要 celery 已安装
            from pulse.services.scheduler.celery_app import build_app

            app = build_app()
        self._app = app
        self._default_queue = default_queue

    @property
    def app(self) -> Any:
        """底层 Celery app。"""

        return self._app

    def submit(
        self,
        task_name: str,
        *args: Any,
        eta: datetime | None = None,
        countdown: float | None = None,
        kwargs: Mapping[str, Any] | None = None,
        task_id: str | None = None,
        queue: str | None = None,
    ) -> TaskSubmission:
        """投递任务（`apply_async` 语义）。

        注意：`send_task` **不受** `task_always_eager` 影响（Celery 会给出
        "task_always_eager has no effect on send_task" 警告），因此本方法只负责
        "把消息投出去"，不等执行结果；eager 执行由 `app.tasks[...].delay()` 负责。
        """

        _validate_timing(eta, countdown)
        tid = task_id or new_task_id()
        queue_name = queue or self._default_queue
        self._app.send_task(
            task_name,
            args=list(args),
            kwargs=dict(kwargs or {}),
            eta=eta,
            countdown=countdown,
            task_id=tid,
            queue=queue_name,
        )
        return TaskSubmission(
            task_id=tid,
            task_name=task_name,
            args=tuple(args),
            kwargs=dict(kwargs or {}),
            eta=eta,
            countdown=countdown,
            queue=queue_name,
        )

    def cancel(self, task_id: str) -> bool:
        """撤销任务（`terminate=False`：不打断正在执行的发布，避免半成品）。"""

        self._app.control.revoke(task_id, terminate=False)
        return True

    def reschedule(
        self,
        task_id: str,
        *,
        eta: datetime | None = None,
        countdown: float | None = None,
    ) -> TaskSubmission:
        """重排：撤销旧任务 + 以新 ID 重新投递（载荷沿用旧任务）。"""

        raise QueueError(
            "CeleryTaskQueue.reschedule 需要原始载荷：请用 submit() 重新投递，"
            "或改用 Dispatcher.reschedule_schedule()（它知道载荷）"
        )


__all__ = [
    "CeleryTaskQueue",
    "EagerTaskQueue",
    "TaskQueue",
    "TaskSubmission",
    "new_task_id",
]
