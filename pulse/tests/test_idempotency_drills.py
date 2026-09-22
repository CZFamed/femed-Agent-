"""W2-A6-4 · 幂等演练：重复投递不产生重复发布。

契约 §6「幂等保证」写了**两层**防线，本文件分别演练，再合起来演练一遍：

* 第一层：``publish_jobs.unified_post_id`` 唯一索引语义
  （A3 的 ``InMemoryJobStore.claim`` / A2 的 ``InMemoryPublishStore.claim``）；
* 第二层：Adapter 的 ``find_existing()`` 平台侧兜底。

判定口径统一为**数平台调用次数**：重复投递后 ``publish_calls_count`` 必须还是 1。
"""

from __future__ import annotations

from datetime import timedelta

from pulse.services.publish import FakeAdapter, PublishGateway
from pulse.services.publish.adapters import FakeBehaviour
from pulse.services.publish.store import InMemoryPublishStore
from pulse.services.scheduler.celery_app import TASK_PUBLISH_DISPATCH
from pulse.services.scheduler.store import PublishJobRecord
from pulse.shared.enums import PublishJobStatus

from pulse.tests.conftest import CredentialStub, make_post, run_async


# --------------------------------------------------------------------------
# 第一层：唯一索引
# --------------------------------------------------------------------------


def test_publish_gateway_dedupes_on_the_idempotency_key(clock):
    """重复投递同一条 ``UnifiedPost`` → 第二次是 duplicate，平台只被调用一次。"""
    adapter = FakeAdapter("linkedin", FakeBehaviour.PENDING_THEN_PUBLISHED)
    gateway = PublishGateway(adapters=[adapter], now=clock)
    post = make_post()

    first = run_async(gateway.dispatch(post, CredentialStub(), job_id="job_a6_idem_1"))
    second = run_async(gateway.dispatch(post, CredentialStub(), job_id="job_a6_idem_1"))

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.status is PublishJobStatus.PENDING_FINALIZE
    assert adapter.publish_calls_count == 1, "重复投递不得产生第二次平台发布"
    assert gateway.store.result_writes(post.unified_post_id) == 1, "publish_results 只应落一条"


def test_gateway_dedupe_survives_a_process_restart(clock):
    """幂等键存在存储里，不在 Adapter 实例里：换一个网关实例仍能识别重复。"""
    store = InMemoryPublishStore()
    first_adapter = FakeAdapter("linkedin", FakeBehaviour.PENDING_THEN_PUBLISHED)
    first_gateway = PublishGateway(adapters=[first_adapter], store=store, now=clock)
    post = make_post()
    run_async(first_gateway.dispatch(post, CredentialStub(), job_id="job_a6_restart"))

    # 模拟进程重启：新的网关 + 新的 Adapter，但沿用同一份存储
    restarted_adapter = FakeAdapter("linkedin", FakeBehaviour.PENDING_THEN_PUBLISHED)
    restarted = PublishGateway(adapters=[restarted_adapter], store=store, now=clock)
    replay = run_async(restarted.dispatch(post, CredentialStub(), job_id="job_a6_restart"))

    assert replay.duplicate is True
    assert restarted_adapter.publish_calls_count == 0


def test_scheduler_job_store_unique_index_blocks_second_job(dispatcher, jobs, clock, monkeypatch):
    """A3 侧：同一个 ``up_`` 已存在任务行时，再次入队被唯一索引挡下。"""
    monkeypatch.setattr(
        "pulse.services.scheduler.dispatcher.new_unified_post_id",
        lambda: "up_a6_preclaimed",
    )
    pre_claimed = PublishJobRecord(
        id="job_a6_preclaimed",
        unified_post_id="up_a6_preclaimed",
        schedule_id="sched_a6_other",
        status=PublishJobStatus.QUEUED,
        account_id="acct_a6_linkedin",
        platform="linkedin",
        created_at=clock(),
        updated_at=clock(),
    )
    assert jobs.claim(pre_claimed) is True
    assert jobs.claim(pre_claimed.evolve(id="job_a6_other")) is False, "唯一索引必须按键判重"

    schedule = dispatcher.create_schedule(
        variant_id="var_a6_uniq",
        account_id="acct_a6_linkedin",
        scheduled_at=clock() + timedelta(days=1),
    )
    outcome = dispatcher.enqueue_schedule(schedule.id)

    assert outcome.duplicate is True
    assert outcome.job_id is None, "被唯一索引挡下时不得新建任务行"
    assert "唯一索引" in outcome.reason


def test_enqueueing_the_same_schedule_twice_is_a_noop(dispatcher, queue):
    """同一条排期重复入队 → 只有一条任务、一次投递。"""
    schedule = dispatcher.create_schedule(
        variant_id="var_a6_twice",
        account_id="acct_a6_linkedin",
        scheduled_at=dispatcher._now() + timedelta(days=1),
    )
    first = dispatcher.enqueue_schedule(schedule.id)
    second = dispatcher.enqueue_schedule(schedule.id)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.job_id == first.job_id
    assert "跳过重复投递" in second.reason
    assert len(queue.submissions_for(TASK_PUBLISH_DISPATCH)) == 1
    assert len(dispatcher.jobs.list_jobs()) == 1


