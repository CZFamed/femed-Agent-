"""Celery + Redis 接线与 eager 测试模式（派工单 §3 W1-A3-1）。"""

from __future__ import annotations

import pytest

from pulse.services.scheduler import (
    ALL_TASK_NAMES,
    DEFAULT_BROKER_URL,
    DEFAULT_QUEUE,
    Dispatcher,
    EagerTaskQueue,
    InMemoryJobStore,
    InMemoryScheduleStore,
    InvalidSchedule,
    TASK_METRICS_FETCH,
    TASK_PUBLISH_DISPATCH,
    TASK_SCHEDULE_ENQUEUE,
    app_from_env,
    build_app,
    is_eager,
    register_tasks,
)
from pulse.services.scheduler.celery_app import (
    ENV_BROKER,
    ENV_EAGER,
    TASK_PUBLISH_FINALIZE,
)

from .conftest import FakeRegistry


def test_task_names_are_frozen_by_contract():
    """任务名按契约 §5 冻结（改名即破坏跨域接口）。"""

    assert ALL_TASK_NAMES == (
        "pulse.schedule.enqueue",
        "pulse.publish.dispatch",
        "pulse.publish.finalize",
        "pulse.metrics.fetch",
    )
    assert TASK_SCHEDULE_ENQUEUE == "pulse.schedule.enqueue"
    assert TASK_PUBLISH_DISPATCH == "pulse.publish.dispatch"
    assert TASK_PUBLISH_FINALIZE == "pulse.publish.finalize"
    assert TASK_METRICS_FETCH == "pulse.metrics.fetch"


def test_default_app_is_eager_and_needs_no_redis():
    """默认 app 处于 eager 模式，broker 是进程内地址（不连网络）。"""

    app = build_app()
    assert is_eager(app) is True
    assert app.conf.broker_url == DEFAULT_BROKER_URL == "memory://"
    assert app.conf.task_default_queue == DEFAULT_QUEUE
    assert app.conf.task_serializer == "json"


def test_register_tasks_exposes_all_names(dispatcher):
    """四个任务名都会被注册到 app 上。"""

    app = build_app()
    registered = register_tasks(app, dispatcher=dispatcher)
    assert registered == ALL_TASK_NAMES
    for name in ALL_TASK_NAMES:
        assert name in app.tasks, f"{name} 未注册"


def test_eager_task_executes_synchronously(scheduled, dispatcher):
    """eager 下任务同步执行并返回结果（单测不需要 Redis / worker）。"""

    app = build_app()
    register_tasks(app, dispatcher=dispatcher)
    schedule = scheduled()

    result = app.tasks[TASK_SCHEDULE_ENQUEUE].delay(schedule.id)
    payload = result.get()

    assert payload["action"] == "enqueue"
    assert payload["schedule_id"] == schedule.id
    assert payload["job_id"].startswith("job_")
    assert dispatcher.schedules.get(schedule.id).status_value.value == "scheduled"


def test_eager_task_propagates_exceptions(scheduled, dispatcher):
    """eager 模式下异常必须冒泡，否则测试会"假绿"。"""

    app = build_app()
    register_tasks(app, dispatcher=dispatcher)
    # 关掉传播就变成静默失败，这里刻意断言默认行为是"抛出来"
    with pytest.raises(Exception):
        app.tasks[TASK_SCHEDULE_ENQUEUE].delay("not-a-schedule-id")


def test_payload_must_be_ids(scheduled, dispatcher, sink):
    """队列只传 ID：业务对象（如整条文案）必须被拒绝。"""

    app = build_app()
    register_tasks(app, dispatcher=dispatcher, sink=sink)

    with pytest.raises(InvalidSchedule, match="job_id 必须是 job_ 前缀"):
        app.tasks[TASK_PUBLISH_DISPATCH].delay("up_123", "up_456")
    with pytest.raises(InvalidSchedule, match="unified_post_id 必须是 up_ 前缀"):
        app.tasks[TASK_PUBLISH_DISPATCH].delay("job_123", {"caption": "不管传什么都拒绝"})


def test_env_driven_app(monkeypatch):
    """部署层可用环境变量切到真实 Redis（此处只验证接线，不真的连）。"""

    monkeypatch.setenv(ENV_BROKER, "redis://localhost:6379/0")
    monkeypatch.setenv(ENV_EAGER, "0")
    app = app_from_env()
    assert is_eager(app) is False
    assert app.conf.broker_url == "redis://localhost:6379/0"

    monkeypatch.setenv(ENV_EAGER, "true")
    assert is_eager(app_from_env()) is True


def test_metrics_task_is_registered_but_skipped(dispatcher):
    """P1 的数据回捞任务名保留，M1 阶段显式跳过（不假装实现）。"""

    app = build_app()
    register_tasks(app, dispatcher=dispatcher)
    payload = app.tasks[TASK_METRICS_FETCH].delay("job_not_real").get()
    assert payload["status"] == "skipped"
    assert "P1" in payload["reason"]


def test_two_apps_do_not_share_task_closures(scheduled, dispatcher):
    """同一进程里建两个 app，各自的任务必须绑各自的 dispatcher。

    回归用例：Celery 的"共享任务"（`shared=True`）会挂到每个 app 上，
    导致第二个 app 跑的是第一个 app 的闭包 —— 表现为"排期凭空不存在"。
    """

    empty_queue = EagerTaskQueue()
    empty_schedules = InMemoryScheduleStore()
    other = Dispatcher(
        accounts=FakeRegistry(),
        schedules=empty_schedules,
        jobs=InMemoryJobStore(),
        queue=empty_queue,
    )

    app_a = build_app()
    register_tasks(app_a, dispatcher=dispatcher)
    app_b = build_app()
    register_tasks(app_b, dispatcher=other)

    assert app_a.tasks[TASK_SCHEDULE_ENQUEUE] is not app_b.tasks[TASK_SCHEDULE_ENQUEUE]

    schedule = scheduled()
    payload_a = app_a.tasks[TASK_SCHEDULE_ENQUEUE].delay(schedule.id).get()
    assert payload_a["schedule_id"] == schedule.id

    # 第二个 app 用另一套存储，看不到第一个 app 的排期 → 必须报"不存在"，
    # 而不是错误地去操作第一个 app 的存储（那才是真正的串台）。
    with pytest.raises(Exception, match="排期不存在"):
        app_b.tasks[TASK_SCHEDULE_ENQUEUE].delay(schedule.id).get()
