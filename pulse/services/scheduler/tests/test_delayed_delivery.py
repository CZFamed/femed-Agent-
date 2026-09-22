"""延迟投递 / 取消 / 重排（派工单 §3 W1-A3-2）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pulse.services.scheduler import (
    TASK_PUBLISH_DISPATCH,
    CeleryTaskQueue,
    EagerTaskQueue,
    QueueError,
    build_app,
)

from .conftest import IST

#: scheduler 域源码目录（用于"禁用 cron"的静态守卫）
_DOMAIN_DIR = Path(__file__).resolve().parent.parent


def test_enqueue_uses_eta_and_ids_only(scheduled, dispatcher, queue):
    """入队用 `eta` 延迟投递，载荷只有 `(job_id, unified_post_id)`。"""

    schedule = scheduled(scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=IST))
    assert dispatcher.enqueue_schedule(schedule.id).action == "enqueue"

    submissions = queue.submissions_for(TASK_PUBLISH_DISPATCH)
    assert len(submissions) == 1
    submission = submissions[0]
    assert submission.countdown is None
    assert submission.eta == schedule.scheduled_at
    assert submission.eta.tzinfo is not None

    job_id, unified_post_id = submission.payload
    assert job_id.startswith("job_")
    assert unified_post_id.startswith("up_")
    assert len(submission.payload) == 2, "载荷只能是两个 ID，不能塞业务对象"


def test_publish_now_uses_countdown_zero(scheduled, dispatcher, queue):
    """"立即发布"用 `countdown=0`，而不是 `eta`。"""

    schedule = scheduled(scheduled_at=datetime(2026, 12, 1, 9, 30, tzinfo=IST))
    dispatcher.enqueue_schedule(schedule.id)

    outcome = dispatcher.publish_now(schedule.id, actor="ops")
    assert outcome.action == "publish_now"

    latest = queue.submissions_for(TASK_PUBLISH_DISPATCH)[-1]
    assert latest.countdown == 0.0
    assert latest.eta is None


def test_cancel_revokes_task_and_marks_records(scheduled, dispatcher, queue):
    """取消排期：撤销队列任务 + 排期与任务转 cancelled。"""

    schedule = scheduled()
    outcome = dispatcher.enqueue_schedule(schedule.id)
    assert outcome.task_id in [s.task_id for s in queue.pending()]

    job_id = outcome.job_id
    cancelled = dispatcher.cancel_schedule(schedule.id, actor="ops", reason="内容需重审")

    assert cancelled.action == "cancel"
    assert dispatcher.schedules.get(schedule.id).status_value.value == "cancelled"
    assert dispatcher.jobs.get(job_id).status_value.value == "cancelled"
    assert outcome.task_id in queue.cancelled_ids()


def test_reschedule_revokes_old_and_books_new_eta(scheduled, dispatcher, queue):
    """重排：旧投递被撤销，新投递按新时间；同一 `unified_post_id`（幂等不重复）。"""

    schedule = scheduled(scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=IST))
    first = dispatcher.enqueue_schedule(schedule.id)
    original_up = dispatcher.jobs.get(first.job_id).unified_post_id

    new_time = datetime(2026, 9, 25, 14, 0, tzinfo=IST)
    outcome = dispatcher.reschedule_schedule(schedule.id, scheduled_at=new_time, actor="ops")

    assert outcome.action == "reschedule"
    assert first.task_id in queue.cancelled_ids()
    assert outcome.task_id != first.task_id
    assert outcome.eta == new_time
    assert dispatcher.schedules.get(schedule.id).scheduled_at == new_time
    assert dispatcher.jobs.get(first.job_id).unified_post_id == original_up

    latest = queue.submissions_for(TASK_PUBLISH_DISPATCH)[-1]
    assert latest.eta == new_time
    assert latest.rescheduled_from is None  # 由 Dispatcher 重新投递，不是队列内部重排


def test_reschedule_rejects_terminal_schedule(scheduled, dispatcher):
    """终态排期不能被重排（避免"取消后又自己发出去"）。"""

    schedule = scheduled()
    dispatcher.enqueue_schedule(schedule.id)
    dispatcher.cancel_schedule(schedule.id, actor="ops")

    with pytest.raises(Exception, match="不能重排"):
        dispatcher.reschedule_schedule(
            schedule.id, scheduled_at=datetime(2026, 9, 26, 9, 0, tzinfo=IST)
        )


def test_queue_rejects_conflicting_timing(queue):
    """`eta` 与 `countdown` 只能二选一；`eta` 必须带偏移。"""

    with pytest.raises(QueueError, match="只能二选一"):
        queue.submit(TASK_PUBLISH_DISPATCH, "job_1", "up_1", eta=datetime.now(), countdown=5)
    with pytest.raises(QueueError, match="时区偏移"):
        queue.submit(TASK_PUBLISH_DISPATCH, "job_1", "up_1", eta=datetime(2026, 9, 23, 9, 0))
    with pytest.raises(QueueError, match="不能为负数"):
        queue.submit(TASK_PUBLISH_DISPATCH, "job_1", "up_1", countdown=-1)
    with pytest.raises(QueueError, match="task_name 不能为空"):
        queue.submit("")


def test_eager_queue_reschedule_and_guards(queue):
    """假队列的重排语义：新 ID + `rescheduled_from`；已取消任务不能重排。"""

    submitted = queue.submit(TASK_PUBLISH_DISPATCH, "job_1", "up_1", countdown=10)
    replacement = queue.reschedule(submitted.task_id, countdown=60)

    assert replacement.task_id != submitted.task_id
    assert replacement.rescheduled_from == submitted.task_id
    assert replacement.payload == ("job_1", "up_1")
    assert submitted.task_id in queue.cancelled_ids()
    assert [s.task_id for s in queue.pending()] == [replacement.task_id]

    with pytest.raises(QueueError, match="任务不存在"):
        queue.reschedule("no-such-task", countdown=1)
    with pytest.raises(QueueError, match="已取消"):
        queue.reschedule(replacement.rescheduled_from, countdown=1)


def test_celery_queue_submits_and_revokes():
    """真实队列实现：投递返回 task_id，取消走 revoke（不打断正在执行的任务）。"""

    app = build_app()
    celery_queue = CeleryTaskQueue(app)
    submission = celery_queue.submit(
        TASK_PUBLISH_DISPATCH,
        "job_abc",
        "up_abc",
        countdown=30,
        task_id="fixed-task-id",
    )

    assert submission.task_id == "fixed-task-id"
    assert submission.queue == "pulse"
    assert celery_queue.app is app
    assert celery_queue.cancel("fixed-task-id") is True

    with pytest.raises(QueueError, match="需要原始载荷"):
        celery_queue.reschedule("fixed-task-id", countdown=5)


def test_domain_never_uses_cron_scheduling():
    """**禁用 cron 硬排**（契约 §5）：源码里不得出现 crontab / beat 调度表。"""

    offenders = []
    for path in _DOMAIN_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ("crontab(", "beat_schedule", "CELERYBEAT_SCHEDULE"):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], f"排期必须用 eta/countdown，发现 cron 痕迹：{offenders}"


def test_eager_queue_records_order(queue):
    """假队列保留投递顺序（便于断言"先撤后投"）。"""

    queue.submit(TASK_PUBLISH_DISPATCH, "job_1", "up_1", countdown=1)
    queue.submit(TASK_PUBLISH_DISPATCH, "job_2", "up_2", countdown=2)
    assert [s.payload[0] for s in queue.pending()] == ["job_1", "job_2"]
    assert len(queue.all_submissions()) == 2


def test_queue_isolated_between_tests(queue):
    """每个用例拿到全新的假队列（不串状态）。"""

    assert queue.all_submissions() == ()
    assert isinstance(queue, EagerTaskQueue)
    assert queue.pending() == ()