def test_reschedule_keeps_the_same_idempotency_key(dispatcher, queue, clock):
    """重排 = 撤销旧投递 + 按新时间重投，**不换幂等键**（换键就等于换了一条内容）。"""
    schedule = dispatcher.create_schedule(
        variant_id="var_a6_resched",
        account_id="acct_a6_linkedin",
        scheduled_at=clock() + timedelta(days=1),
    )
    first = dispatcher.enqueue_schedule(schedule.id)
    job_before = dispatcher.jobs.get(first.job_id)

    moved = dispatcher.reschedule_schedule(
        schedule.id, scheduled_at=clock() + timedelta(days=3)
    )

    job_after = dispatcher.jobs.get(first.job_id)
    assert moved.job_id == first.job_id
    assert job_after.unified_post_id == job_before.unified_post_id
    assert dispatcher.jobs.get_by_unified_post_id(job_before.unified_post_id) is not None
    assert len(dispatcher.jobs.list_jobs()) == 1
    assert moved.eta == dispatcher.schedules.get(schedule.id).scheduled_at
    assert len(queue.submissions_for(TASK_PUBLISH_DISPATCH)) == 2, "重排需要重新投递一次"
    assert queue.cancelled_ids(), "旧的延迟投递必须被撤销"


# --------------------------------------------------------------------------
# 第二层：Adapter 的 find_existing()
# --------------------------------------------------------------------------


def test_adapter_find_existing_prevents_the_second_publish(clock):
    """平台侧已经存在这条 ``up_`` → 不再发布，直接进 ``pending_finalize`` 等核实。"""
    adapter = FakeAdapter("linkedin", FakeBehaviour.PENDING_THEN_PUBLISHED)
    post = make_post()
    adapter.seed_existing(post.unified_post_id, "li_existing_9")
    gateway = PublishGateway(adapters=[adapter], now=clock)

    outcome = run_async(gateway.dispatch(post, CredentialStub(), job_id="job_a6_find"))

    assert adapter.publish_calls_count == 0, "兜底命中时绝不能调用 publish"
    assert outcome.duplicate is True
    assert outcome.status is PublishJobStatus.PENDING_FINALIZE
    assert outcome.result.platform_post_id == "li_existing_9"
    assert outcome.alert and "平台已存在" in outcome.alert
    assert outcome.finalize_deadline is not None, "兜底命中也要收敛回执，而不是直接判定成功"


def test_both_layers_hold_in_one_flow(dispatcher, queue, clock):
    """两层合起来：先入队投递一次，再用同一 ``up_`` 重复投递 + 平台侧兜底。"""
    adapter = FakeAdapter("linkedin", FakeBehaviour.PENDING_THEN_PUBLISHED)
    gateway = PublishGateway(adapters=[adapter], now=clock)
    post = make_post()
    credential = CredentialStub()

    accepted = run_async(gateway.dispatch(post, credential, job_id="job_a6_both_layers"))
    replay = run_async(gateway.dispatch(post, credential, job_id="job_a6_both_layers"))
    adapter.seed_existing(post.unified_post_id, accepted.result.platform_post_id or "li_1")
    settled = run_async(
        gateway.finalize(post.unified_post_id, credential, job_id="job_a6_both_layers")
    )
    finalize_again = run_async(
        gateway.finalize(post.unified_post_id, credential, job_id="job_a6_both_layers")
    )

    assert accepted.status is PublishJobStatus.PENDING_FINALIZE
    assert replay.duplicate is True
    assert settled.status is PublishJobStatus.PUBLISHED
    assert finalize_again.duplicate is True, "已是终态时收敛必须幂等返回"
    assert adapter.publish_calls_count == 1
    assert adapter.finalize_calls == [accepted.result.platform_post_id]


def test_dispatch_marks_publishing_then_never_marks_published_twice(clock):
    """回执只写一次终态：``published`` 之后再收到"已受理"结果不得回退状态。"""
    adapter = FakeAdapter("linkedin", FakeBehaviour.PUBLISHED)
    gateway = PublishGateway(adapters=[adapter], now=clock)
    post = make_post()

    first = run_async(gateway.dispatch(post, CredentialStub(), job_id="job_a6_once"))
    second = run_async(gateway.dispatch(post, CredentialStub(), job_id="job_a6_once"))

    assert first.status is PublishJobStatus.PUBLISHED
    assert second.duplicate is True
    assert gateway.store.get(post.unified_post_id).status is PublishJobStatus.PUBLISHED
    assert adapter.publish_calls_count == 1


def test_finalize_before_any_dispatch_is_reported_not_guessed(clock):
    """收到没有本地记录的 ``finalize`` → 告警，不猜状态（契约 §3.3 的收敛前提）。"""
    gateway = PublishGateway(adapters=[FakeAdapter("linkedin")], now=clock)
    outcome = run_async(
        gateway.finalize("up_a6_unknown", CredentialStub(), job_id="job_a6_unknown")
    )
    assert outcome.status is PublishJobStatus.FAILED
    assert outcome.alert and "没有对应的 dispatch 记录" in outcome.alert
    assert gateway.store.result_writes("up_a6_unknown") == 0
