"""调度器：决定"什么时候、用哪个账号、以什么频率"发（契约 §3.2 / §3.3 / §5）。

投递一条排期的完整链路（每一步失败都有可读原因，且**不静默降级**）::

    pulse.schedule.enqueue {schedule_id}
        → 账号是否 active（否则熔断并拒绝）
        → 配额预检（不足则不投递）
        → 令牌桶 + 发布冷却（超限/冷却中则拒绝）
        → publish_jobs 占位（唯一索引 = 幂等第一层防线）
        → pulse.publish.dispatch {job_id, unified_post_id}，延迟用 eta / countdown

**队列只传 ID**：出口载荷恒为 `(job_id, unified_post_id)`，不含业务对象。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from pulse.services.scheduler import state as st
from pulse.services.scheduler.best_time import BestTimeTable
from pulse.services.scheduler.celery_app import (
    TASK_PUBLISH_DISPATCH,
    TASK_PUBLISH_FINALIZE,
)
from pulse.services.scheduler.circuit import AccountCircuitBreaker, CircuitResult
from pulse.services.scheduler.errors import (
    AccountUnavailable,
    InvalidSchedule,
    InvalidTransition,
    JobNotFound,
    QuotaExceeded,
    RateLimitExceeded,
    ScheduleNotFound,
    SchedulerError,
)
from pulse.services.scheduler.ports import AccountRegistry, AccountView
from pulse.services.scheduler.quota import QuotaDecision, QuotaLedger
from pulse.services.scheduler.queue import TaskQueue, TaskSubmission
from pulse.services.scheduler.ratelimit import RateDecision, RateLimiter, rate_key
from pulse.services.scheduler.store import (
    JobStore,
    PublishJobRecord,
    ScheduleRecord,
    ScheduleStore,
)
from pulse.shared.enums import PublishJobStatus, ScheduleStatus
from pulse.shared.ids import new_job_id, new_schedule_id, new_unified_post_id

LOGGER_NAME = "pulse.scheduler"

#: `pending_finalize` 的超时与轮询间隔（契约 §3.3：默认 30 分钟超时 → 告警）
DEFAULT_FINALIZE_TIMEOUT_S = 30 * 60
DEFAULT_FINALIZE_POLL_S = 5 * 60

#: 重试策略（与契约 §3.4 的"指数退避"一致）
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY_S = 30.0
DEFAULT_MAX_DELAY_S = 900.0

#: 账号状态字面量：只有 active 允许投递
ACTIVE_STATUS = "active"


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    """一次排期动作的结果（入队 / 重排 / 立即发布 / 取消）。"""

    action: str
    schedule_id: str | None = None
    job_id: str | None = None
    status: str | None = None
    task_id: str | None = None
    eta: datetime | None = None
    duplicate: bool = False
    decision: QuotaDecision | None = None
    rate: RateDecision | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化（时间带偏移）。"""

        return {
            "action": self.action,
            "schedule_id": self.schedule_id,
            "job_id": self.job_id,
            "status": self.status,
            "task_id": self.task_id,
            "eta": st.iso(self.eta),
            "duplicate": self.duplicate,
            "quota": self.decision.to_dict() if self.decision else None,
            "rate": {
                "allowed": self.rate.allowed,
                "reason": self.rate.reason,
                "retry_after_s": self.rate.retry_after_s,
            }
            if self.rate
            else None,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """一次发布结果收敛的结果。"""

    job_id: str
    unified_post_id: str
    status: PublishJobStatus
    attempts: int = 0
    schedule_status: str | None = None
    next_retry_at: datetime | None = None
    finalize_deadline: datetime | None = None
    alert: str | None = None
    duplicate: bool = False
    task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化（时间带偏移）。"""

        return {
            "job_id": self.job_id,
            "unified_post_id": self.unified_post_id,
            "status": self.status.value,
            "attempts": self.attempts,
            "schedule_status": self.schedule_status,
            "next_retry_at": st.iso(self.next_retry_at),
            "finalize_deadline": st.iso(self.finalize_deadline),
            "alert": self.alert,
            "duplicate": self.duplicate,
            "task_id": self.task_id,
        }


class Dispatcher:
    """调度器（A3 的唯一入口）。

    Args:
        accounts: 账号视图入口（identity `Account` 或测试替身）。
        schedules: `schedules` 存储。
        jobs: `publish_jobs` 存储。
        queue: 任务队列（生产为 `CeleryTaskQueue`，单测用 `EagerTaskQueue`）。
        quota: 配额台账。
        best_time: best-time 表（按 best-time 排期时必需）。
        now: 时间源（返回**带偏移**时间）。
        finalize_timeout_s: `pending_finalize` 超时秒数。
        finalize_poll_s: `pending_finalize` 轮询间隔秒数。
        max_attempts: 同一任务最大投递次数。
        base_delay_s / max_delay_s: 退避参数。
        logger: 日志器（默认 `pulse.scheduler`）。
    """

    def __init__(
        self,
        *,
        accounts: AccountRegistry,
        schedules: ScheduleStore,
        jobs: JobStore,
        queue: TaskQueue,
        quota: QuotaLedger | None = None,
        best_time: BestTimeTable | None = None,
        now: Any = None,
        finalize_timeout_s: int = DEFAULT_FINALIZE_TIMEOUT_S,
        finalize_poll_s: int = DEFAULT_FINALIZE_POLL_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay_s: float = DEFAULT_BASE_DELAY_S,
        max_delay_s: float = DEFAULT_MAX_DELAY_S,
        logger: logging.Logger | None = None,
    ) -> None:
        self._accounts = accounts
        self._schedules = schedules
        self._jobs = jobs
        self._queue = queue
        self._quota = quota or QuotaLedger()
        self._best_time = best_time
        self._now = now or st.utcnow
        self._finalize_timeout_s = int(finalize_timeout_s)
        self._finalize_poll_s = int(finalize_poll_s)
        self._max_attempts = int(max_attempts)
        self._base_delay_s = float(base_delay_s)
        self._max_delay_s = float(max_delay_s)
        self.log = logger or logging.getLogger(LOGGER_NAME)
        self._limiters: dict[str, RateLimiter] = {}
        self.breaker = AccountCircuitBreaker(
            schedules=self._schedules, jobs=self._jobs, queue=self._queue
        )

    # -- 只读属性 -----------------------------------------------------------

    @property
    def schedules(self) -> ScheduleStore:
        """排期存储。"""

        return self._schedules

    @property
    def jobs(self) -> JobStore:
        """发布任务存储。"""

        return self._jobs

    @property
    def queue(self) -> TaskQueue:
        """任务队列。"""

        return self._queue

    @property
    def quota(self) -> QuotaLedger:
        """配额台账。"""

        return self._quota

    @property
    def best_time(self) -> BestTimeTable | None:
        """best-time 表。"""

        return self._best_time

    # -- 排期创建 -----------------------------------------------------------

    def create_schedule(
        self,
        *,
        variant_id: str,
        account_id: str,
        scheduled_at: datetime | None = None,
        at_best_time: bool = False,
        actor: str = "system",
        schedule_id: str | None = None,
        now: datetime | None = None,
    ) -> ScheduleRecord:
        """新建排期（`schedules` 一行，状态 `pending`）。

        两种时间来源二选一：显式 `scheduled_at`（**必须带偏移**）或
        `at_best_time=True`（按账号时区的 best-time 表推算）。
        """

        moment = now or self._now()
        account = self._require_active_account(
            account_id, suspend=False, action="创建排期"
        )
        resolved_at, reason = self._resolve_time(
            account, scheduled_at=scheduled_at, at_best_time=at_best_time, now=moment
        )
        record = ScheduleRecord(
            id=schedule_id or new_schedule_id(),
            variant_id=variant_id,
            account_id=account.id,
            timezone=str(account.timezone),
            scheduled_at=resolved_at,
            status=ScheduleStatus.PENDING,
            reason=f"{reason}（actor={actor}）",
            created_at=moment,
            updated_at=moment,
        )
        created = self._schedules.add(record)
        self.log.info(
            "排期已创建 %s account=%s platform=%s at=%s",
            created.id,
            created.account_id,
            account.platform,
            created.scheduled_at.isoformat(),
        )
        return created

    # -- 入队 / 重排 / 取消 / 立即发布 ---------------------------------------

    def enqueue_schedule(
        self,
        schedule_id: str,
        *,
        now: datetime | None = None,
        immediate: bool = False,
        force: bool = False,
    ) -> PlanOutcome:
        """处理 `pulse.schedule.enqueue {schedule_id}`。

        Args:
            schedule_id: 排期 ID（消息载荷里**只有它**）。
            now: 时间源覆盖。
            immediate: True 时用 `countdown=0` 立即投递（对应"立即发布"）。
            force: True 时跳过配额与限流预检（管理员显式强制；**不**跳过账号状态检查）。

        Raises:
            AccountUnavailable: 账号不存在或非 `active`（同时触发熔断）。
            QuotaExceeded: 配额预检失败，**不投递**。
            RateLimitExceeded: 令牌桶不足或处于发布冷却中。
        """

        moment = now or self._now()
        schedule = self._require_schedule(schedule_id)
        existing = self._jobs.get_by_schedule(schedule.id)

        if existing is not None and not existing.is_terminal:
            return PlanOutcome(
                action="enqueue",
                schedule_id=schedule.id,
                job_id=existing.id,
                status=existing.status_value.value,
                task_id=existing.task_id,
                eta=schedule.scheduled_at,
                duplicate=True,
                reason=(
                    "该排期已有发布任务，跳过重复投递"
                    "（publish_jobs.unified_post_id 唯一索引 = 幂等第一层防线）"
                ),
            )

        if schedule.status_value not in (ScheduleStatus.PENDING, ScheduleStatus.SCHEDULED):
            raise InvalidSchedule(
                f"排期 {schedule.id} 当前状态为 {schedule.status_value.value}，不能入队"
            )

        account = self._require_active_account(schedule.account_id, suspend=True)
        decision: QuotaDecision | None = None
        rate: RateDecision | None = None
        notes: list[str] = []

        if force:
            notes.append("管理员强制投递：跳过配额与限流预检")
        else:
            decision = self._quota.reserve(
                account.id, str(account.platform), account.quota_config, now=moment
            )
            if not decision.allowed:
                self.log.warning("配额预检未通过 %s：%s", account.id, decision.reason)
                raise QuotaExceeded(
                    decision.reason,
                    decision=decision,
                    retry_after_s=decision.resets_in_s,
                )

            limiter = self._limiter_for(account)
            rate = limiter.acquire(rate_key(account.id, str(account.platform)), now=moment)
            if not rate.allowed:
                self.log.warning("限流未通过 %s：%s", account.id, rate.reason)
                raise RateLimitExceeded(rate.reason, retry_after_s=rate.retry_after_s)

        unified_post_id = (
            existing.unified_post_id if existing is not None else new_unified_post_id()
        )
        job = PublishJobRecord(
            id=new_job_id(),
            unified_post_id=unified_post_id,
            schedule_id=schedule.id,
            status=PublishJobStatus.QUEUED,
            account_id=account.id,
            platform=str(account.platform),
            created_at=moment,
            updated_at=moment,
        )
        if not self._jobs.claim(job):
            return PlanOutcome(
                action="enqueue",
                schedule_id=schedule.id,
                job_id=None,
                duplicate=True,
                decision=decision,
                rate=rate,
                reason=(
                    f"unified_post_id {unified_post_id} 已存在发布任务，"
                    "跳过重复投递（唯一索引命中）"
                ),
            )

        if immediate:
            submission = self._submit_dispatch(job, countdown=0.0)
        else:
            submission = self._submit_dispatch(job, eta=schedule.scheduled_at)

        job = self._jobs.save(job.evolve(task_id=submission.task_id, updated_at=moment))
        schedule = self._set_schedule_status(schedule, ScheduleStatus.SCHEDULED, moment)
        schedule = self._schedules.save(schedule.evolve(task_id=submission.task_id))

        notes.append(f"已投递 {TASK_PUBLISH_DISPATCH}")
        self.log.info(
            "排期已入队 %s job=%s eta=%s immediate=%s",
            schedule.id,
            job.id,
            submission.eta.isoformat() if submission.eta else None,
            immediate,
        )
        return PlanOutcome(
            action="enqueue",
            schedule_id=schedule.id,
            job_id=job.id,
            status=job.status_value.value,
            task_id=submission.task_id,
            eta=submission.eta,
            decision=decision,
            rate=rate,
            reason="；".join(notes),
        )

    def reschedule_schedule(
        self,
        schedule_id: str,
        *,
        scheduled_at: datetime | None = None,
        at_best_time: bool = False,
        actor: str = "system",
        now: datetime | None = None,
    ) -> PlanOutcome:
        """重排：撤销旧投递，按新时间重新投递（契约 §5 要求可取消可重排）。"""

        moment = now or self._now()
        schedule = self._require_schedule(schedule_id)
        if schedule.is_terminal:
            raise InvalidSchedule(
                f"排期 {schedule.id} 已是终态 {schedule.status_value.value}，不能重排"
            )

        account = self._require_active_account(schedule.account_id, suspend=True)
        resolved_at, reason = self._resolve_time(
            account, scheduled_at=scheduled_at, at_best_time=at_best_time, now=moment
        )

        revoked = 0
        if schedule.task_id and self._queue.cancel(schedule.task_id):
            revoked += 1

        schedule = self._schedules.save(
            schedule.evolve(
                scheduled_at=resolved_at,
                task_id=None,
                reason=f"重排：{reason}（actor={actor}）",
                updated_at=moment,
            )
        )

        job = self._jobs.get_by_schedule(schedule.id)
        if job is None or job.is_terminal:
            outcome = self.enqueue_schedule(schedule.id, now=moment)
            return PlanOutcome(
                action="reschedule",
                schedule_id=schedule.id,
                job_id=outcome.job_id,
                status=outcome.status,
                task_id=outcome.task_id,
                eta=outcome.eta,
                duplicate=outcome.duplicate,
                decision=outcome.decision,
                rate=outcome.rate,
                reason=f"原任务不存在或已终结，按新时间重新入队；{outcome.reason}",
            )

        submission = self._submit_dispatch(job, eta=resolved_at)
        job = self._jobs.save(
            job.evolve(task_id=submission.task_id, next_retry_at=None, updated_at=moment)
        )
        schedule = self._set_schedule_status(schedule, ScheduleStatus.SCHEDULED, moment)
        schedule = self._schedules.save(schedule.evolve(task_id=submission.task_id))

        self.log.info("排期已重排 %s → %s", schedule.id, resolved_at.isoformat())
        return PlanOutcome(
            action="reschedule",
            schedule_id=schedule.id,
            job_id=job.id,
            status=job.status_value.value,
            task_id=submission.task_id,
            eta=submission.eta,
            reason=(
                f"已撤销 {revoked} 个旧投递并按新时间重新投递（同一 unified_post_id，"
                f"不重复消耗配额）；{reason}"
            ),
        )

    def publish_now(
        self,
        schedule_id: str,
        *,
        actor: str = "system",
        force: bool = False,
        now: datetime | None = None,
    ) -> PlanOutcome:
        """立即发布（对应 `POST /api/v1/schedules/{id}/publish`）。

        `force=True` 跳过配额与限流（管理员显式操作）；账号状态检查**永不跳过**。
        """

        moment = now or self._now()
        schedule = self._require_schedule(schedule_id)
        if schedule.is_terminal:
            raise InvalidSchedule(
                f"排期 {schedule.id} 已是终态 {schedule.status_value.value}，不能立即发布"
            )

        job = self._jobs.get_by_schedule(schedule.id)
        if job is None or job.is_terminal:
            return self.enqueue_schedule(
                schedule.id, now=moment, immediate=True, force=force
            )

        self._require_active_account(schedule.account_id, suspend=True)
        revoked = 0
        if schedule.task_id and self._queue.cancel(schedule.task_id):
            revoked += 1

        submission = self._submit_dispatch(job, countdown=0.0)
        job = self._jobs.save(job.evolve(task_id=submission.task_id, updated_at=moment))
        schedule = self._set_schedule_status(schedule, ScheduleStatus.SCHEDULED, moment)
        schedule = self._schedules.save(schedule.evolve(task_id=submission.task_id))

        self.log.info("立即发布 %s job=%s actor=%s", schedule.id, job.id, actor)
        return PlanOutcome(
            action="publish_now",
            schedule_id=schedule.id,
            job_id=job.id,
            status=job.status_value.value,
            task_id=submission.task_id,
            eta=None,
            reason=(
                f"已撤销 {revoked} 个延迟投递并以 countdown=0 立即投递（actor={actor}"
                f"{'，强制跳过配额与限流' if force else ''}）"
            ),
        )

    def cancel_schedule(
        self,
        schedule_id: str,
        *,
        actor: str = "system",
        reason: str = "",
        now: datetime | None = None,
    ) -> PlanOutcome:
        """取消排期：撤销投递 + 排期与任务转 `cancelled`。"""

        moment = now or self._now()
        schedule = self._require_schedule(schedule_id)

        revoked = 0
        if schedule.task_id and self._queue.cancel(schedule.task_id):
            revoked += 1

        job = self._jobs.get_by_schedule(schedule.id)
        if job is not None and not job.is_terminal:
            st.assert_job_transition(job.status_value, PublishJobStatus.CANCELLED)
            if job.task_id and self._queue.cancel(job.task_id):
                revoked += 1
            job = self._jobs.save(
                job.evolve(
                    status=PublishJobStatus.CANCELLED,
                    next_retry_at=None,
                    error_message=reason or f"排期取消（actor={actor}）",
                    updated_at=moment,
                )
            )

        if not schedule.is_terminal:
            schedule = self._set_schedule_status(
                schedule, ScheduleStatus.CANCELLED, moment
            )
            schedule = self._schedules.save(
                schedule.evolve(
                    task_id=None, reason=reason or f"排期取消（actor={actor}）"
                )
            )

        self.log.info("排期已取消 %s 撤销投递 %d 个", schedule.id, revoked)
        return PlanOutcome(
            action="cancel",
            schedule_id=schedule.id,
            job_id=job.id if job else None,
            status=schedule.status_value.value,
            duplicate=False,
            reason=f"已撤销 {revoked} 个投递；{reason or '运营取消'}",
        )

    # -- 投递结果收敛 -------------------------------------------------------

    def mark_dispatching(
        self, job_id: str, *, now: datetime | None = None
    ) -> PublishJobRecord:
        """把任务置为 `dispatching` 并记一次尝试（投递前置动作）。"""

        moment = now or self._now()
        job = self._require_job(job_id)
        st.assert_job_transition(job.status_value, PublishJobStatus.DISPATCHING)
        return self._jobs.save(
            job.evolve(
                status=PublishJobStatus.DISPATCHING,
                attempts=job.attempts + 1,
                updated_at=moment,
            )
        )

    def apply_publish_result(
        self,
        job_id: str,
        result: Any,
        *,
        now: datetime | None = None,
    ) -> JobOutcome:
        """收敛一次发布结果（契约 §3.3）。

        * 受理但未终结 → `pending_finalize` + 排一个收敛任务（**绝不上抛 published**）；
        * 可重试错误 → `retrying` + 按指数退避重投，超过上限转 `failed` 并告警；
        * 政策拒绝 → `rejected`（终态）并告警；
        * payload 不合法 → `failed`（属代码缺陷）并告警。
        """

        moment = now or self._now()
        job = self._require_job(job_id)
        publish_result = st.coerce_publish_result(result)
        target = st.job_status_for_result(publish_result)
        st.assert_job_transition(job.status_value, target)

        if target is PublishJobStatus.PUBLISHED:
            job = self._jobs.save(
                job.evolve(
                    status=PublishJobStatus.PUBLISHED,
                    error_class=None,
                    error_message=None,
                    next_retry_at=None,
                    finalize_deadline=None,
                    updated_at=moment,
                )
            )
            schedule = self._advance_schedule(job, ScheduleStatus.PUBLISHED, moment)
            return self._job_outcome(job, schedule)

        if target is PublishJobStatus.PENDING_FINALIZE:
            deadline = moment + timedelta(seconds=self._finalize_timeout_s)
            job = self._jobs.save(
                job.evolve(
                    status=PublishJobStatus.PENDING_FINALIZE,
                    error_class=publish_result.error_class,
                    error_message=publish_result.error_message,
                    next_retry_at=None,
                    finalize_deadline=deadline,
                    updated_at=moment,
                )
            )
            schedule = self._advance_schedule(job, ScheduleStatus.PUBLISHING, moment)
            submission = self._queue.submit(
                TASK_PUBLISH_FINALIZE, job.id, countdown=self._finalize_poll_s
            )
            job = self._jobs.save(job.evolve(task_id=submission.task_id))
            self.log.info(
                "任务转 pending_finalize %s（截止 %s，%ds 后轮询）",
                job.id,
                deadline.isoformat(),
                self._finalize_poll_s,
            )
            return self._job_outcome(job, schedule)

        if target is PublishJobStatus.RETRYING:
            if job.attempts >= self._max_attempts:
                job = self._jobs.save(
                    job.evolve(
                        status=PublishJobStatus.FAILED,
                        error_class=publish_result.error_class,
                        error_message=publish_result.error_message,
                        # 终态必须清掉重试时间，否则看板会显示"还会重试"
                        next_retry_at=None,
                        finalize_deadline=None,
                        updated_at=moment,
                    )
                )
                schedule = self._advance_schedule(job, ScheduleStatus.FAILED, moment)
                outcome = self._job_outcome(job, schedule)
                return self._with_alert(
                    outcome,
                    f"已达最大投递次数 {self._max_attempts}，转人工："
                    f"{publish_result.error_message or publish_result.error_class}",
                )

            delay = st.backoff_delay_s(
                job.attempts, base_s=self._base_delay_s, max_delay_s=self._max_delay_s
            )
            next_retry = moment + timedelta(seconds=delay)
            job = self._jobs.save(
                job.evolve(
                    status=PublishJobStatus.RETRYING,
                    error_class=publish_result.error_class,
                    error_message=publish_result.error_message,
                    next_retry_at=next_retry,
                    updated_at=moment,
                )
            )
            schedule = self._advance_schedule(job, ScheduleStatus.PUBLISHING, moment)
            submission = self._submit_dispatch(job, eta=next_retry)
            job = self._jobs.save(job.evolve(task_id=submission.task_id))
            self.log.warning(
                "任务重试 %s 第 %d 次，%ss 后重投（%s）",
                job.id,
                job.attempts + 1,
                int(delay),
                publish_result.error_class,
            )
            outcome = self._job_outcome(job, schedule)
            return self._with_alert(
                outcome,
                f"可重试错误 {publish_result.error_class}："
                f"{int(delay)}s 后第 {job.attempts + 1} 次投递",
            )

        # REJECTED（政策拒绝，终态）/ FAILED（payload 不合法或未知错误）
        job = self._jobs.save(
            job.evolve(
                status=target,
                error_class=publish_result.error_class,
                error_message=publish_result.error_message,
                next_retry_at=None,
                updated_at=moment,
            )
        )
        schedule = self._advance_schedule(job, ScheduleStatus.FAILED, moment)
        outcome = self._job_outcome(job, schedule)
        if target is PublishJobStatus.REJECTED:
            return self._with_alert(
                outcome,
                "平台政策拒绝（不可重试），需人工核对内容："
                f"{publish_result.error_message or '-'}",
            )
        return self._with_alert(
            outcome,
            "不可重试失败（多为 payload 不合法，属代码缺陷）："
            f"{publish_result.error_message or publish_result.error_class}",
        )

    def finalize_pending(
        self,
        job_id: str,
        *,
        result: Any = None,
        now: datetime | None = None,
    ) -> JobOutcome:
        """处理 `pulse.publish.finalize {job_id}`。

        * 带 `result` → 直接收敛终态；
        * 不带 → 未超时则再排一次轮询；超时则**只告警**，
          既不自动置 `published`，也不自动判 `failed`（契约 §3.3）。
        """

        moment = now or self._now()
        job = self._require_job(job_id)
        if job.status_value is not PublishJobStatus.PENDING_FINALIZE:
            raise InvalidTransition(
                f"任务 {job.id} 当前状态为 {job.status_value.value}，"
                "只有 pending_finalize 需要收敛"
            )

        if result is not None:
            target = st.job_status_for_result(st.coerce_publish_result(result))
            if target is not PublishJobStatus.PENDING_FINALIZE:
                return self.apply_publish_result(job.id, result, now=moment)
            # 平台仍在处理中：保持 pending_finalize，继续轮询（不重复置状态）
            job = self._jobs.save(job.evolve(updated_at=moment))

        deadline = job.finalize_deadline
        if deadline is not None and moment >= deadline:
            schedule = self._schedules.get(job.schedule_id) if job.schedule_id else None
            outcome = self._job_outcome(job, schedule)
            return self._with_alert(
                outcome,
                f"pending_finalize 已超时（截止 {deadline.isoformat()}）："
                "需人工到平台后台核对后手工收敛（既不自动置 published，也不自动判 failed）",
            )

        submission = self._queue.submit(
            TASK_PUBLISH_FINALIZE, job.id, countdown=self._finalize_poll_s
        )
        job = self._jobs.save(job.evolve(task_id=submission.task_id, updated_at=moment))
        schedule = self._schedules.get(job.schedule_id) if job.schedule_id else None
        return self._job_outcome(job, schedule)

    def retry_job(self, job_id: str, *, now: datetime | None = None) -> JobOutcome:
        """人工重投（`failed → retrying → publishing`，契约 §3.3）。"""

        moment = now or self._now()
        job = self._require_job(job_id)
        st.assert_job_transition(job.status_value, PublishJobStatus.RETRYING)
        job = self._jobs.save(
            job.evolve(
                status=PublishJobStatus.RETRYING,
                attempts=0,
                next_retry_at=None,
                updated_at=moment,
            )
        )
        submission = self._submit_dispatch(job, countdown=0.0)
        job = self._jobs.save(job.evolve(task_id=submission.task_id))
        schedule = self._schedules.get(job.schedule_id) if job.schedule_id else None
        self.log.info("人工重投 %s", job.id)
        return self._job_outcome(job, schedule)

    # -- 熔断 ---------------------------------------------------------------

    def suspend_account(
        self, account_id: str, status: str, *, reason: str = ""
    ) -> CircuitResult:
        """挂起该账号全部待发任务（identity 状态变化的执行端）。"""

        result = self.breaker.suspend_account(account_id, status, reason=reason)
        if result.triggered:
            self.log.warning(
                "账号熔断 %s status=%s 取消任务 %d 个、排期 %d 个，在飞 %d 个",
                account_id,
                status,
                result.cancelled_jobs,
                result.cancelled_schedules,
                result.in_flight_jobs,
            )
        return result

    def on_account_status_changed(self, account_id: str, status: str) -> CircuitResult:
        """identity `AccountStatusListener` 回调。"""

        return self.suspend_account(account_id, status)

    # -- 内部工具 -----------------------------------------------------------

    def _now_or(self, now: datetime | None) -> datetime:
        return now or self._now()

    def _require_schedule(self, schedule_id: str) -> ScheduleRecord:
        record = self._schedules.get(schedule_id)
        if record is None:
            raise ScheduleNotFound(f"排期不存在：{schedule_id}")
        return record

    def _require_job(self, job_id: str) -> PublishJobRecord:
        record = self._jobs.get(job_id)
        if record is None:
            raise JobNotFound(f"发布任务不存在：{job_id}")
        return record

    def _require_account(self, account_id: str) -> AccountView:
        view = self._accounts.account_view(account_id)
        if view is None:
            raise AccountUnavailable(f"账号不存在：{account_id}")
        return view

    def _require_active_account(
        self, account_id: str, *, suspend: bool, action: str = "投递"
    ) -> AccountView:
        """账号必须是 `active`；否则（可选）触发熔断并拒绝。"""

        view = self._require_account(account_id)
        status = str(view.status)
        if status != ACTIVE_STATUS:
            if suspend:
                self.suspend_account(
                    account_id, status, reason=f"投递时账号状态为 {status}"
                )
            raise AccountUnavailable(
                f"账号 {account_id} 当前状态为 {status}，拒绝{action}"
                "（已挂起该账号全部待发任务）"
            )
        return view

    def _limiter_for(self, account: AccountView) -> RateLimiter:
        """取（或按账号配额配置创建）限流器。"""

        key = str(account.id)
        limiter = self._limiters.get(key)
        if limiter is None:
            limiter = RateLimiter.from_config(dict(account.quota_config or {}))
            self._limiters[key] = limiter
        return limiter

    def _resolve_time(
        self,
        account: AccountView,
        *,
        scheduled_at: datetime | None,
        at_best_time: bool,
        now: datetime,
    ) -> tuple[datetime, str]:
        """决定排期时刻，返回 `(带偏移时间, 可读原因)`。"""

        if scheduled_at is not None and at_best_time:
            raise InvalidSchedule("scheduled_at 与 at_best_time 只能二选一")

        if scheduled_at is not None:
            if scheduled_at.tzinfo is None:
                raise InvalidSchedule(
                    "scheduled_at 必须携带时区偏移（如 2026-09-11T03:00:00+05:30）；"
                    "无偏移会让多时区排期漂移"
                )
            return scheduled_at, "运营指定时间"

        if at_best_time:
            if self._best_time is None:
                raise InvalidSchedule("未配置 best-time 表，无法按 best-time 排期")
            resolved = self._best_time.resolve(
                str(account.id),
                str(account.platform),
                str(account.timezone),
                after=now,
            )
            return resolved.scheduled_at, resolved.reason

        raise InvalidSchedule("必须给出 scheduled_at 或 at_best_time=True")

    def _submit_dispatch(
        self,
        job: PublishJobRecord,
        *,
        eta: datetime | None = None,
        countdown: float | None = None,
    ) -> TaskSubmission:
        """投递 `pulse.publish.dispatch`。**载荷只有两个 ID**（契约 §5）。"""

        if not str(job.id).startswith("job_") or not str(job.unified_post_id).startswith("up_"):
            raise SchedulerError(
                f"队列载荷必须是 ID：job_id={job.id!r} unified_post_id={job.unified_post_id!r}"
            )
        return self._queue.submit(
            TASK_PUBLISH_DISPATCH, job.id, job.unified_post_id, eta=eta, countdown=countdown
        )

    def _set_schedule_status(
        self, schedule: ScheduleRecord, target: ScheduleStatus, moment: datetime
    ) -> ScheduleRecord:
        """迁移排期状态；必要时先经 `scheduled`（契约 §3.2 的唯一入边）。"""

        if schedule.status_value is target:
            return schedule
        if schedule.status_value is ScheduleStatus.PENDING and target in (
            ScheduleStatus.PUBLISHING,
            ScheduleStatus.PUBLISHED,
            ScheduleStatus.FAILED,
        ):
            st.assert_schedule_transition(schedule.status_value, ScheduleStatus.SCHEDULED)
            schedule = self._schedules.save(
                schedule.evolve(status=ScheduleStatus.SCHEDULED, updated_at=moment)
            )
        st.assert_schedule_transition(schedule.status_value, target)
        return self._schedules.save(schedule.evolve(status=target, updated_at=moment))

    def _advance_schedule(
        self, job: PublishJobRecord, target: ScheduleStatus, moment: datetime
    ) -> ScheduleRecord | None:
        """按发布任务状态推进其排期状态（终态排期不再改动）。"""

        if not job.schedule_id:
            return None
        schedule = self._schedules.get(job.schedule_id)
        if schedule is None or schedule.is_terminal:
            return schedule
        return self._set_schedule_status(schedule, target, moment)

    def _job_outcome(
        self, job: PublishJobRecord, schedule: ScheduleRecord | None
    ) -> JobOutcome:
        return JobOutcome(
            job_id=job.id,
            unified_post_id=job.unified_post_id,
            status=job.status_value,
            attempts=job.attempts,
            schedule_status=schedule.status_value.value if schedule else None,
            next_retry_at=job.next_retry_at,
            finalize_deadline=job.finalize_deadline,
            task_id=job.task_id,
        )

    @staticmethod
    def _with_alert(outcome: JobOutcome, alert: str | None) -> JobOutcome:
        return JobOutcome(
            job_id=outcome.job_id,
            unified_post_id=outcome.unified_post_id,
            status=outcome.status,
            attempts=outcome.attempts,
            schedule_status=outcome.schedule_status,
            next_retry_at=outcome.next_retry_at,
            finalize_deadline=outcome.finalize_deadline,
            alert=alert,
            duplicate=outcome.duplicate,
            task_id=outcome.task_id,
        )

    def quota_snapshot(self, account_id: str, *, now: datetime | None = None) -> Mapping[str, Any]:
        """账号的配额快照（供 API/看板展示）。"""

        moment = self._now_or(now)
        account = self._require_account(account_id)
        decision = self._quota.precheck(
            account.id, str(account.platform), account.quota_config, now=moment
        )
        limiter = self._limiter_for(account)
        rate = limiter.check(rate_key(account.id, str(account.platform)), now=moment)
        return {
            "quota": decision.to_dict(),
            "rate": {
                "allowed": rate.allowed,
                "reason": rate.reason,
                "retry_after_s": rate.retry_after_s,
                "remaining_tokens": rate.remaining_tokens,
            },
        }


__all__ = [
    "ACTIVE_STATUS",
    "DEFAULT_BASE_DELAY_S",
    "DEFAULT_FINALIZE_POLL_S",
    "DEFAULT_FINALIZE_TIMEOUT_S",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DELAY_S",
    "LOGGER_NAME",
    "Dispatcher",
    "JobOutcome",
    "PlanOutcome",
]
