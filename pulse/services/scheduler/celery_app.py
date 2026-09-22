"""Celery + Redis 接线（契约 §5），**含 eager 测试模式**。

派工单 §3 W1-A3-1 的完成定义是"单测不依赖真实 Redis"，所以：

* 默认 broker 用 `memory://`（进程内），默认 **eager**：任务调用即同步执行；
* 需要真实 Redis 时由部署层设 `PULSE_CELERY_EAGER=0` + `PULSE_CELERY_BROKER=redis://…`。

任务名按契约 §5 冻结，全部以 `pulse.` 开头（`AGENTS.md` §7）。
"""

from __future__ import annotations

import os
from typing import Any

from celery import Celery

# -- 契约 §5 的任务名（冻结，勿改） ---------------------------------------
TASK_SCHEDULE_ENQUEUE = "pulse.schedule.enqueue"
TASK_PUBLISH_DISPATCH = "pulse.publish.dispatch"
TASK_PUBLISH_FINALIZE = "pulse.publish.finalize"
TASK_METRICS_FETCH = "pulse.metrics.fetch"

#: 全部已接线任务（按契约顺序）
ALL_TASK_NAMES: tuple[str, ...] = (
    TASK_SCHEDULE_ENQUEUE,
    TASK_PUBLISH_DISPATCH,
    TASK_PUBLISH_FINALIZE,
    TASK_METRICS_FETCH,
)

#: 默认队列名与地址（进程内，不产生网络连接）
DEFAULT_QUEUE = "pulse"
DEFAULT_BROKER_URL = "memory://"
DEFAULT_BACKEND_URL = "cache+memory://"

#: 环境变量名（部署层用）
ENV_BROKER = "PULSE_CELERY_BROKER"
ENV_BACKEND = "PULSE_CELERY_BACKEND"
ENV_EAGER = "PULSE_CELERY_EAGER"
ENV_QUEUE = "PULSE_CELERY_QUEUE"


def _env_flag(name: str, default: bool) -> bool:
    """读布尔环境变量（`0` / `false` / `no` / `off` 视为假）。"""

    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def build_app(
    *,
    name: str = "pulse",
    broker_url: str | None = None,
    backend_url: str | None = None,
    eager: bool = True,
    queue: str = DEFAULT_QUEUE,
    timezone_name: str = "UTC",
) -> Celery:
    """构造 Celery app。

    Args:
        name: app 名（默认 `pulse`）。
        broker_url: broker 地址；缺省 `memory://`（进程内，不连网络）。
        backend_url: 结果后端；缺省内存 cache 后端。
        eager: 是否 eager（同步执行）。**单测必须为 True。**
        queue: 默认队列名。
        timezone_name: broker 侧时区（任务时间统一按带偏移的 `eta` 传递）。
    """

    app = Celery(
        name,
        broker=broker_url or DEFAULT_BROKER_URL,
        backend=backend_url or DEFAULT_BACKEND_URL,
    )
    app.conf.update(
        task_always_eager=eager,
        # eager 下让异常直接冒泡：否则测试会看到"绿"，生产才炸
        task_eager_propagates=eager,
        # eager 也把结果存下来，`.get()` 才拿得到返回值
        task_store_eager_result=eager,
        task_default_queue=queue,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        enable_utc=True,
        timezone=timezone_name,
        # W1 阶段不因为连不上 broker 而重试到超时
        broker_connection_retry_on_startup=False,
    )
    return app


def app_from_env() -> Celery:
    """按环境变量构造 app（部署接线入口）。"""

    return build_app(
        broker_url=os.environ.get(ENV_BROKER) or DEFAULT_BROKER_URL,
        backend_url=os.environ.get(ENV_BACKEND) or DEFAULT_BACKEND_URL,
        eager=_env_flag(ENV_EAGER, True),
        queue=os.environ.get(ENV_QUEUE) or DEFAULT_QUEUE,
    )


def is_eager(app: Any) -> bool:
    """该 app 是否处于 eager 模式。"""

    return bool(app.conf.task_always_eager)


__all__ = [
    "ALL_TASK_NAMES",
    "DEFAULT_BACKEND_URL",
    "DEFAULT_BROKER_URL",
    "DEFAULT_QUEUE",
    "ENV_BACKEND",
    "ENV_BROKER",
    "ENV_EAGER",
    "ENV_QUEUE",
    "TASK_METRICS_FETCH",
    "TASK_PUBLISH_DISPATCH",
    "TASK_PUBLISH_FINALIZE",
    "TASK_SCHEDULE_ENQUEUE",
    "app_from_env",
    "build_app",
    "is_eager",
]
