"""账号停用熔断（派工单 §3 W1-A3-9）。"""

from __future__ import annotations

from datetime import datetime

from pulse.services.scheduler import (
    AccountCircuitBreaker,
    AccountUnavailable,
    EagerTaskQueue,
    InMemoryJobStore,
    InMemoryScheduleStore,
    PublishJobRecord,
    ScheduleRecord,
    TASK_PUBLISH_DISPATCH,
)

from .conftest import IST


def _records(queue: EagerTaskQueue, count: int = 1, *, account_id: str = "acct_cb"):
    """造 `count` 条"已入队待发"的排期 + 任务，返回 (schedules, jobs, 队列任务 ID)。"""

    schedules = InMemoryScheduleStore()
    jobs = InMemoryJobStore()
    task_ids = []

    for index in range(count):
        schedule_id = f"sched_cb_{index}"
        job_id = f"job_cb_{index}"
        submission = queue.submit(
            TASK_PUBLISH_DISPATCH,
            job_id,
            f"up_cb_{index}",
            eta=datetime(2026, 9, 23, 9, 30, tzinfo=IST),
        )
        schedules.add(
            ScheduleRecord(
                id=schedule_id,
                variant_id=f"var_cb_{index}",
                account_id=account_id,
                timezone="Asia/Kolkata",
                scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=IST),
                status="scheduled",
                task_id=submission.task_id,
            )
        )
        jobs.claim(
            PublishJobRecord(
                id=job_id,
                unified_post_id=f"up_cb_{index}",
                schedule_id=schedule_id,
                account_id=account_id,
                platform="linkedin",
                task_id=submission.task_id,
            )
        )
        task_ids.append(submission.task_id)

    return schedules, jobs, task_ids


def test_pause_cancels_pending_jobs_and_schedules():
    """`paused` → 全部待发任务置 cancelled 且撤销队列任务。"""

    queue = EagerTaskQueue()
    schedules, jobs, task_ids = _records(queue, 2)
    breaker = AccountCircuitBreaker(schedules=schedules, jobs=jobs, queue=queue)

    result = breaker.suspend_account("acct_cb", "paused", reason="合规复核")

    assert result.triggered is True
    assert result.cancelled_jobs == 2
    assert result.cancelled_schedules == 2
    assert result.revoked_tasks == 2
    assert all(j.status_value.value == "cancelled" for j in jobs.all_jobs())
    assert all(s.status_value.value == "cancelled" for s in schedules.all_records())
    assert set(queue.cancelled_ids()) == set(task_ids)
    assert queue.pending() == ()
    assert breaker.is_suspended("acct_cb") is True
    assert breaker.suspended_accounts() == ("acct_cb",)


def test_revoke_status_also_triggers():
    """`revoked` 与 `paused` 一样触发熔断。"""

    queue = EagerTaskQueue()
    schedules, jobs, _ = _records(queue)
    breaker = AccountCircuitBreaker(schedules=schedules, jobs=jobs, queue=queue)

    result = breaker.suspend_account("acct_cb", "revoked", reason="凭据泄露")
    assert result.triggered is True
    assert result.cancelled_jobs == 1
    assert result.status == "revoked"
    assert result.reason == "凭据泄露"


def test_active_status_is_noop():
    """`active` 不是熔断条件：不取消任何东西（也用于账号恢复时的回调）。"""

    queue = EagerTaskQueue()
    schedules, jobs, _ = _records(queue)
    breaker = AccountCircuitBreaker(schedules=schedules, jobs=jobs, queue=queue)

    result = breaker.suspend_account("acct_cb", "active")
    assert result.triggered is False
    assert result.cancelled_jobs == 0
    assert jobs.all_jobs()[0].status_value.value == "queued"
    assert breaker.is_suspended("acct_cb") is False


def test_in_flight_jobs_are_not_claimed_as_cancelled():
    """已经"在飞"的任务不能被声称撤销（平台可能已经发出去了）。"""

    queue = EagerTaskQueue()
    schedules, jobs, _ = _records(queue, 1)
    jobs.save(jobs.get("job_cb_0").evolve(status="pending_finalize"))
    breaker = AccountCircuitBreaker(schedules=schedules, jobs=jobs, queue=queue)

    result = breaker.suspend_account("acct_cb", "paused", reason="人工暂停")

    assert result.in_flight_jobs == 1
    assert result.cancelled_jobs == 0
    assert result.cancelled_schedules == 0, "在飞的排期保持 publishing，不误标取消"
    assert jobs.get("job_cb_0").status_value.value == "pending_finalize"
    assert schedules.get("sched_cb_0").status_value.value == "scheduled"


