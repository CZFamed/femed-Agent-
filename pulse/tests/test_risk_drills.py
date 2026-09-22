"""W2-A6-3 · 风控演练（四类：限流 / 认证失效 / 政策拒绝 / 异步超时）。

派工单要求「各自断言**降级行为**，而不是仅断言"没崩"」。因此每类演练都断言三件事：

1. **状态去哪了**（``retrying`` / ``pending_finalize`` / ``rejected``……）；
2. **接下来会不会自动继续**（退避时间、轮询任务、有没有重投）；
3. **有没有静默降级**（不许"没重试也没告警"，也不许把"受理"当"成功"）。

全部离线：Fake Adapter + 内存存储 + 固定时钟，不发真实请求、不连 Redis。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from pulse.services.identity import IdentityService, InMemoryVault, TokenSet
from pulse.services.identity.errors import (
    AccountNotPublishable,
    TokenExpired,
    TokenRevoked,
)
from pulse.services.publish import FakeAdapter, PublishGateway, RetryPolicy
from pulse.services.publish.adapters import FakeBehaviour
from pulse.services.publish.state import DEFAULT_FINALIZE_TIMEOUT_S
from pulse.services.scheduler import QuotaLedger
from pulse.services.scheduler.celery_app import (
    TASK_PUBLISH_DISPATCH,
    TASK_PUBLISH_FINALIZE,
)
from pulse.services.scheduler.errors import (
    AccountUnavailable,
    InvalidTransition,
    RateLimitExceeded,
)
from pulse.services.scheduler.tasks import publish_finalize
from pulse.shared.enums import PublishJobStatus, ScheduleStatus
from pulse.shared.models import PublishResult

from pulse.tests.conftest import A6_MASTER_KEY, CredentialStub, make_post, run_async


class TokenRefresherStub:
    """假刷新服务（identity 域的 ``TokenRefresher`` 协议）。"""

    def __init__(self, clock: Any) -> None:
        self.clock = clock
        self.calls: list[str] = []

    def refresh(self, account_id: str) -> TokenSet:
        self.calls.append(account_id)
        now = self.clock()
        return TokenSet(
            access_token="rotated-access",
            refresh_token="rotated-refresh",
            expires_at=now + timedelta(days=30),
            refresh_expires_at=now + timedelta(days=60),
        )


def _pending_job(dispatcher: Any, clock: Any, *, variant_id: str = "var_a6_risk"):
    """造一条处于 ``pending_finalize`` 的任务（走真实入队 + 受理路径）。"""
    schedule = dispatcher.create_schedule(
        variant_id=variant_id,
        account_id="acct_a6_linkedin",
        scheduled_at=clock() + timedelta(days=1),
    )
    plan = dispatcher.enqueue_schedule(schedule.id)
    dispatcher.mark_dispatching(plan.job_id)
    outcome = dispatcher.apply_publish_result(
        plan.job_id,
        PublishResult(ok=True, status="pending_finalize", platform_post_id="pf_a6_risk"),
    )
    assert outcome.status is PublishJobStatus.PENDING_FINALIZE
    return schedule, plan, outcome


# --------------------------------------------------------------------------
# 演练一：限流（平台 429 / 日配额耗尽 / 发布冷却）
# --------------------------------------------------------------------------


def test_drill_rate_limited_degrades_to_backoff_retry(clock):
    """平台限流 → 进 ``retrying`` 并给出退避时间（不是 failed，也不是静默丢弃）。"""
    adapter = FakeAdapter("linkedin", FakeBehaviour.RATE_LIMITED)
    gateway = PublishGateway(adapters=[adapter], now=clock, retry_policy=RetryPolicy())

    outcome = run_async(gateway.dispatch(make_post(), CredentialStub(), job_id="job_a6_rate"))

    assert outcome.status is PublishJobStatus.RETRYING
    assert outcome.result.error_class == "rate_limited"
    assert outcome.next_retry_at == clock() + timedelta(seconds=30)
    assert outcome.alert is None, "还在自动重试，不该刷告警"
    assert outcome.needs_polling is False
    assert gateway.store.get(outcome.unified_post_id).status is PublishJobStatus.RETRYING


def test_drill_rate_limited_retry_is_not_a_second_platform_call(clock):
    """退避期间重投同一条消息 → 走幂等，不再打平台（否则限流会被自己加剧）。"""
    adapter = FakeAdapter("linkedin", FakeBehaviour.RATE_LIMITED)
    gateway = PublishGateway(adapters=[adapter], now=clock)
    post = make_post()

    first = run_async(gateway.dispatch(post, CredentialStub(), job_id="job_a6_rate2"))
    again = run_async(gateway.dispatch(post, CredentialStub(), job_id="job_a6_rate2"))

    assert first.status is PublishJobStatus.RETRYING
    assert again.duplicate is True
    assert adapter.publish_calls_count == 1


def test_drill_rate_limited_exhausts_retries_then_alerts(dispatcher, queue, clock):
    """重试次数用尽 → 终态 failed + 明确告警（不许无限重试，也不许悄悄停）。"""
    schedule = dispatcher.create_schedule(
        variant_id="var_a6_rate3",
        account_id="acct_a6_linkedin",
        scheduled_at=clock() + timedelta(days=1),
    )
    plan = dispatcher.enqueue_schedule(schedule.id)
    rate_limited = PublishResult(
        ok=False, status="failed", error_class="rate_limited", error_message="429"
    )

    delays: list[float] = []
    outcome = None
    for _ in range(6):
        if dispatcher.jobs.get(plan.job_id).is_terminal:
            break
        dispatcher.mark_dispatching(plan.job_id)
        outcome = dispatcher.apply_publish_result(plan.job_id, rate_limited)
        if outcome.next_retry_at is not None:
            delays.append((outcome.next_retry_at - clock()).total_seconds())
        if outcome.status is PublishJobStatus.FAILED:
            break

    assert outcome is not None and outcome.status is PublishJobStatus.FAILED
    assert outcome.alert and "最大投递次数" in outcome.alert
    assert outcome.next_retry_at is None, "终态必须清掉重试时间，否则看板会显示还会重试"
    assert dispatcher.schedules.get(schedule.id).status_value is ScheduleStatus.FAILED
    assert delays[:3] == [30.0, 60.0, 120.0], "退避必须是 30→60→120…（且受 max_delay 限制）"


def test_drill_cooldown_blocks_enqueue_instead_of_silently_queueing(dispatcher, queue):
    """发布冷却中再投 → 显式拒绝，**不**投递、不留半截任务。"""
    first = dispatcher.create_schedule(
        variant_id="var_a6_cool_1",
        account_id="acct_a6_linkedin",
        scheduled_at=dispatcher._now() + timedelta(days=1),
    )
    dispatcher.enqueue_schedule(first.id)

    second = dispatcher.create_schedule(
        variant_id="var_a6_cool_2",
        account_id="acct_a6_linkedin",
        scheduled_at=dispatcher._now() + timedelta(days=2),
    )
    with pytest.raises(RateLimitExceeded) as excinfo:
        dispatcher.enqueue_schedule(second.id)

    assert "冷却" in str(excinfo.value)
    assert dispatcher.jobs.get_by_schedule(second.id) is None, "被拒时不应留下任务行"
    assert dispatcher.schedules.get(second.id).status_value is ScheduleStatus.PENDING
    assert len(queue.submissions_for(TASK_PUBLISH_DISPATCH)) == 1


def test_drill_quota_exhaustion_blocks_instead_of_overrunning(clock):
    """日配额（YouTube 1600 单位/次、10000/天）耗尽 → 拒投，并给出重置剩余时间。"""
    ledger = QuotaLedger(now=clock)
    config: dict[str, Any] = {}
    remaining_after: list[float | None] = []

    for _ in range(6):
        decision = ledger.reserve("acct_a6_yt", "youtube", config, now=clock())
        assert decision.allowed, "前 6 次上传（9600 单位）应在 10000 的日配额内"
        remaining_after.append(decision.remaining)

    blocked = ledger.reserve("acct_a6_yt", "youtube", config, now=clock())
    assert blocked.allowed is False
    assert blocked.reason and "配额" in blocked.reason
    assert blocked.resets_in_s is not None and blocked.resets_in_s > 0
    assert remaining_after == sorted(remaining_after, reverse=True)

    # 只读预检不得扣减额度
    before = ledger.remaining("acct_a6_yt", "youtube", now=clock(), config=config)
    ledger.precheck("acct_a6_yt", "youtube", config, now=clock())
    assert ledger.remaining("acct_a6_yt", "youtube", now=clock(), config=config) == before


# --------------------------------------------------------------------------
# 演练二：认证失效（令牌过期 / 刷新令牌死掉 / 凭据吊销）
# --------------------------------------------------------------------------


def test_drill_expired_access_token_is_refreshed_then_publish_can_continue(clock):
    """进入提前刷新窗口 → 刷新后仍可发布（``auth_expired`` 是"刷新后重试"，不是失败）。"""
    refresher = TokenRefresherStub(clock)
    service = IdentityService(
        vault=InMemoryVault(master_key=A6_MASTER_KEY), now=clock, refresher=refresher
    )
    service.create_account("linkedin", "a6", "Asia/Kolkata", account_id="acct_a6_auth")
    service.store_tokens(
        "acct_a6_auth",
        tokens=TokenSet(
            access_token="stale-access",
            refresh_token="stale-refresh",
            expires_at=clock() + timedelta(minutes=3),  # 落在 10 分钟提前刷新窗口内
            refresh_expires_at=clock() + timedelta(days=30),
        ),
    )

    decision = service.decision_for("acct_a6_auth")
    assert decision.needed is True and decision.must_reauthorize is False
    service.ensure_fresh("acct_a6_auth")
    assert refresher.calls == ["acct_a6_auth"]

    credential = service.bind("acct_a6_auth")
    assert credential.access_token() == "rotated-access"
    assert "rotated-access" not in repr(credential), "凭据对象不得回显明文"

    # 网关侧：401 归类为 auth_expired（可重试），而不是 policy_rejected（不可重试）
    adapter = FakeAdapter("linkedin", FakeBehaviour.AUTH_EXPIRED)
    gateway = PublishGateway(adapters=[adapter], now=clock)
    outcome = run_async(gateway.dispatch(make_post(), credential, job_id="job_a6_auth"))
    assert outcome.status is PublishJobStatus.RETRYING
    assert outcome.result.error_class == "auth_expired"
    assert outcome.next_retry_at is not None


def test_drill_dead_refresh_token_requires_reauthorization(clock):
    """刷新令牌过期 → 显式 ``TokenExpired``，绝不拿一个可能失效的令牌去发。"""
    refresher = TokenRefresherStub(clock)
    service = IdentityService(
        vault=InMemoryVault(master_key=A6_MASTER_KEY), now=clock, refresher=refresher
    )
    service.create_account("linkedin", "a6", "Asia/Kolkata", account_id="acct_a6_dead")
    # 注意：凭据模型不允许"刷新令牌先于访问令牌过期"的畸形组合
    # （store_tokens 会报 InvalidCredential），所以这里用**时间推进**制造真实场景：
    # 刷新令牌 60 分钟后过期，访问令牌 30 分钟后过期，时钟走过 61 分钟。
    service.store_tokens(
        "acct_a6_dead",
        tokens=TokenSet(
            access_token="dead-access",
            refresh_token="dead-refresh",
            expires_at=clock() + timedelta(minutes=30),
            refresh_expires_at=clock() + timedelta(minutes=60),
        ),
    )
    clock.advance(minutes=61)

    assert service.decision_for("acct_a6_dead").must_reauthorize is True
    with pytest.raises(TokenExpired):
        service.ensure_fresh("acct_a6_dead")
    assert refresher.calls == [], "刷新令牌已死时不该再调刷新接口"
    with pytest.raises(TokenExpired):
        service.bind("acct_a6_dead")


def test_drill_revoked_credential_melts_down_pending_jobs(identity, dispatcher, queue, clock):
    """凭据吊销 → 账号暂停 → 该账号待发任务即时挂起、队列任务被撤销。"""
    schedule = dispatcher.create_schedule(
        variant_id="var_a6_revoke",
        account_id="acct_a6_linkedin",
        scheduled_at=clock() + timedelta(days=1),
    )
    plan = dispatcher.enqueue_schedule(schedule.id)
    submission = queue.submissions_for(TASK_PUBLISH_DISPATCH)[0]

    identity.revoke_credential("acct_a6_linkedin", actor="a6_verifier", reason="演练")

    assert dispatcher.jobs.get(plan.job_id).status_value is PublishJobStatus.CANCELLED
    assert dispatcher.schedules.get(schedule.id).status_value is ScheduleStatus.CANCELLED
    assert submission.task_id in queue.cancelled_ids()
    # 账号状态是发布的第一道闸门：已暂停 → 直接拒发
    with pytest.raises(AccountNotPublishable):
        identity.bind("acct_a6_linkedin")
    # 凭据本身也已吊销：刷新路径同样被拒（两道闸门都不放行）
    with pytest.raises(TokenRevoked):
        identity.ensure_fresh("acct_a6_linkedin")

    # 熔断不靠"记得别发"：后续任何新排期都会被拒
    with pytest.raises(AccountUnavailable):
        dispatcher.create_schedule(
            variant_id="var_a6_revoke2",
            account_id="acct_a6_linkedin",
            scheduled_at=clock() + timedelta(days=2),
        )


def test_drill_auth_expired_result_never_becomes_published(dispatcher, clock):
    """认证失效结果在调度侧同样只进 ``retrying``，绝不因"拿到过响应"而放过。"""
    schedule = dispatcher.create_schedule(
        variant_id="var_a6_auth2",
        account_id="acct_a6_linkedin",
        scheduled_at=clock() + timedelta(days=1),
    )
    plan = dispatcher.enqueue_schedule(schedule.id)
    dispatcher.mark_dispatching(plan.job_id)
    outcome = dispatcher.apply_publish_result(
        plan.job_id,
        PublishResult(ok=False, status="failed", error_class="auth_expired", error_message="401"),
    )

    assert outcome.status is PublishJobStatus.RETRYING
    assert outcome.next_retry_at is not None
    assert outcome.alert and "auth_expired" in outcome.alert
    assert dispatcher.schedules.get(schedule.id).status_value is ScheduleStatus.PUBLISHING


# --------------------------------------------------------------------------
# 演练三：政策拒绝（终态，不重试）
# --------------------------------------------------------------------------


def test_drill_policy_rejection_is_terminal_with_alert(clock):
    """平台政策拒绝 → ``rejected`` 终态 + 告警，且**不**安排任何重试。"""
    adapter = FakeAdapter("linkedin", FakeBehaviour.POLICY_REJECTED)
    gateway = PublishGateway(adapters=[adapter], now=clock)
    outcome = run_async(gateway.dispatch(make_post(), CredentialStub(), job_id="job_a6_policy"))

    assert outcome.status is PublishJobStatus.REJECTED
    assert outcome.result.error_class == "policy_rejected"
    assert outcome.next_retry_at is None
    assert outcome.needs_polling is False
    assert outcome.alert and "政策拒绝" in outcome.alert and "不重试" in outcome.alert


def test_drill_policy_rejection_in_scheduler_has_no_retry_task(dispatcher, queue, clock):
    """调度侧：``rejected`` 是终态，排期转 failed，队列里不出现新的重投。"""
    schedule = dispatcher.create_schedule(
        variant_id="var_a6_policy",
        account_id="acct_a6_linkedin",
        scheduled_at=clock() + timedelta(days=1),
    )
    plan = dispatcher.enqueue_schedule(schedule.id)
    dispatcher.mark_dispatching(plan.job_id)
    outcome = dispatcher.apply_publish_result(
        plan.job_id,
        PublishResult(
            ok=False,
            status="rejected",
            error_class="policy_rejected",
            error_message="community guidelines",
        ),
    )

    assert outcome.status is PublishJobStatus.REJECTED
    assert outcome.attempts == 1
    assert outcome.next_retry_at is None
    assert outcome.alert and "政策拒绝" in outcome.alert
    assert dispatcher.schedules.get(schedule.id).status_value is ScheduleStatus.FAILED
    assert len(queue.submissions_for(TASK_PUBLISH_DISPATCH)) == 1, "拒绝后不得再投"

    # 终态不可逆：再想推进必须人工重投（契约 §3.3）
    with pytest.raises(InvalidTransition):
        dispatcher.apply_publish_result(
            plan.job_id, PublishResult(ok=True, status="published", platform_post_id="x")
        )


# --------------------------------------------------------------------------
# 演练四：异步超时（平台迟迟不收敛）
# --------------------------------------------------------------------------


def test_drill_async_timeout_alerts_without_auto_resolution(clock, adapter):
    """超过 30 分钟仍未收敛 → 只告警；既不自动置 published，也不自动判 failed。"""
    gateway = PublishGateway(adapters=[adapter], now=clock)
    post = make_post()
    accepted = run_async(
        gateway.dispatch(post, CredentialStub(), job_id="job_a6_timeout")
    )
    assert accepted.status is PublishJobStatus.PENDING_FINALIZE
    deadline = accepted.finalize_deadline
    assert deadline == clock() + timedelta(seconds=DEFAULT_FINALIZE_TIMEOUT_S)

    clock.advance(minutes=31)
    timed_out = run_async(
        gateway.finalize(post.unified_post_id, CredentialStub(), job_id="job_a6_timeout")
    )

    assert timed_out.status is PublishJobStatus.PENDING_FINALIZE
    assert timed_out.alert and "人工" in timed_out.alert
    assert "既不自动置 published" in timed_out.alert
    assert timed_out.finalize_deadline == deadline, "反复轮询不得给 30 分钟上限续期"
    assert adapter.finalize_calls == [], "超时后不再自动打平台"


def test_drill_async_timeout_can_be_force_polled_by_a_human(clock, adapter):
    """人工核对允许强制轮询一次（``force=True``）——把"人"这条出口留出来。"""
    gateway = PublishGateway(adapters=[adapter], now=clock)
    post = make_post()
    run_async(gateway.dispatch(post, CredentialStub(), job_id="job_a6_force"))
    clock.advance(minutes=31)

    forced = run_async(
        gateway.finalize(post.unified_post_id, CredentialStub(), job_id="job_a6_force", force=True)
    )

    assert adapter.finalize_calls == ["linkedin_fake_1"]
    assert forced.status is PublishJobStatus.PUBLISHED
    assert forced.alert is None


def test_drill_async_timeout_in_scheduler_keeps_state_and_reschedules(
    clock, dispatcher, queue
):
    """调度侧同样只告警：任务停在 ``pending_finalize``，排期停在 ``publishing``。"""
    schedule, plan, _ = _pending_job(dispatcher, clock)

    alive = dispatcher.finalize_pending(plan.job_id)
    assert alive.status is PublishJobStatus.PENDING_FINALIZE
    assert alive.alert is None
    assert len(queue.submissions_for(TASK_PUBLISH_FINALIZE)) >= 2, "未超时要继续轮询"

    deadline = dispatcher.jobs.get(plan.job_id).finalize_deadline
    clock.advance(minutes=31)
    expired = dispatcher.finalize_pending(plan.job_id, now=clock())

    assert expired.status is PublishJobStatus.PENDING_FINALIZE
    assert expired.alert and "超时" in expired.alert
    assert dispatcher.jobs.get(plan.job_id).status_value is PublishJobStatus.PENDING_FINALIZE
    assert dispatcher.schedules.get(schedule.id).status_value is ScheduleStatus.PUBLISHING
    assert dispatcher.jobs.get(plan.job_id).finalize_deadline == deadline


def test_drill_poll_exception_keeps_waiting_instead_of_publishing(dispatcher, clock):
    """轮询**抛异常**（网络断、平台 5xx 异常）→ 保持 ``pending_finalize`` 并继续轮询，
    既不判成功也不判失败。"""
    _, plan, _ = _pending_job(dispatcher, clock, variant_id="var_a6_poll")
    clock.advance(minutes=5)

    class ExplodingSink:
        def poll_finalize(self, job_id: str) -> Any:
            raise TimeoutError("平台轮询超时")

    result = publish_finalize(dispatcher, plan.job_id, sink=ExplodingSink())

    assert result["status"] == PublishJobStatus.PENDING_FINALIZE.value
    assert result["poll_error"] == "TimeoutError"
    assert dispatcher.jobs.get(plan.job_id).status_value is PublishJobStatus.PENDING_FINALIZE
    assert dispatcher.jobs.get(plan.job_id).finalize_deadline is not None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "A3 收敛路径缺陷（G-集成 门未过）：``pending_finalize`` 期间轮询**返回**一个"
        "可重试错误结果（例如 `PublishResult(ok=False, error_class='transient')`，"
        "即平台 5xx/超时被正常翻译成结果对象）时，``Dispatcher.finalize_pending`` → "
        "``apply_publish_result`` 会去执行 ``pending_finalize → retrying``，"
        "而契约 §3.3 的迁移表里 ``pending_finalize`` 只有 published / failed / cancelled 三条出边，"
        "于是抛 `InvalidTransition`：既不保持 pending_finalize，也不再安排下一次轮询，"
        "回执收敛就此静默中断（要等人工发现 30 分钟超时）。"
        "注意：轮询**抛异常**的路径是好的（tasks.publish_finalize 已 catch，见上一个用例）。"
        "修法建议：pending_finalize 期间收到可重试/未知结果时，保持 pending_finalize 并重排轮询"
        "（等价于把该结果当作一次轮询失败），只在超时或拿到明确终态时才改状态。"
    ),
)
def test_drill_poll_returning_retryable_error_keeps_waiting(dispatcher, clock):
    """最小复现：轮询返回 transient 结果（而不是抛异常）。"""
    _, plan, _ = _pending_job(dispatcher, clock, variant_id="var_a6_poll2")
    clock.advance(minutes=5)

    outcome = dispatcher.finalize_pending(
        plan.job_id,
        result=PublishResult(
            ok=False, status="failed", error_class="transient", error_message="502"
        ),
        now=clock(),
    )

    assert outcome.status is PublishJobStatus.PENDING_FINALIZE
    assert dispatcher.jobs.get(plan.job_id).status_value is PublishJobStatus.PENDING_FINALIZE
