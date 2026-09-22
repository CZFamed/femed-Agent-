"""配额预检（派工单 §3 W1-A3-4）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pulse.services.scheduler import (
    DEFAULT_DAILY_LIMIT,
    DEFAULT_UNIT_COSTS,
    QuotaExceeded,
    QuotaLedger,
    QuotaPolicy,
)

from .conftest import IST, FakeAccount

UPLOAD_COST = 1600.0


def test_defaults_match_verified_numbers():
    """默认值来自派工单已核实数字：单次上传 1600 单位、日配额 10000。"""

    policy = QuotaPolicy()
    assert DEFAULT_DAILY_LIMIT == 10000.0
    assert DEFAULT_UNIT_COSTS["youtube"] == UPLOAD_COST
    assert policy.cost_for("youtube") == UPLOAD_COST
    assert policy.limit_for("youtube") == DEFAULT_DAILY_LIMIT
    assert policy.limit_for("linkedin") is None, "未配置成本的平台不参与配额预检"


def test_seventh_youtube_upload_is_blocked(clock):
    """YouTube：6 次上传（9600）通过，**第 7 次被拦**（9600+1600 > 10000）。"""

    ledger = QuotaLedger(now=clock)
    account_id = "acct_youtube_01"

    for attempt in range(1, 7):
        decision = ledger.reserve(account_id, "youtube", None, now=clock())
        assert decision.allowed, f"第 {attempt} 次应当通过：{decision.reason}"
        assert decision.consumed == UPLOAD_COST * attempt

    blocked = ledger.reserve(account_id, "youtube", None, now=clock())
    assert blocked.allowed is False
    assert blocked.limit == DEFAULT_DAILY_LIMIT
    assert blocked.remaining == pytest.approx(DEFAULT_DAILY_LIMIT - 6 * UPLOAD_COST)
    assert "1600" in blocked.reason and "10000" in blocked.reason
    assert "不投递" in blocked.reason, "失败原因必须写明「本次不投递」"


def test_precheck_does_not_consume(clock):
    """预检只读：反复预检不消耗配额；reserve 才扣减。"""

    ledger = QuotaLedger(now=clock)
    for _ in range(10):
        assert ledger.precheck("acct_x", "youtube", None, now=clock()).allowed
    assert ledger.consumed("acct_x", "youtube", now=clock()) == 0.0

    ledger.reserve("acct_x", "youtube", None, now=clock())
    assert ledger.consumed("acct_x", "youtube", now=clock()) == UPLOAD_COST


def test_ledger_is_per_account_and_platform(clock):
    """配额台账按 (账号, 平台) 隔离。"""

    ledger = QuotaLedger(now=clock)
    ledger.reserve("acct_a", "youtube", None, now=clock())
    assert ledger.consumed("acct_a", "youtube", now=clock()) == UPLOAD_COST
    assert ledger.consumed("acct_b", "youtube", now=clock()) == 0.0
    assert ledger.consumed("acct_a", "linkedin", now=clock()) == 0.0


def test_window_resets_on_next_day(clock):
    """跨天后配额窗口重置（UTC 日切）。"""

    ledger = QuotaLedger(now=clock)
    ledger.reserve("acct_youtube_01", "youtube", None, now=clock())
    assert ledger.consumed("acct_youtube_01", "youtube", now=clock()) == UPLOAD_COST

    clock.advance(days=1)
    assert ledger.consumed("acct_youtube_01", "youtube", now=clock()) == 0.0
    assert ledger.remaining("acct_youtube_01", "youtube", now=clock()) == DEFAULT_DAILY_LIMIT


def test_account_config_can_override_cost_and_limit(clock):
    """`accounts.quota_config` 可按平台覆盖成本与日上限。"""

    ledger = QuotaLedger(now=clock)
    config = {
        "daily_quota": 10000,
        "daily_quota_by_platform": {"youtube": 3000},
        "unit_costs": {"youtube": 1000},
        "quota_window_timezone": "Asia/Kolkata",
    }
    first = ledger.reserve("acct_custom", "youtube", config, now=clock())
    second = ledger.reserve("acct_custom", "youtube", config, now=clock())
    third = ledger.reserve("acct_custom", "youtube", config, now=clock())
    fourth = ledger.reserve("acct_custom", "youtube", config, now=clock())

    assert (first.allowed, second.allowed, third.allowed) == (True, True, True)
    assert third.consumed == 3000.0, "刚好用满 3000 属于允许（不超上限）"
    assert fourth.allowed is False, "3000 上限下第 4 次（4000）应被拦"
    assert fourth.limit == 3000.0
    assert "Asia/Kolkata" in fourth.window


def test_unlimited_platform_skips_check(clock):
    """没进配额表的平台直接放行，并说明原因。"""

    ledger = QuotaLedger(now=clock)
    decision = ledger.reserve("acct_a", "linkedin", None, now=clock())
    assert decision.allowed is True
    assert decision.limit is None
    assert "跳过配额预检" in decision.reason


def test_reset_clears_usage(clock):
    """运维可清空台账（例如平台侧确认已重置）。"""

    ledger = QuotaLedger(now=clock)
    ledger.reserve("acct_a", "youtube", None, now=clock())
    ledger.reset("acct_a")
    assert ledger.consumed("acct_a", "youtube", now=clock()) == 0.0
    ledger.reserve("acct_a", "youtube", None, now=clock())
    ledger.reset()
    assert ledger.consumed("acct_a", "youtube", now=clock()) == 0.0


def test_quota_window_resets_in_is_reported(clock):
    """结论里带窗口重置倒计时（便于运营判断"等多久再发"）。"""

    ledger = QuotaLedger(now=clock)
    decision = ledger.precheck("acct_a", "youtube", None, now=clock())
    assert decision.window.startswith("2026-09-22")
    assert decision.resets_in_s == pytest.approx(20 * 3600.0)


def test_dispatcher_blocks_seventh_upload(dispatcher, registry, queue, scheduled):
    """端到端：第 7 次上传时**不投递**，且原因可读（派工单 §3 W1-A3-4）。"""

    registry.add(
        FakeAccount(
            id="acct_youtube_quota",
            platform="youtube",
            timezone="Asia/Kolkata",
            quota_config={"rate": {"capacity": 100, "refill_per_minute": 60}, "cooldown_seconds": 0},
        )
    )

    for index in range(6):
        schedule = scheduled(
            account_id="acct_youtube_quota",
            variant_id=f"var_quota_{index}",
            scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=IST),
        )
        dispatcher.enqueue_schedule(schedule.id)

    assert len(queue.submissions_for("pulse.publish.dispatch")) == 6

    seventh = scheduled(
        account_id="acct_youtube_quota",
        variant_id="var_quota_7",
        scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=IST),
    )
    with pytest.raises(QuotaExceeded) as excinfo:
        dispatcher.enqueue_schedule(seventh.id)

    error = excinfo.value
    assert "1600" in str(error) and "10000" in str(error)
    assert error.retry_after_s is not None
    assert len(queue.submissions_for("pulse.publish.dispatch")) == 6, "被拦的任务不得投递"
    assert dispatcher.jobs.get_by_schedule(seventh.id) is None
    assert dispatcher.schedules.get(seventh.id).status_value.value == "pending"


def test_dispatcher_quota_snapshot(dispatcher, registry):
    """配额快照供 API/看板展示。"""

    snapshot = dispatcher.quota_snapshot("acct_youtube_01")
    assert snapshot["quota"]["platform"] == "youtube"
    assert snapshot["rate"]["allowed"] is True


def test_quota_timezone_fallback_still_works(clock):
    """未配置窗口时区时按 UTC 切分（默认值有注释说明 YouTube 实际口径待确认）。"""

    ledger = QuotaLedger(now=clock)
    decision = ledger.precheck("acct_a", "youtube", {"daily_quota": 100}, now=clock())
    assert "UTC" in decision.window
    assert decision.resets_in_s == pytest.approx(
        (datetime(2026, 9, 23, tzinfo=timezone.utc) - clock()).total_seconds()
    )
    assert timedelta(seconds=decision.resets_in_s).total_seconds() > 0
