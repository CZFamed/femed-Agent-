"""identity ↔ scheduler 接线（两个域都属 A3；此处验证真的能扣在一起）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

import pytest

from pulse.services.identity import (
    AccountStatus,
    IdentityService,
    InMemoryVault,
    TokenSet,
)
from pulse.services.scheduler import (
    AccountUnavailable,
    Dispatcher,
    EagerTaskQueue,
    InMemoryJobStore,
    InMemoryScheduleStore,
    QuotaLedger,
    RateLimitExceeded,
)
from pulse.services.scheduler.identity_adapter import (
    IdentityAccountRegistry,
    wire_account_status_listener,
)

from .conftest import IST, FrozenClock


@runtime_checkable
class PublishCredential(Protocol):
    """复刻 A2 `PlatformAdapter.publish` 需要的凭据协议（只读核对形状）。"""

    account_id: str
    provider: str

    def access_token(self) -> str:
        """取访问令牌明文。"""


@pytest.fixture
def clock() -> FrozenClock:
    """固定时钟（2026-09-22 04:00 UTC）。"""

    return FrozenClock(datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc))


@pytest.fixture
def wired(clock):
    """identity + scheduler 接好线的组合（账号已建、监听者已注册）。"""

    identity = IdentityService(vault=InMemoryVault(master_key=b"w" * 32), now=clock)
    queue = EagerTaskQueue()
    schedules = InMemoryScheduleStore()
    jobs = InMemoryJobStore()
    dispatcher = Dispatcher(
        accounts=IdentityAccountRegistry(identity),
        schedules=schedules,
        jobs=jobs,
        queue=queue,
        quota=QuotaLedger(now=clock),
        now=clock,
    )
    wire_account_status_listener(identity, dispatcher)
    account = identity.create_account(
        "linkedin",
        "菲美得 LinkedIn 公司页",
        "Asia/Kolkata",
        quota_config={"rate": {"capacity": 5, "refill_per_minute": 1}, "cooldown_seconds": 60},
        account_id="acct_wired_01",
    )
    return identity, dispatcher, queue, account


def test_registry_adapter_returns_none_for_missing_account(wired):
    """协议要求"不存在返回 None"，适配器把 identity 的异常翻译掉。"""

    identity, _, _, _ = wired
    registry = IdentityAccountRegistry(identity)
    assert registry.account_view("acct_missing") is None
    assert registry.service is identity


def test_identity_account_satisfies_scheduler_view(wired):
    """identity 的 `Account` 天然满足 scheduler 的 `AccountView` 协议。"""

    from pulse.services.scheduler import AccountView

    identity, _, _, account = wired
    view = IdentityAccountRegistry(identity).account_view(account.id)
    assert isinstance(view, AccountView)
    assert view.platform == "linkedin"
    assert view.timezone == "Asia/Kolkata"
    assert dict(view.quota_config)["rate"]["capacity"] == 5


def test_pause_account_suspends_pending_jobs(wired):
    """派工单 §3 W1-A3-9 的端到端证据：账号暂停 → 待发任务即时挂起。"""

    identity, dispatcher, queue, account = wired

    schedule = dispatcher.create_schedule(
        variant_id="var_wired_01",
        account_id=account.id,
        scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=IST),
    )
    outcome = dispatcher.enqueue_schedule(schedule.id)
    assert outcome.action == "enqueue"

    identity.pause_account(account.id, actor="ops", reason="合规复核")

    assert dispatcher.jobs.get(outcome.job_id).status_value.value == "cancelled"
    assert dispatcher.schedules.get(schedule.id).status_value.value == "cancelled"
    assert outcome.task_id in queue.cancelled_ids()
    assert dispatcher.breaker.is_suspended(account.id) is True


def test_revoke_credential_melts_down_via_listener(wired):
    """吊销凭据 → 账号暂停 → 熔断（一条链路走通，无需人工干预）。"""

    identity, dispatcher, _, account = wired
    identity.store_tokens(
        account.id,
        tokens=TokenSet(
            access_token="access",
            refresh_token="refresh",
            expires_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
            refresh_expires_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
        ),
    )
    schedule = dispatcher.create_schedule(
        variant_id="var_wired_02",
        account_id=account.id,
        scheduled_at=datetime(2026, 9, 23, 10, 0, tzinfo=IST),
    )
    dispatcher.enqueue_schedule(schedule.id)

    identity.revoke_credential(account.id, actor="security", reason="疑似泄露")

    assert identity.get_account(account.id).status_value is AccountStatus.PAUSED
    assert dispatcher.breaker.is_suspended(account.id) is True
    assert dispatcher.schedules.get(schedule.id).status_value.value == "cancelled"


def test_pause_cancels_pending_schedule_and_blocks_new_plans(wired):
    """暂停：连"已排期但尚未入队"的内容也一并挂起，并拒绝新建排期。

    注意语义：契约 §3.2 的 `ScheduleStatus` 没有 `paused`，
    因此"挂起"落在 `cancelled` 上（见交付报告的未决问题）。
    """

    identity, dispatcher, _, account = wired
    pending = dispatcher.create_schedule(
        variant_id="var_wired_03",
        account_id=account.id,
        scheduled_at=datetime(2026, 9, 23, 11, 0, tzinfo=IST),
    )
    identity.pause_account(account.id, actor="ops")

    assert dispatcher.schedules.get(pending.id).status_value.value == "cancelled"

    with pytest.raises(AccountUnavailable, match="拒绝创建排期"):
        dispatcher.create_schedule(
            variant_id="var_wired_04",
            account_id=account.id,
            scheduled_at=datetime(2026, 9, 23, 12, 0, tzinfo=IST),
        )


def test_quota_config_flows_from_identity_to_limiter(wired):
    """`accounts.quota_config` 由 identity 存、被 scheduler 用（配置单一来源）。"""

    identity, dispatcher, _, account = wired
    identity.set_status(account.id, AccountStatus.ACTIVE, actor="ops")  # 幂等，不报错

    first = dispatcher.create_schedule(
        variant_id="var_q1",
        account_id=account.id,
        scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=IST),
    )
    dispatcher.enqueue_schedule(first.id)

    second = dispatcher.create_schedule(
        variant_id="var_q2",
        account_id=account.id,
        scheduled_at=datetime(2026, 9, 23, 10, 30, tzinfo=IST),
    )
    with pytest.raises(RateLimitExceeded):
        dispatcher.enqueue_schedule(second.id)


def test_identity_credential_matches_a2_publish_protocol(wired):
    """identity 的 `Credential` 满足 A2 发布网关需要的凭据协议（形状核对）。"""

    identity, _, _, account = wired
    identity.store_tokens(
        account.id,
        tokens=TokenSet(
            access_token="token-for-a2",
            refresh_token="refresh-for-a2",
            expires_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
            refresh_expires_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
        ),
    )
    credential = identity.bind(account.id)

    assert isinstance(credential, PublishCredential)
    assert credential.account_id == account.id
    assert credential.provider == "linkedin"
    assert credential.access_token() == "token-for-a2"
    assert "token-for-a2" not in repr(credential)


def test_credential_refresh_keeps_publishing_possible(wired, clock):
    """临近过期时先刷新再发布：`ensure_fresh` 之后 `bind` 依然可用。"""

    identity, _, _, account = wired
    # 这个组合需要刷新器：接线时由平台 OAuth 客户端提供，这里注入测试替身
    class Refresher:
        def refresh(self, account_id: str) -> TokenSet:
            return TokenSet(
                access_token="refreshed-access",
                refresh_token="refresh",
                expires_at=clock() + timedelta(days=30),
                refresh_expires_at=clock() + timedelta(days=60),
            )

    identity.set_refresher(Refresher())
    identity.store_tokens(
        account.id,
        tokens=TokenSet(
            access_token="short-lived",
            refresh_token="refresh",
            expires_at=clock() + timedelta(seconds=30),
            refresh_expires_at=clock() + timedelta(days=30),
        ),
    )
    handle = identity.ensure_fresh(account.id)
    assert handle.account_id == account.id
    assert identity.bind(account.id).access_token() == "refreshed-access"
