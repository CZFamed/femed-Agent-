"""Celery 任务装配（契约 §5）：把处理器绑到冻结的任务名上。

任务函数本身只做两件事：**校验载荷是 ID** + 转交 `Dispatcher`。
业务状态一律从库里取（队列只传 ID），因此重投天然幂等。
"""

from __future__ import annotations

from typing import Any

from pulse.services.scheduler.celery_app import (
    ALL_TASK_NAMES,
    TASK_METRICS_FETCH,
    TASK_PUBLISH_DISPATCH,
    TASK_PUBLISH_FINALIZE,
    TASK_SCHEDULE_ENQUEUE,
    build_app,
)
from pulse.services.scheduler.dispatcher import Dispatcher
from pulse.services.scheduler.errors import InvalidSchedule
from pulse.services.scheduler.ports import PublishDispatchSink
from pulse.services.scheduler.queue import TaskQueue
from pulse.shared.models import PublishResult


def require_id(value: Any, prefix: str, field: str) -> str:
    """校验队列载荷里的 ID（契约 §5：队列只传 ID）。"""

    if not isinstance(value, str) or not value.startswith(prefix):
        raise InvalidSchedule(
            f"{field} 必须是 {prefix} 前缀的 ID（队列只传 ID，不传业务对象），收到 {value!r}"
        )
    return value


def schedule_enqueue(dispatcher: Dispatcher, schedule_id: str) -> dict[str, Any]:
    """`pulse.schedule.enqueue {schedule_id}` 的执行体。"""

    sid = require_id(schedule_id, "sched_", "schedule_id")
    outcome = dispatcher.enqueue_schedule(sid)
    return {"task": TASK_SCHEDULE_ENQUEUE, **outcome.to_dict()}


def publish_dispatch(
    dispatcher: Dispatcher,
    job_id: str,
    unified_post_id: str,
    *,
    sink: PublishDispatchSink | None = None,
) -> dict[str, Any]:
    """`pulse.publish.dispatch {job_id, unified_post_id}` 的执行体。

    消费方是 A2 发布网关（`sink`）。W1 阶段没有网关时只登记投递，
    不做任何状态推进，避免制造"看起来发过了"的假象。
    """

    jid = require_id(job_id, "job_", "job_id")
    upid = require_id(unified_post_id, "up_", "unified_post_id")

    if sink is None:
        return {
            "task": TASK_PUBLISH_DISPATCH,
            "job_id": jid,
            "unified_post_id": upid,
            "dispatched": False,
            "reason": "未注入发布网关（sink），仅登记投递，未推进状态",
        }

    dispatcher.mark_dispatching(jid)
    try:
        result: Any = sink.dispatch(jid, upid)
    except Exception as exc:  # noqa: BLE001 - 网关异常统一归为可重试
        # 只带异常类型名：异常文本可能包含平台响应细节，不做透传
        result = PublishResult(
            ok=False,
            status="failed",
            error_class="transient",
            error_message=f"发布网关调用异常：{type(exc).__name__}",
        )

    outcome = dispatcher.apply_publish_result(jid, result)
    return {"task": TASK_PUBLISH_DISPATCH, "dispatched": True, **outcome.to_dict()}


def publish_finalize(
    dispatcher: Dispatcher,
    job_id: str,
    *,
    sink: PublishDispatchSink | None = None,
) -> dict[str, Any]:
    """`pulse.publish.finalize {job_id}` 的执行体（收敛 `pending_finalize`）。"""

    jid = require_id(job_id, "job_", "job_id")

    poll = getattr(sink, "poll_finalize", None) if sink is not None else None
    if poll is None:
        outcome = dispatcher.finalize_pending(jid)
        return {"task": TASK_PUBLISH_FINALIZE, "polled": False, **outcome.to_dict()}

    try:
        result: Any = poll(jid)
    except Exception as exc:  # noqa: BLE001 - 轮询失败继续等下一次
        outcome = dispatcher.finalize_pending(jid)
        return {
            "task": TASK_PUBLISH_FINALIZE,
            "polled": True,
            "poll_error": type(exc).__name__,
            **outcome.to_dict(),
        }

    outcome = dispatcher.finalize_pending(jid, result=result)
    return {"task": TASK_PUBLISH_FINALIZE, "polled": True, **outcome.to_dict()}


def metrics_fetch(dispatcher: Dispatcher, job_id: str) -> dict[str, Any]:
    """`pulse.metrics.fetch {job_id}`（P1：数据回捞，M1 阶段只登记）。"""

    jid = require_id(job_id, "job_", "job_id")
    return {
        "task": TASK_METRICS_FETCH,
        "job_id": jid,
        "status": "skipped",
        "reason": "FR-8 数据回捞属 P1，M1 阶段不实现（任务名按契约 §5 保留）",
    }


def register_tasks(
    app: Any,
    *,
    dispatcher: Dispatcher,
    sink: PublishDispatchSink | None = None,
) -> tuple[str, ...]:
    """把四个任务名绑到 Celery app 上；返回已注册的任务名。

    刻意使用 `shared=False`：Celery 的"共享任务"会通过 finalize 钩子挂到**每个** app 上，
    于是同一进程里第二次 `build_app()` 时，任务会解析到**第一次**注册的闭包
    （曾实测：两个 app 都跑 old dispatcher 的代码）。任务与 app 的依赖是实例绑定的，
    必须 app 级注册。
    """

    @app.task(name=TASK_SCHEDULE_ENQUEUE, shared=False)
    def _schedule_enqueue(schedule_id: str) -> dict[str, Any]:
        """pulse.schedule.enqueue(schedule_id)"""

        return schedule_enqueue(dispatcher, schedule_id)

    @app.task(name=TASK_PUBLISH_DISPATCH, shared=False)
    def _publish_dispatch(job_id: str, unified_post_id: str) -> dict[str, Any]:
        """pulse.publish.dispatch(job_id, unified_post_id)"""

        return publish_dispatch(dispatcher, job_id, unified_post_id, sink=sink)

    @app.task(name=TASK_PUBLISH_FINALIZE, shared=False)
    def _publish_finalize(job_id: str) -> dict[str, Any]:
        """pulse.publish.finalize(job_id)"""

        return publish_finalize(dispatcher, job_id, sink=sink)

    @app.task(name=TASK_METRICS_FETCH, shared=False)
    def _metrics_fetch(job_id: str) -> dict[str, Any]:
        """pulse.metrics.fetch(job_id)"""

        return metrics_fetch(dispatcher, job_id)

    return ALL_TASK_NAMES


def build_wired_app(
    *,
    dispatcher: Dispatcher,
    sink: PublishDispatchSink | None = None,
    queue: TaskQueue | None = None,
    eager: bool = True,
    **app_kwargs: Any,
) -> Any:
    """构造 app 并注册任务（eager 测试模式的便捷入口）。"""

    app = build_app(eager=eager, **app_kwargs)
    register_tasks(app, dispatcher=dispatcher, sink=sink)
    if queue is not None and hasattr(queue, "_app"):  # pragma: no cover - 接线便利
        queue._app = app
    return app


__all__ = [
    "build_wired_app",
    "metrics_fetch",
    "publish_dispatch",
    "publish_finalize",
    "register_tasks",
    "require_id",
    "schedule_enqueue",
]
