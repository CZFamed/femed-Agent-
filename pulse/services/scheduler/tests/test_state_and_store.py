"""状态机、结果收敛与存储语义（契约 §3.2 / §3.3 / §6）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pulse.services.scheduler import (
    InMemoryJobStore,
    InMemoryScheduleStore,
    InvalidTransition,
    JobNotFound,
    PublishJobRecord,
    ScheduleRecord,
    SchedulerError,
    assert_job_transition,
    assert_schedule_transition,
    backoff_delay_s,
    coerce_publish_result,
    is_terminal_job,
    job_status_for_result,
    schedule_status_for_job,
)
from pulse.shared.enums import PublishJobStatus, ScheduleStatus
from pulse.shared.models import PublishResult

from .conftest import IST

NOW = datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc)


def _schedule(**overrides) -> ScheduleRecord:
    base = ScheduleRecord(
        id="sched_unit_01",
        variant_id="var_unit_01",
        account_id="acct_unit_01",
        timezone="Asia/Kolkata",
        scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=IST),
    )
    return base.evolve(**overrides)


def _job(**overrides) -> PublishJobRecord:
    base = PublishJobRecord(
        id="job_unit_01",
        unified_post_id="up_unit_01",
        schedule_id="sched_unit_01",
        account_id="acct_unit_01",
        platform="linkedin",
    )
    return base.evolve(**overrides)


# -- 排期状态机 -------------------------------------------------------------


def test_schedule_transitions_follow_contract():
    """pending → scheduled → publishing → published|failed；cancelled 从非终态进入。"""

    assert assert_schedule_transition("pending", "scheduled") is ScheduleStatus.SCHEDULED
    assert assert_schedule_transition("scheduled", "publishing") is ScheduleStatus.PUBLISHING
    assert assert_schedule_transition("publishing", "published") is ScheduleStatus.PUBLISHED
    assert assert_schedule_transition("publishing", "failed") is ScheduleStatus.FAILED
    assert assert_schedule_transition("scheduled", "cancelled") is ScheduleStatus.CANCELLED
    assert assert_schedule_transition("pending", "cancelled") is ScheduleStatus.CANCELLED


@pytest.mark.parametrize(
    "current, target",
    [
        ("pending", "publishing"),
        ("pending", "published"),
        ("published", "cancelled"),
        ("cancelled", "scheduled"),
        ("failed", "published"),
    ],
)
def test_schedule_transitions_rejected(current, target):
    """跳过中间态或复活终态都必须被拒绝。"""

    with pytest.raises(InvalidTransition):
        assert_schedule_transition(current, target)


# -- 发布任务状态机 ---------------------------------------------------------


def test_job_transitions_follow_contract():
    """契约 §3.3 的迁移，重点看 `pending_finalize` 与 `retrying`。"""

    assert assert_job_transition("queued", "dispatching") is PublishJobStatus.DISPATCHING
    assert (
        assert_job_transition("dispatching", "pending_finalize")
        is PublishJobStatus.PENDING_FINALIZE
    )
    assert assert_job_transition("pending_finalize", "published") is PublishJobStatus.PUBLISHED
    assert assert_job_transition("dispatching", "retrying") is PublishJobStatus.RETRYING
    assert assert_job_transition("retrying", "publishing") is PublishJobStatus.PUBLISHING
    assert assert_job_transition("failed", "retrying") is PublishJobStatus.RETRYING
    assert assert_job_transition("publishing", "rejected") is PublishJobStatus.REJECTED


@pytest.mark.parametrize(
    "current, target",
    [
        ("queued", "published"),
        ("published", "retrying"),
        ("rejected", "publishing"),
        ("cancelled", "dispatching"),
    ],
)
def test_job_transitions_rejected(current, target):
    """终态不可复活；`queued` 不能直接变 `published`（必须经过投递）。"""

    with pytest.raises(InvalidTransition):
        assert_job_transition(current, target)


def test_is_terminal_job_matches_shared_enum():
    """终态判定复用共享枚举（published/failed/rejected/cancelled）。"""

    assert is_terminal_job("published")
    assert is_terminal_job("failed")
    assert is_terminal_job("rejected")
    assert is_terminal_job("cancelled")
    assert not is_terminal_job("pending_finalize")
    assert not is_terminal_job("retrying")


# -- 结果收敛 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "result, expected",
    [
        (
            PublishResult(ok=True, status="published", platform_post_id="pf_1"),
            PublishJobStatus.PUBLISHED,
        ),
        # **受理 ≠ 发布成功**：必须进 pending_finalize
        (
            PublishResult(ok=True, status="publishing", platform_post_id="pf_2"),
            PublishJobStatus.PENDING_FINALIZE,
        ),
        (
            PublishResult(ok=True, status="pending_finalize"),
            PublishJobStatus.PENDING_FINALIZE,
        ),
        (
            PublishResult(ok=False, status="failed", error_class="policy_rejected"),
            PublishJobStatus.REJECTED,
        ),
        (
            PublishResult(ok=False, status="failed", error_class="media_processing"),
            PublishJobStatus.PENDING_FINALIZE,
        ),
        (
            PublishResult(ok=False, status="failed", error_class="rate_limited"),
            PublishJobStatus.RETRYING,
        ),
        (
            PublishResult(ok=False, status="failed", error_class="transient"),
            PublishJobStatus.RETRYING,
        ),
        (
            PublishResult(ok=False, status="failed", error_class="auth_expired"),
            PublishJobStatus.RETRYING,
        ),
        (
            PublishResult(ok=False, status="failed", error_class="validation_error"),
            PublishJobStatus.FAILED,
        ),
        (
            PublishResult(ok=False, status="failed", error_class="unknown_class"),
            PublishJobStatus.FAILED,
        ),
        (PublishResult(ok=True, status="weird"), PublishJobStatus.FAILED),
    ],
)
def test_job_status_for_result(result, expected):
    """`PublishResult` → `PublishJobStatus` 的完整映射。"""

    assert job_status_for_result(result) is expected


def test_schedule_status_for_job_mapping():
    """任务状态 → 排期状态；进行中映射到 `publishing`。"""

    assert schedule_status_for_job("published") is ScheduleStatus.PUBLISHED
    assert schedule_status_for_job("failed") is ScheduleStatus.FAILED
    assert schedule_status_for_job("rejected") is ScheduleStatus.FAILED
    assert schedule_status_for_job("cancelled") is ScheduleStatus.CANCELLED
    assert schedule_status_for_job("dispatching") is ScheduleStatus.PUBLISHING
    assert schedule_status_for_job("pending_finalize") is None


def test_coerce_publish_result_accepts_many_shapes():
    """A2 的结果对象 / 字典 / 带 `.result` 的对象都能吃进来。"""

    direct = PublishResult(ok=True, status="published")
    assert coerce_publish_result(direct) is direct

    from_dict = coerce_publish_result(
        {"ok": True, "status": "published", "platform_post_id": "pf_9"}
    )
    assert from_dict.ok is True and from_dict.platform_post_id == "pf_9"

    from_object = coerce_publish_result(
        SimpleNamespace(
            ok=False,
            status="failed",
            error_class="transient",
            error_message="boom",
            platform_post_id=None,
            post_url=None,
        )
    )
    assert from_object.error_class == "transient"

    # A2 的 DispatchOutcome 形态：状态在 .status，真正的结果在 .result
    a2_like = SimpleNamespace(
        status=PublishJobStatus.PENDING_FINALIZE,
        result=PublishResult(ok=True, status="publishing", platform_post_id="pf_a2"),
        needs_polling=True,
    )
    coerced = coerce_publish_result(a2_like)
    assert coerced.status == "publishing"
    assert job_status_for_result(coerced) is PublishJobStatus.PENDING_FINALIZE


def test_coerce_publish_result_rejects_garbage():
    """无法识别的结果类型显式报错（避免静默当成失败/成功）。"""

    with pytest.raises(SchedulerError, match="无法识别的发布结果类型"):
        coerce_publish_result("ok")


def test_backoff_delay_grows_and_caps():
    """指数退避：30 → 60 → 120 … 上限 900。"""

    assert backoff_delay_s(1) == 30.0
    assert backoff_delay_s(2) == 60.0
    assert backoff_delay_s(3) == 120.0
    assert backoff_delay_s(10) == 900.0
    with pytest.raises(ValueError, match="attempt 必须 >= 1"):
        backoff_delay_s(0)


# -- 存储 -------------------------------------------------------------------


def test_schedule_record_validation():
    """排期字段校验（ID 前缀 / 时间必须带偏移）。"""

    assert _schedule().validate() == []
    assert "sched_ 开头" in " ".join(_schedule(id="s1").validate())
    assert "时区偏移" in " ".join(
        _schedule(scheduled_at=datetime(2026, 9, 23, 9, 30)).validate()
    )
    assert "status 非法" in " ".join(_schedule(status="weird").validate())
    assert "variant_id 不能为空" in " ".join(_schedule(variant_id="").validate())
    assert "account_id 不能为空" in " ".join(_schedule(account_id="").validate())
    assert "timezone 不能为空" in " ".join(_schedule(timezone="").validate())


def test_job_record_validation():
    """发布任务字段校验（`up_` 前缀 = 幂等键）。"""

    assert _job().validate() == []
    assert "job_ 开头" in " ".join(_job(id="j1").validate())
    assert "up_ 开头" in " ".join(_job(unified_post_id="nope").validate())
    assert "status 非法" in " ".join(_job(status="weird").validate())
    assert "attempts 不能为负数" in " ".join(_job(attempts=-1).validate())


def test_job_store_unique_index_semantics():
    """`publish_jobs.unified_post_id` 唯一索引 = 幂等第一层防线。"""

    store = InMemoryJobStore()
    assert store.claim(_job()) is True
    assert store.claim(_job(id="job_unit_02")) is False, "同一 unified_post_id 不得二次占位"
    assert store.get_by_unified_post_id("up_unit_01").id == "job_unit_01"
    assert store.get_by_schedule("sched_unit_01").id == "job_unit_01"
    assert store.get("job_unit_02") is None


def test_job_store_save_requires_existing_row():
    """更新不存在的任务必须报错（防止"看起来更新了，其实没落库"）。"""

    store = InMemoryJobStore()
    with pytest.raises(JobNotFound):
        store.save(_job())

    store.claim(_job())
    updated = store.save(_job(status=PublishJobStatus.RETRYING, attempts=1))
    assert updated.status_value is PublishJobStatus.RETRYING
    assert store.get("job_unit_01").attempts == 1


def test_job_store_claim_validates_payload():
    """占位前先校验字段（脏数据不许进库）。"""

    store = InMemoryJobStore()
    with pytest.raises(ValueError, match="up_ 开头"):
        store.claim(_job(unified_post_id="bad"))


def test_job_store_filters():
    """按账号与状态集合过滤（熔断按账号定位待发任务）。"""

    store = InMemoryJobStore()
    store.claim(_job())
    store.claim(_job(id="job_unit_02", unified_post_id="up_unit_02", account_id="acct_other"))
    store.save(_job(id="job_unit_02", unified_post_id="up_unit_02", account_id="acct_other", status=PublishJobStatus.PUBLISHED))

    assert len(store.list_jobs(account_id="acct_unit_01")) == 1
    assert len(store.list_jobs(account_id="acct_other")) == 1
    assert len(store.list_jobs(statuses=(PublishJobStatus.QUEUED,))) == 1
    assert len(store.all_jobs()) == 2


def test_schedule_store_add_save_and_filters():
    """排期存储：主键冲突、更新不存在的行、按状态过滤。"""

    store = InMemoryScheduleStore()
    store.add(_schedule())

    with pytest.raises(Exception, match="主键冲突"):
        store.add(_schedule())
    with pytest.raises(Exception, match="排期不存在"):
        store.save(_schedule(id="sched_missing"))

    store.add(_schedule(id="sched_unit_02", status=ScheduleStatus.CANCELLED))
    assert [s.id for s in store.list_schedules(account_id="acct_unit_01")] == [
        "sched_unit_01",
        "sched_unit_02",
    ]
    assert [s.id for s in store.list_schedules(status="cancelled")] == ["sched_unit_02"]
    assert store.get("sched_missing") is None
    assert len(store.all_records()) == 2


def test_record_to_dict_serializes_offsets():
    """序列化后时间必须带偏移（前端/日志都不该看到裸时间）。"""

    schedule_payload = _schedule().to_dict()
    assert schedule_payload["scheduled_at"].endswith("+05:30")
    assert schedule_payload["status"] == "pending"

    job_payload = _job(
        next_retry_at=NOW + timedelta(seconds=30),
        finalize_deadline=NOW + timedelta(minutes=30),
    ).to_dict()
    assert job_payload["next_retry_at"].endswith("+00:00")
    assert job_payload["finalize_deadline"].endswith("+00:00")
    assert job_payload["unified_post_id"] == "up_unit_01"