def test_terminal_records_are_untouched():
    """终态任务与排期不参与熔断（published 的贴子不会被"取消"）。"""

    queue = EagerTaskQueue()
    schedules, jobs, _ = _records(queue, 1)
    jobs.save(jobs.get("job_cb_0").evolve(status="published"))
    schedules.save(schedules.get("sched_cb_0").evolve(status="published"))
    breaker = AccountCircuitBreaker(schedules=schedules, jobs=jobs, queue=queue)

    result = breaker.suspend_account("acct_cb", "paused")
    assert result.cancelled_jobs == 0
    assert result.in_flight_jobs == 0
    assert jobs.get("job_cb_0").status_value.value == "published"


def test_other_accounts_are_not_affected():
    """熔断只影响目标账号。"""

    queue = EagerTaskQueue()
    schedules, jobs, _ = _records(queue, 1, account_id="acct_cb")
    _, other_jobs, _ = _records(EagerTaskQueue(), 1, account_id="acct_other")
    for job in other_jobs.all_jobs():
        jobs.claim(job)

    breaker = AccountCircuitBreaker(schedules=schedules, jobs=jobs, queue=queue)
    breaker.suspend_account("acct_cb", "paused")

    assert jobs.get("job_cb_0").status_value.value == "cancelled"
    assert jobs.get("job_cb_0") is not None
    assert all(j.status_value.value == "queued" for j in jobs.list_jobs(account_id="acct_other"))


def test_resume_clears_flag_but_does_not_revive_jobs():
    """恢复账号只清标记；已取消的任务不会自动复活。"""

    queue = EagerTaskQueue()
    schedules, jobs, _ = _records(queue, 1)
    breaker = AccountCircuitBreaker(schedules=schedules, jobs=jobs, queue=queue)
    breaker.suspend_account("acct_cb", "paused")

    breaker.resume_account("acct_cb")
    assert breaker.is_suspended("acct_cb") is False
    assert jobs.get("job_cb_0").status_value.value == "cancelled"


def test_dispatcher_suspend_account_delegates_to_breaker(dispatcher, registry, scheduled):
    """`Dispatcher.suspend_account` / `on_account_status_changed` 走同一熔断器。"""

    schedule = scheduled(account_id="acct_linkedin_01")
    outcome = dispatcher.enqueue_schedule(schedule.id)

    result = dispatcher.suspend_account("acct_linkedin_01", "paused", reason="人工暂停")
    assert result.cancelled_jobs == 1
    assert dispatcher.jobs.get(outcome.job_id).status_value.value == "cancelled"

    dispatcher.breaker.resume_account("acct_linkedin_01")
    direct = dispatcher.on_account_status_changed("acct_linkedin_01", "revoked")
    assert direct.triggered is True


def test_enqueue_rejected_for_paused_account_and_melts_down(
    dispatcher, registry, scheduled, queue
):
    """投递时账号已暂停：拒绝投递 + 挂起其余待发任务（不静默放行）。"""

    first = scheduled(account_id="acct_linkedin_01", variant_id="var_p1")
    ok = dispatcher.enqueue_schedule(first.id)
    assert ok.action == "enqueue"

    # 第二条排期（还没入队）→ 账号被暂停
    second = scheduled(account_id="acct_linkedin_01", variant_id="var_p2")
    registry.set_status("acct_linkedin_01", "paused")

    try:
        dispatcher.enqueue_schedule(second.id)
    except AccountUnavailable as exc:
        assert "拒绝投递" in str(exc)
    else:  # pragma: no cover - 必须抛
        raise AssertionError("账号暂停时必须拒绝投递")

    assert dispatcher.jobs.get(ok.job_id).status_value.value == "cancelled"
    assert dispatcher.schedules.get(first.id).status_value.value == "cancelled"
    assert ok.task_id in queue.cancelled_ids()
    assert dispatcher.jobs.get_by_schedule(second.id) is None


def test_enqueue_rejected_for_missing_account(dispatcher, scheduled):
    """账号不存在也拒绝（数据不完整时不允许盲发）。"""

    schedule = scheduled(account_id="acct_linkedin_01")
    dispatcher._accounts.rows.clear()

    try:
        dispatcher.enqueue_schedule(schedule.id)
    except AccountUnavailable as exc:
        assert "账号不存在" in str(exc)
    else:  # pragma: no cover - 必须抛
        raise AssertionError("账号不存在时必须拒绝投递")
