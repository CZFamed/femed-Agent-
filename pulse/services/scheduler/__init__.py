"""A3 scheduler 域（调度与限流 / 配额）。

对外只有几样东西：

* `Dispatcher` —— 入排期、入队、重排、取消、结果收敛（契约 §3.2 / §3.3）
* `QuotaLedger` / `RateLimiter` —— 配额预检与令牌桶限流（派工单 §3 W1-A3-3/4）
* `BestTimeTable` —— best-time 查询（返回**带偏移**的 `scheduled_at`）
* `EagerTaskQueue` / `CeleryTaskQueue` + `build_app` —— eager 可测的 Celery 接线
* `AccountCircuitBreaker` —— 账号停用 → 即时挂起待发任务

契约：`pulse/contracts/INTERFACES.md` §3.2 / §3.3 / §5 / §6。
本域**不 import** identity（各自独立可测），接线见 `identity_adapter.py`。
"""

from pulse.services.scheduler.best_time import (
    DEFAULT_LOCAL_TIME,
    FALLBACK_SLOT_NAME,
    BestTimeSlot,
    BestTimeTable,
    ResolvedSlot,
)
from pulse.services.scheduler.celery_app import (
    ALL_TASK_NAMES,
    DEFAULT_BROKER_URL,
    DEFAULT_QUEUE,
    TASK_METRICS_FETCH,
    TASK_PUBLISH_DISPATCH,
    TASK_PUBLISH_FINALIZE,
    TASK_SCHEDULE_ENQUEUE,
    app_from_env,
    build_app,
    is_eager,
)
from pulse.services.scheduler.circuit import (
    SUSPEND_STATUSES,
    AccountCircuitBreaker,
    CircuitResult,
)
from pulse.services.scheduler.dispatcher import (
    DEFAULT_FINALIZE_POLL_S,
    DEFAULT_FINALIZE_TIMEOUT_S,
    DEFAULT_MAX_ATTEMPTS,
    Dispatcher,
    JobOutcome,
    PlanOutcome,
)
from pulse.services.scheduler.errors import (
    AccountUnavailable,
    CircuitOpen,
    DuplicateJob,
    DuplicateSchedule,
    InvalidSchedule,
    InvalidTransition,
    JobNotFound,
    QueueError,
    QuotaExceeded,
    RateLimitExceeded,
    ScheduleNotFound,
    SchedulerError,
)
from pulse.services.scheduler.ports import AccountRegistry, AccountView, PublishDispatchSink
from pulse.services.scheduler.quota import (
    DEFAULT_DAILY_LIMIT,
    DEFAULT_UNIT_COSTS,
    QuotaDecision,
    QuotaLedger,
    QuotaPolicy,
)
from pulse.services.scheduler.queue import (
    CeleryTaskQueue,
    EagerTaskQueue,
    TaskQueue,
    TaskSubmission,
    new_task_id,
)
from pulse.services.scheduler.ratelimit import (
    CooldownTracker,
    RateConfig,
    RateDecision,
    RateLimiter,
    TokenBucket,
    rate_key,
)
from pulse.services.scheduler.state import (
    ALLOWED_JOB_TRANSITIONS,
    ALLOWED_SCHEDULE_TRANSITIONS,
    assert_job_transition,
    assert_schedule_transition,
    backoff_delay_s,
    coerce_publish_result,
    is_terminal_job,
    job_status_for_result,
    schedule_status_for_job,
)
from pulse.services.scheduler.store import (
    InMemoryJobStore,
    InMemoryScheduleStore,
    JobStore,
    PublishJobRecord,
    ScheduleRecord,
    ScheduleStore,
)
from pulse.services.scheduler.tasks import (
    metrics_fetch,
    publish_dispatch,
    publish_finalize,
    register_tasks,
    require_id,
    schedule_enqueue,
)
from pulse.services.scheduler.timezones import (
    UnknownTimezone,
    is_valid_timezone,
    resolve_timezone,
)

__all__ = [
    "ALLOWED_JOB_TRANSITIONS",
    "ALLOWED_SCHEDULE_TRANSITIONS",
    "ALL_TASK_NAMES",
    "DEFAULT_BROKER_URL",
    "DEFAULT_DAILY_LIMIT",
    "DEFAULT_FINALIZE_POLL_S",
    "DEFAULT_FINALIZE_TIMEOUT_S",
    "DEFAULT_LOCAL_TIME",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_QUEUE",
    "DEFAULT_UNIT_COSTS",
    "FALLBACK_SLOT_NAME",
    "SUSPEND_STATUSES",
    "TASK_METRICS_FETCH",
    "TASK_PUBLISH_DISPATCH",
    "TASK_PUBLISH_FINALIZE",
    "TASK_SCHEDULE_ENQUEUE",
    "AccountCircuitBreaker",
    "AccountRegistry",
    "AccountUnavailable",
    "AccountView",
    "BestTimeSlot",
    "BestTimeTable",
    "CeleryTaskQueue",
    "CircuitOpen",
    "CircuitResult",
    "CooldownTracker",
    "Dispatcher",
    "DuplicateJob",
    "DuplicateSchedule",
    "EagerTaskQueue",
    "InMemoryJobStore",
    "InMemoryScheduleStore",
    "InvalidSchedule",
    "InvalidTransition",
    "JobNotFound",
    "JobOutcome",
    "JobStore",
    "PlanOutcome",
    "PublishDispatchSink",
    "PublishJobRecord",
    "QuotaDecision",
    "QuotaExceeded",
    "QuotaLedger",
    "QuotaPolicy",
    "QueueError",
    "RateConfig",
    "RateDecision",
    "RateLimitExceeded",
    "RateLimiter",
    "ResolvedSlot",
    "ScheduleNotFound",
    "ScheduleRecord",
    "ScheduleStore",
    "SchedulerError",
    "TaskQueue",
    "TaskSubmission",
    "TokenBucket",
    "UnknownTimezone",
    "app_from_env",
    "assert_job_transition",
    "assert_schedule_transition",
    "backoff_delay_s",
    "build_app",
    "coerce_publish_result",
    "is_eager",
    "is_terminal_job",
    "is_valid_timezone",
    "job_status_for_result",
    "metrics_fetch",
    "new_task_id",
    "publish_dispatch",
    "publish_finalize",
    "rate_key",
    "register_tasks",
    "require_id",
    "resolve_timezone",
    "schedule_enqueue",
    "schedule_status_for_job",
]
