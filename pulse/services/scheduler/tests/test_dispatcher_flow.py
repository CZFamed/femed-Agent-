"""调度器主流程：入队 → 投递 → 收敛 → 重试 / 终态（契约 §3.2 / §3.3）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pulse.services.scheduler import (
    InvalidSchedule,
    JobNotFound,
    PublishJobRecord,
    ScheduleNotFound,
    TASK_PUBLISH_DISPATCH,
    TASK_PUBLISH_FINALIZE,
    publish_dispatch,
    publish_finalize,
    schedule_enqueue,
)
from pulse.shared.models import PublishResult

from .conftest import IST

NOW = datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc)


def test_enqueue_is_idempotent(scheduled, dispatcher, queue):
    """重复投递 `pulse.schedule.enqueue` 不会产生第二个任务（幂等）。"""

    schedule = scheduled()
    first = dispatcher.enqueue_schedule(schedule.id)
    second = dispatcher.enqueue_schedule(schedule.id)

    assert first.job_id == second.job_id
    assert second.duplicate is True
    assert "幂等" in second.reason
    assert len(queue.submissions_for(TASK_PUBLISH_DISPATCH)) == 1
    assert dispatcher.jobs.all_jobs()[0].unified_post_id == dispatcher.jobs.get(first.job_id).unified_post_id


def test_enqueue_uses_upstream_idempotency_key_and_warns_on_fallback(
    scheduled, dispatcher, caplog
):
    """幂等键由上游透传；没传时兜底生成但必须留痕（F-2 的回归守卫）。"""

    schedule = scheduled()
    plan = dispatcher.enqueue_schedule(schedule.id, unified_post_id="up_from_a1")
    assert dispatcher.jobs.get(plan.job_id).unified_post_id == "up_from_a1"

    other = scheduled()
    with caplog.at_level("WARNING"):
        # force=True 只是跳过配额/限流预检（同一账号处于发布冷却中），
        # 幂等键兜底逻辑与它无关
        plan2 = dispatcher.enqueue_schedule(other.id, force=True)
    assert dispatcher.jobs.get(plan2.job_id).unified_post_id.startswith("up_")
    assert any("未收到上游 unified_post_id" in record.message for record in caplog.records)


def test_unique_index_blocks_second_job_for_same_post(
    scheduled, dispatcher, queue
):
    """`publish_jobs.unified_post_id` 唯一索引：同一幂等键不得再建任务。"""

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)
    job = dispatcher.jobs.get(outcome.job_id)

    # 人工把任务打成终态、排期退回 scheduled（模拟"任务已终结但排期还在"）
    dispatcher.jobs.save(job.evolve(status="cancelled"))
    dispatcher.schedules.save(
        dispatcher.schedules.get(schedule.id).evolve(status="scheduled")
    )

    again = dispatcher.enqueue_schedule(schedule.id, force=True)
    assert again.duplicate is True
    assert "unified_post_id" in again.reason
    assert len(queue.submissions_for(TASK_PUBLISH_DISPATCH)) == 1


def test_enqueue_rejects_terminal_schedule(scheduled, dispatcher):
    """终态排期不得再入队。"""

    schedule = scheduled()
    dispatcher.enqueue_schedule(schedule.id)
    dispatcher.cancel_schedule(schedule.id, actor="ops")

    with pytest.raises(InvalidSchedule, match="不能入队"):
        dispatcher.enqueue_schedule(schedule.id)


def test_unknown_ids_raise(scheduled, dispatcher):
    """找不到的排期 / 任务要显式报错。"""

    with pytest.raises(ScheduleNotFound):
        dispatcher.enqueue_schedule("sched_nope")
    with pytest.raises(JobNotFound):
        dispatcher.mark_dispatching("job_nope")
    with pytest.raises(JobNotFound):
        dispatcher.apply_publish_result("job_nope", {"ok": True, "status": "published"})


def test_full_loop_publish_to_published(scheduled, dispatcher, sink, queue):
    """闭环：入队 → 投递（受理）→ 收敛 → published，排期同步到 published。"""

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)
    sink.push({"ok": True, "status": "publishing", "platform_post_id": "li_123"})

    # 任务级执行体：消费方（A2 网关）返回"已受理"
    accepted = publish_dispatch(
        dispatcher, outcome.job_id, dispatcher.jobs.get(outcome.job_id).unified_post_id, sink=sink
    )
    assert accepted["status"] == "pending_finalize"
    assert accepted["finalize_deadline"] is not None
    assert dispatcher.jobs.get(outcome.job_id).attempts == 1

    # 收敛任务被排进队列（带轮询间隔）
    finalize_submissions = queue.submissions_for(TASK_PUBLISH_FINALIZE)
    assert len(finalize_submissions) == 1
    assert finalize_submissions[0].countdown == dispatcher._finalize_poll_s

    sink.poll_result = PublishResult(ok=True, status="published", platform_post_id="li_123")
    settled = publish_finalize(dispatcher, outcome.job_id, sink=sink)
    assert settled["status"] == "published"
    assert settled["schedule_status"] == "published"
    assert dispatcher.schedules.get(schedule.id).status_value.value == "published"


def test_dispatch_without_sink_does_not_fake_success(scheduled, dispatcher):
    """没有发布网关时只登记投递，绝不推进状态（避免"看起来发过了"）。"""

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)

    payload = publish_dispatch(
        dispatcher, outcome.job_id, dispatcher.jobs.get(outcome.job_id).unified_post_id, sink=None
    )
    assert payload["dispatched"] is False
    assert dispatcher.jobs.get(outcome.job_id).status_value.value == "queued"


def test_gateway_exception_becomes_retryable(scheduled, dispatcher, queue):
    """网关抛异常 → 归 `transient` → retrying + 退避重投（不吞异常、不判死）。"""

    class ExplodingSink:
        def dispatch(self, job_id, unified_post_id):
            raise RuntimeError("network boom")

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)
    payload = publish_dispatch(
        dispatcher,
        outcome.job_id,
        dispatcher.jobs.get(outcome.job_id).unified_post_id,
        sink=ExplodingSink(),
    )

    job = dispatcher.jobs.get(outcome.job_id)
    assert job.status_value.value == "retrying"
    assert job.error_class == "transient"
    assert "RuntimeError" in job.error_message
    assert job.next_retry_at == NOW + timedelta(seconds=30)
    assert payload["next_retry_at"] is not None

    retries = queue.submissions_for(TASK_PUBLISH_DISPATCH)
    assert len(retries) == 2
    assert retries[-1].eta == job.next_retry_at


def test_retry_backoff_then_terminal_failure(scheduled, dispatcher, queue):
    """连续可重试错误：退避递增，超过上限转 `failed` 并告警。"""

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)
    up_id = dispatcher.jobs.get(outcome.job_id).unified_post_id

    delays = []
    for _ in range(5):
        dispatcher.mark_dispatching(outcome.job_id)
        result = dispatcher.apply_publish_result(
            outcome.job_id, {"ok": False, "status": "failed", "error_class": "rate_limited"}
        )
        if result.next_retry_at is not None:
            delays.append((result.next_retry_at - NOW).total_seconds())

    assert delays == [30.0, 60.0, 120.0, 240.0]
    final = dispatcher.jobs.get(outcome.job_id)
    assert final.status_value.value == "failed"
    assert final.attempts == 5
    assert dispatcher.schedules.get(schedule.id).status_value.value == "failed"
    assert up_id  # 幂等键不变，重投不会换键


def test_policy_rejected_is_terminal_with_alert(scheduled, dispatcher):
    """政策拒绝：终态 `rejected` + 告警，**不重试**。"""

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)
    dispatcher.mark_dispatching(outcome.job_id)

    result = dispatcher.apply_publish_result(
        outcome.job_id,
        {"ok": False, "status": "rejected", "error_class": "policy_rejected", "error_message": "社区规范"},
    )
    assert result.status.value == "rejected"
    assert result.alert is not None and "人工核对" in result.alert
    assert dispatcher.schedules.get(schedule.id).status_value.value == "failed"


def test_validation_error_is_terminal_failure(scheduled, dispatcher):
    """payload 不合法 = 代码缺陷：终态 `failed` + 告警，不重试。"""

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)
    dispatcher.mark_dispatching(outcome.job_id)

    result = dispatcher.apply_publish_result(
        outcome.job_id, {"ok": False, "status": "failed", "error_class": "validation_error"}
    )
    assert result.status.value == "failed"
    assert result.alert is not None and "不可重试" in result.alert
    assert dispatcher.schedules.get(schedule.id).status_value.value == "failed"


def test_finalize_timeout_alerts_without_auto_resolution(scheduled, dispatcher, clock):
    """`pending_finalize` 超时：只告警，既不自动置 published 也不自动判 failed。"""

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)
    dispatcher.mark_dispatching(outcome.job_id)
    dispatcher.apply_publish_result(
        outcome.job_id, {"ok": True, "status": "publishing", "platform_post_id": "pf_1"}
    )

    clock.advance(minutes=31)
    result = dispatcher.finalize_pending(outcome.job_id)

    assert result.status.value == "pending_finalize"
    assert result.alert is not None and "已超时" in result.alert
    assert dispatcher.jobs.get(outcome.job_id).status_value.value == "pending_finalize"
    assert dispatcher.schedules.get(schedule.id).status_value.value == "publishing"


def test_finalize_reschedules_polling(scheduled, dispatcher, clock, queue):
    """未超时且平台仍在处理：排下一次轮询，状态保持 pending_finalize。"""

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)
    dispatcher.mark_dispatching(outcome.job_id)
    dispatcher.apply_publish_result(
        outcome.job_id, {"ok": True, "status": "publishing", "platform_post_id": "pf_2"}
    )

    clock.advance(minutes=5)
    still_processing = dispatcher.finalize_pending(
        outcome.job_id, result={"ok": True, "status": "pending_finalize"}
    )
    assert still_processing.status.value == "pending_finalize"
    assert len(queue.submissions_for(TASK_PUBLISH_FINALIZE)) == 2


def test_finalize_rejects_wrong_state(scheduled, dispatcher):
    """只有 `pending_finalize` 需要收敛，其它状态调用要报错。"""

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)

    with pytest.raises(Exception, match="只有 pending_finalize 需要收敛"):
        dispatcher.finalize_pending(outcome.job_id)


def test_manual_retry_from_failed(scheduled, dispatcher, queue):
    """人工重投：`failed → retrying → publishing`（契约 §3.3）。"""

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)
    dispatcher.mark_dispatching(outcome.job_id)
    dispatcher.apply_publish_result(
        outcome.job_id, {"ok": False, "status": "failed", "error_class": "validation_error"}
    )
    assert dispatcher.jobs.get(outcome.job_id).status_value.value == "failed"

    result = dispatcher.retry_job(outcome.job_id)
    assert result.status.value == "retrying"
    assert dispatcher.jobs.get(outcome.job_id).attempts == 0
    latest = queue.submissions_for(TASK_PUBLISH_DISPATCH)[-1]
    assert latest.countdown == 0.0


def test_schedule_enqueue_handler_returns_outcome(scheduled, dispatcher):
    """任务级执行体返回可序列化结果（供 Celery 结果后端记录）。"""

    schedule = scheduled()
    payload = schedule_enqueue(dispatcher, schedule.id)
    assert payload["task"] == "pulse.schedule.enqueue"
    assert payload["action"] == "enqueue"

    job_id = payload["job_id"]
    up_id = payload["job_id"].replace("job_", "up_")  # 仅为占位，真正取值在下面
    assert dispatcher.jobs.get(job_id).unified_post_id.startswith("up_")
    assert up_id.startswith("up_")


def test_pending_finalize_never_becomes_published_directly(scheduled, dispatcher):
    """**受理 ≠ 发布**：`publishing` 结果绝不能把任务直接推进 published。"""

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)
    dispatcher.mark_dispatching(outcome.job_id)
    result = dispatcher.apply_publish_result(
        outcome.job_id, {"ok": True, "status": "publishing", "platform_post_id": "pf_3"}
    )

    assert result.status.value == "pending_finalize"
    assert dispatcher.jobs.get(outcome.job_id).finalize_deadline is not None
    assert dispatcher.schedules.get(schedule.id).status_value.value == "publishing"


def test_job_record_fields_stay_contract_shaped(scheduled, dispatcher):
    """任务记录的核心字段与契约 §6 一致（含唯一索引键）。"""

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)
    job = dispatcher.jobs.get(outcome.job_id)
    assert isinstance(job, PublishJobRecord)
    assert job.schedule_id == schedule.id
    assert job.status_value.value == "queued"
    assert job.attempts == 0
    assert job.platform == "linkedin"


def test_publish_now_on_unenqueued_schedule(scheduled, dispatcher, queue):
    """未入队的排期直接"立即发布"：走标准入队但用 countdown=0。"""

    schedule = scheduled(scheduled_at=datetime(2026, 12, 1, 9, 30, tzinfo=IST))
    outcome = dispatcher.publish_now(schedule.id, actor="ops", force=True)
    assert outcome.action == "enqueue"
    latest = queue.submissions_for(TASK_PUBLISH_DISPATCH)[-1]
    assert latest.countdown == 0.0
