"""异步回执状态机与超时告警（派工单 §3 W1-A2-8）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pulse.services.publish.adapters.fake import FakeAdapter, FakeBehaviour
from pulse.services.publish.gateway import PublishGateway
from pulse.shared.enums import PublishJobStatus

BASE_TIME = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


class FixedClock:
    """可手动推进的时钟。"""

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


async def test_acceptance_sets_pending_finalize_with_deadline(make_post, credential) -> None:
    """受理只能进 pending_finalize，并带上 30 分钟窗口。"""

    clock = FixedClock()
    adapter = FakeAdapter(behaviour=FakeBehaviour.PENDING_THEN_PUBLISHED)
    gateway = PublishGateway([adapter], now=clock)

    outcome = await gateway.dispatch(make_post(), credential)

    assert outcome.status is PublishJobStatus.PENDING_FINALIZE
    assert outcome.status is not PublishJobStatus.PUBLISHED
    assert outcome.finalize_deadline == BASE_TIME + timedelta(minutes=30)


async def test_finalize_converges_to_published(make_post, credential) -> None:
    clock = FixedClock()
    adapter = FakeAdapter(behaviour=FakeBehaviour.PENDING_THEN_PUBLISHED)
    gateway = PublishGateway([adapter], now=clock)
    post = make_post()
    dispatched = await gateway.dispatch(post, credential)

    clock.advance(60)
    outcome = await gateway.finalize(post.unified_post_id, credential, job_id=dispatched.job_id)

    assert outcome.status is PublishJobStatus.PUBLISHED
    assert outcome.result.platform_post_id
    assert adapter.finalize_calls


async def test_still_processing_keeps_pending_and_deadline(make_post, credential) -> None:
    """仍在处理中：保持 pending_finalize，且**不重置** deadline（否则会无限续期）。"""

    clock = FixedClock()
    adapter = FakeAdapter(
        behaviour=FakeBehaviour.PENDING_THEN_PUBLISHED,
        finalize_behaviour=FakeBehaviour.PENDING_THEN_PUBLISHED,
    )
    gateway = PublishGateway([adapter], now=clock)
    post = make_post()
    await gateway.dispatch(post, credential)

    clock.advance(300)
    outcome = await gateway.finalize(post.unified_post_id, credential)

    assert outcome.status is PublishJobStatus.PENDING_FINALIZE
    assert outcome.finalize_deadline == BASE_TIME + timedelta(minutes=30)
    assert outcome.alert is None


async def test_timeout_alerts_but_never_auto_publishes(make_post, credential) -> None:
    """**核心约束**：超时只告警——既不自动置 published，也不自动判 failed。"""

    clock = FixedClock()
    adapter = FakeAdapter(
        behaviour=FakeBehaviour.PENDING_THEN_PUBLISHED,
        finalize_behaviour=FakeBehaviour.PENDING_THEN_PUBLISHED,
    )
    gateway = PublishGateway([adapter], now=clock)
    post = make_post()
    await gateway.dispatch(post, credential)

    clock.advance(31 * 60)
    calls_before = len(adapter.finalize_calls)
    outcome = await gateway.finalize(post.unified_post_id, credential)

    assert outcome.status is PublishJobStatus.PENDING_FINALIZE
    assert outcome.status is not PublishJobStatus.PUBLISHED
    assert outcome.alert and "人工" in outcome.alert
    assert len(adapter.finalize_calls) == calls_before  # 超时后不再自动轮询


async def test_force_allows_manual_recheck_after_timeout(make_post, credential) -> None:
    clock = FixedClock()
    adapter = FakeAdapter(behaviour=FakeBehaviour.PENDING_THEN_PUBLISHED)
    gateway = PublishGateway([adapter], now=clock)
    post = make_post()
    await gateway.dispatch(post, credential)

    clock.advance(31 * 60)
    outcome = await gateway.finalize(post.unified_post_id, credential, force=True)

    assert outcome.status is PublishJobStatus.PUBLISHED


async def test_finalize_without_dispatch_record_alerts(credential) -> None:
    gateway = PublishGateway([FakeAdapter()])
    outcome = await gateway.finalize("up_missing", credential)
    assert outcome.status is PublishJobStatus.FAILED
    assert outcome.alert and "没有对应的 dispatch 记录" in outcome.alert


async def test_finalize_is_idempotent_on_terminal_record(make_post, credential) -> None:
    clock = FixedClock()
    adapter = FakeAdapter(behaviour=FakeBehaviour.PUBLISHED)
    gateway = PublishGateway([adapter], now=clock)
    post = make_post()
    await gateway.dispatch(post, credential)

    again = await gateway.finalize(post.unified_post_id, credential)

    assert again.status is PublishJobStatus.PUBLISHED
    assert again.duplicate is True
    assert adapter.finalize_calls == []


async def test_policy_rejection_during_finalize_alerts(make_post, credential) -> None:
    clock = FixedClock()
    adapter = FakeAdapter(
        behaviour=FakeBehaviour.PENDING_THEN_PUBLISHED,
        finalize_behaviour=FakeBehaviour.POLICY_REJECTED,
    )
    gateway = PublishGateway([adapter], now=clock)
    post = make_post()
    await gateway.dispatch(post, credential)

    clock.advance(120)
    outcome = await gateway.finalize(post.unified_post_id, credential)

    assert outcome.status is PublishJobStatus.REJECTED
    assert outcome.alert and "政策拒绝" in outcome.alert


async def test_fetch_metrics_returns_empty_for_p1(make_post, credential) -> None:
    gateway = PublishGateway([FakeAdapter(behaviour=FakeBehaviour.PUBLISHED)])
    post = make_post()
    await gateway.dispatch(post, credential)
    assert await gateway.fetch_metrics(post.unified_post_id, credential) == {}
    assert await gateway.fetch_metrics("up_unknown", credential) == {}
