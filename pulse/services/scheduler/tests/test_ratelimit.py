"""令牌桶限流 + 发布冷却（派工单 §3 W1-A3-3）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pulse.services.scheduler import (
    CooldownTracker,
    RateConfig,
    RateLimitExceeded,
    RateLimiter,
    TokenBucket,
    rate_key,
)

from .conftest import IST, FakeAccount


def test_rate_config_defaults_are_conservative():
    """默认：容量 5、每分钟补 1 个、冷却 30 分钟。"""

    config = RateConfig()
    assert config.capacity == 5.0
    assert config.refill_per_minute == 1.0
    assert config.cooldown_seconds == 1800.0
    assert config.refill_per_second == pytest.approx(1 / 60)


def test_rate_config_from_nested_and_flat_mapping():
    """`accounts.quota_config` 支持嵌套 `rate` 与平铺两种写法。"""

    nested = RateConfig.from_mapping(
        {"rate": {"capacity": 2, "refill_per_minute": 6, "cooldown_seconds": 60}}
    )
    assert (nested.capacity, nested.refill_per_minute, nested.cooldown_seconds) == (2, 6, 60)

    flat = RateConfig.from_mapping(
        {"capacity": 3, "refill_per_minute": 2, "cooldown_seconds": 30}
    )
    assert (flat.capacity, flat.refill_per_minute, flat.cooldown_seconds) == (3, 2, 30)

    defaults = RateConfig.from_mapping(None)
    assert defaults.capacity == 5.0


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"capacity": 0}, "capacity 必须为正数"),
        ({"refill_per_minute": -1}, "不能为负数"),
        ({"cooldown_seconds": -1}, "不能为负数"),
    ],
)
def test_rate_config_rejects_bad_values(kwargs, message):
    """非法配置直接报错，不静默取默认（否则限流形同虚设）。"""

    base = {"capacity": 1.0, "refill_per_minute": 1.0, "cooldown_seconds": 1.0}
    base.update(kwargs)
    with pytest.raises(ValueError, match=message):
        RateConfig(**base)


def test_token_bucket_exhausts_then_refills(clock):
    """令牌桶：容量内放行，用尽即拒；随时间补充后恢复。"""

    bucket = TokenBucket(capacity=2, refill_per_second=1 / 60)
    key = rate_key("acct_1", "linkedin")

    assert bucket.try_acquire(key, now=clock()) == (True, 0.0)
    assert bucket.try_acquire(key, now=clock()) == (True, 0.0)

    allowed, retry_after = bucket.try_acquire(key, now=clock())
    assert allowed is False
    assert retry_after == pytest.approx(60.0)

    clock.advance(seconds=30)
    allowed, retry_after = bucket.try_acquire(key, now=clock())
    assert allowed is False
    assert retry_after == pytest.approx(30.0)

    clock.advance(seconds=30)
    assert bucket.try_acquire(key, now=clock())[0] is True


def test_token_bucket_without_refill_is_stuck_until_reset(clock):
    """补充速率为 0 且桶空时，只能等人工重置（retry_after 为无穷）。"""

    bucket = TokenBucket(capacity=1, refill_per_second=0)
    key = "k"
    assert bucket.try_acquire(key, now=clock())[0] is True

    allowed, retry_after = bucket.try_acquire(key, now=clock())
    assert allowed is False
    assert retry_after == float("inf")

    bucket.reset(key)
    assert bucket.try_acquire(key, now=clock())[0] is True


def test_token_bucket_reset_all_and_validation(clock):
    """重置接口与参数校验。"""

    bucket = TokenBucket(capacity=1, refill_per_second=1)
    bucket.try_acquire("a", now=clock())
    bucket.try_acquire("b", now=clock())
    bucket.reset()
    assert bucket.tokens("a", now=clock()) == pytest.approx(1.0)
    assert bucket.tokens("b", now=clock()) == pytest.approx(1.0)

    with pytest.raises(ValueError, match="capacity 必须为正数"):
        TokenBucket(capacity=0, refill_per_second=1)
    with pytest.raises(ValueError, match="refill_per_second 不能为负数"):
        TokenBucket(capacity=1, refill_per_second=-1)
    with pytest.raises(ValueError, match="amount 必须为正数"):
        bucket.try_acquire("a", now=clock(), amount=0)


def test_cooldown_tracker_blocks_then_allows(clock):
    """冷却：投递后进入冷却窗口，窗口过去才放行。"""

    tracker = CooldownTracker(seconds=1800)
    key = rate_key("acct_1", "facebook")
    assert tracker.allows(key, now=clock()) == (True, 0.0)

    tracker.mark(key, now=clock())
    allowed, remaining = tracker.allows(key, now=clock())
    assert allowed is False
    assert remaining == pytest.approx(1800.0)
    assert tracker.last_at(key) == clock()

    clock.advance(minutes=29)
    allowed, remaining = tracker.allows(key, now=clock())
    assert allowed is False and remaining == pytest.approx(60.0)

    clock.advance(minutes=1)
    assert tracker.allows(key, now=clock()) == (True, 0.0)

    tracker.reset(key)
    assert tracker.last_at(key) is None


def test_zero_cooldown_disables_check(clock):
    """冷却设为 0 时视为关闭（用于测试与特殊运营场景）。"""

    tracker = CooldownTracker(seconds=0)
    tracker.mark("k", now=clock())
    assert tracker.allows("k", now=clock()) == (True, 0.0)


def test_rate_limiter_check_is_read_only_then_acquire_consumes(clock):
    """`check()` 只读；`acquire()` 原子地扣令牌 + 记冷却。"""

    limiter = RateLimiter(config=RateConfig(capacity=1, refill_per_minute=1, cooldown_seconds=600))
    key = rate_key("acct_1", "linkedin")

    assert limiter.check(key, now=clock()).allowed is True
    assert limiter.check(key, now=clock()).allowed is True, "只读检查不得扣减"

    first = limiter.acquire(key, now=clock())
    assert first.allowed is True
    assert first.remaining_tokens == pytest.approx(0.0)

    second = limiter.acquire(key, now=clock())
    assert second.allowed is False
    assert "发布冷却中" in second.reason
    assert second.retry_after_s == pytest.approx(600.0)

    # 冷却过后但桶还没补满（600s 只补 10 个中的 0.x 个 → 这里换成容量 1 的桶已补满）
    clock.advance(minutes=10)
    third = limiter.acquire(key, now=clock())
    assert third.allowed is True


def test_rate_limiter_reports_bucket_exhaustion(clock):
    """桶先被扣空时，拒绝原因指向令牌桶（而不是冷却）。"""

    limiter = RateLimiter(config=RateConfig(capacity=2, refill_per_minute=0, cooldown_seconds=0))
    key = "k"
    assert limiter.acquire(key, now=clock()).allowed is True
    assert limiter.acquire(key, now=clock()).allowed is True

    blocked = limiter.acquire(key, now=clock())
    assert blocked.allowed is False
    assert "令牌桶不足" in blocked.reason
    assert blocked.retry_after_s == float("inf")

    limiter.reset()
    assert limiter.acquire(key, now=clock()).allowed is True


def test_rate_limiter_from_config(clock):
    """从账号配置构造限流器。"""

    limiter = RateLimiter.from_config({"rate": {"capacity": 1, "refill_per_minute": 1}})
    assert limiter.config.capacity == 1.0
    assert limiter.check("k", now=clock()).allowed is True


def test_dispatcher_enforces_cooldown(dispatcher, registry, scheduled, queue):
    """端到端：同一账号同一平台在冷却期内**不得重复发**（派工单 §3 W1-A3-3）。"""

    account = FakeAccount(
        id="acct_cooldown",
        platform="linkedin",
        timezone="Asia/Kolkata",
        quota_config={"rate": {"capacity": 5, "refill_per_minute": 1}, "cooldown_seconds": 1800},
    )
    registry.add(account)

    first = scheduled(
        account_id=account.id,
        variant_id="var_c1",
        scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=IST),
    )
    dispatcher.enqueue_schedule(first.id)

    second = scheduled(
        account_id=account.id,
        variant_id="var_c2",
        scheduled_at=datetime(2026, 9, 23, 10, 30, tzinfo=IST),
    )
    with pytest.raises(RateLimitExceeded) as excinfo:
        dispatcher.enqueue_schedule(second.id)

    assert excinfo.value.retry_after_s is not None
    assert "冷却" in str(excinfo.value)
    assert len(queue.submissions_for("pulse.publish.dispatch")) == 1
    assert dispatcher.jobs.get_by_schedule(second.id) is None

    # 冷却过后可以继续（用另一个时间点推进注入时钟）
    later = dispatcher._now() + timedelta(minutes=31)
    third = scheduled(
        account_id=account.id,
        variant_id="var_c3",
        scheduled_at=datetime(2026, 9, 23, 11, 30, tzinfo=IST),
    )
    outcome = dispatcher.enqueue_schedule(third.id, now=later)
    assert outcome.action == "enqueue"
    assert len(queue.submissions_for("pulse.publish.dispatch")) == 2


def test_dispatcher_token_bucket_blocks_burst(dispatcher, registry, scheduled):
    """端到端：容量 2 的桶在 3 条排期上触发"桶不足"。"""

    account = FakeAccount(
        id="acct_burst",
        platform="linkedin",
        timezone="Asia/Kolkata",
        quota_config={"rate": {"capacity": 2, "refill_per_minute": 0}, "cooldown_seconds": 0},
    )
    registry.add(account)

    for index in range(2):
        schedule = scheduled(
            account_id=account.id,
            variant_id=f"var_burst_{index}",
            scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=IST),
        )
        dispatcher.enqueue_schedule(schedule.id)

    third = scheduled(
        account_id=account.id,
        variant_id="var_burst_3",
        scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=IST),
    )
    with pytest.raises(RateLimitExceeded, match="令牌桶不足"):
        dispatcher.enqueue_schedule(third.id)


def test_force_skips_rate_and_quota(dispatcher, registry, scheduled):
    """管理员强制投递：跳过配额与限流（并在原因里留痕）。"""

    account = FakeAccount(
        id="acct_force",
        platform="youtube",
        timezone="Asia/Kolkata",
        quota_config={"rate": {"capacity": 1, "refill_per_minute": 0}, "cooldown_seconds": 99999},
    )
    registry.add(account)

    first = scheduled(
        account_id=account.id,
        variant_id="var_f1",
        scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=IST),
    )
    dispatcher.enqueue_schedule(first.id)

    second = scheduled(
        account_id=account.id,
        variant_id="var_f2",
        scheduled_at=datetime(2026, 9, 23, 10, 30, tzinfo=IST),
    )
    outcome = dispatcher.enqueue_schedule(second.id, force=True)
    assert outcome.action == "enqueue"
    assert "强制" in outcome.reason
