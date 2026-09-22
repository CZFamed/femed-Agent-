"""best-time 表与排期时刻解析（派工单 §3 W1-A3-5）。"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from pulse.services.scheduler import (
    DEFAULT_LOCAL_TIME,
    FALLBACK_SLOT_NAME,
    BestTimeSlot,
    BestTimeTable,
)

from .conftest import IST, NEW_YORK


def _slot(weekday: int, local_time: time, slot: str = "morning", score: float = 1.0):
    return BestTimeSlot(
        account_id="acct_linkedin_01",
        platform="linkedin",
        timezone="Asia/Kolkata",
        weekday=weekday,
        slot=slot,
        local_time=local_time,
        score=score,
    )


def test_empty_table_falls_back_with_todo_marker():
    """表为空时回退默认本地时间，并留下 `TODO(need-real-data)`（不编数据）。"""

    table = BestTimeTable()
    resolved = table.resolve(
        "acct_linkedin_01",
        "linkedin",
        "Asia/Kolkata",
        after=datetime(2026, 9, 22, 4, 0, tzinfo=IST),
    )

    assert resolved.used_fallback is True
    assert resolved.slot == FALLBACK_SLOT_NAME
    assert resolved.scheduled_at.time() == DEFAULT_LOCAL_TIME
    assert "TODO(need-real-data)" in resolved.reason
    assert resolved.scheduled_at.tzinfo is not None


def test_lookup_prefers_higher_score_then_earlier_time():
    """多行时取 score 最高；同分取更早的本地时间（稳定可预测）。"""

    table = BestTimeTable(
        [
            _slot(2, time(9, 0), "morning", 0.5),
            _slot(2, time(15, 0), "afternoon", 0.9),
            _slot(2, time(8, 30), "early", 0.9),
        ]
    )
    best = table.lookup("acct_linkedin_01", "linkedin", "Asia/Kolkata", 2)
    assert best is not None
    assert best.slot == "early"
    assert [s.slot for s in table.slots_for("acct_linkedin_01", "linkedin", "Asia/Kolkata")] == [
        "early",
        "morning",
        "afternoon",
    ]
    assert len(table) == 3


def test_resolve_returns_next_matching_weekday():
    """从 after 之后找最近的匹配时刻（跳过已过去的时间点）。"""

    # 2026-09-22 是周二；周三（weekday=2）09:30 本地
    table = BestTimeTable([_slot(2, time(9, 30), "wed-morning")])
    resolved = table.resolve(
        "acct_linkedin_01",
        "linkedin",
        "Asia/Kolkata",
        after=datetime(2026, 9, 22, 12, 0, tzinfo=IST),
    )
    assert resolved.scheduled_at == datetime(2026, 9, 23, 9, 30, tzinfo=IST)
    assert resolved.slot == "wed-morning"
    assert resolved.used_fallback is False
    assert "命中 best-time 槽位" in resolved.reason


def test_cross_timezone_does_not_drift():
    """跨时区不漂移：IST 09:30 == UTC 04:00（同一时刻，偏移正确）。"""

    table = BestTimeTable([_slot(2, time(9, 30))])
    resolved = table.resolve(
        "acct_linkedin_01",
        "linkedin",
        "Asia/Kolkata",
        after=datetime(2026, 9, 22, 12, 0, tzinfo=IST),
    )

    assert resolved.scheduled_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert resolved.scheduled_at.isoformat() == "2026-09-23T09:30:00+05:30"
    assert resolved.scheduled_at.astimezone(timezone.utc) == datetime(
        2026, 9, 23, 4, 0, tzinfo=timezone.utc
    )


def test_dst_aware_resolution():
    """夏令时地区解析正确（2026-09 的纽约是 EDT = -04:00，不是 -05:00）。"""

    slot = BestTimeSlot(
        account_id="acct_us_01",
        platform="linkedin",
        timezone=NEW_YORK,
        weekday=2,
        slot="us-morning",
        local_time=time(9, 0),
    )
    table = BestTimeTable([slot])
    resolved = table.resolve(
        "acct_us_01",
        "linkedin",
        NEW_YORK,
        after=datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc),
    )
    assert resolved.scheduled_at.utcoffset() == timedelta(hours=-4)
    assert resolved.to_dict()["scheduled_at"].endswith("-04:00")


def test_resolve_requires_aware_after():
    """`after` 必须带偏移，否则多时区排期会漂移。"""

    table = BestTimeTable()
    with pytest.raises(ValueError, match="时区偏移"):
        table.resolve(
            "acct_linkedin_01", "linkedin", "Asia/Kolkata", after=datetime(2026, 9, 22, 4, 0)
        )


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"weekday": 7}, "weekday 必须在 0..6"),
        ({"timezone": "Mars/Phobos"}, "timezone 非法"),
        ({"slot": ""}, "slot 不能为空"),
        ({"platform": ""}, "platform 不能为空"),
        ({"account_id": ""}, "account_id 不能为空"),
        ({"local_time": "09:30"}, "必须是 datetime.time"),
    ],
)
def test_slot_validation(overrides, expected):
    """行数据校验：星期、时区、槽位名都要合法。"""

    base = _slot(1, time(9, 30))
    from dataclasses import replace

    broken = replace(base, **overrides)
    assert any(expected in msg for msg in broken.validate())

    table = BestTimeTable()
    with pytest.raises(ValueError):
        table.add(broken)


def test_slot_key_matches_dispatch_key_definition():
    """键与派工单一致：account / platform / timezone / weekday / slot。"""

    slot = _slot(3, time(10, 0), "evening")
    assert slot.key == ("acct_linkedin_01", "linkedin", "Asia/Kolkata", 3, "evening")
    assert "周四 10:00" in slot.describe()


def test_create_schedule_at_best_time_uses_account_timezone(dispatcher):
    """端到端：`at_best_time=True` 产出的 `scheduled_at` 落在账号时区且带偏移。"""

    schedule = dispatcher.create_schedule(
        variant_id="var_bt_1", account_id="acct_linkedin_01", at_best_time=True
    )
    assert schedule.timezone == "Asia/Kolkata"
    assert schedule.scheduled_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert schedule.status_value.value == "pending"
    assert "TODO(need-real-data)" in schedule.reason


def test_create_schedule_requires_time_source(dispatcher):
    """既没给时间也不走 best-time → 显式报错。"""

    with pytest.raises(Exception, match="必须给出 scheduled_at 或 at_best_time"):
        dispatcher.create_schedule(variant_id="var_bt_2", account_id="acct_linkedin_01")


def test_create_schedule_rejects_both_sources(dispatcher):
    """两种时间来源互斥。"""

    with pytest.raises(Exception, match="只能二选一"):
        dispatcher.create_schedule(
            variant_id="var_bt_3",
            account_id="acct_linkedin_01",
            scheduled_at=datetime(2026, 9, 23, 9, 0, tzinfo=IST),
            at_best_time=True,
        )


def test_create_schedule_rejects_naive_datetime(dispatcher):
    """无偏移时间直接拒绝（契约 §2 的硬要求）。"""

    with pytest.raises(Exception, match="必须携带时区偏移"):
        dispatcher.create_schedule(
            variant_id="var_bt_4",
            account_id="acct_linkedin_01",
            scheduled_at=datetime(2026, 9, 23, 9, 0),
        )


def test_reschedule_accepts_best_time(dispatcher, scheduled):
    """重排也支持 best-time（同样落账号时区）。"""

    schedule = scheduled(scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=IST))
    dispatcher.enqueue_schedule(schedule.id)
    outcome = dispatcher.reschedule_schedule(schedule.id, at_best_time=True, actor="ops")
    assert outcome.action == "reschedule"
    assert outcome.eta is not None and outcome.eta.tzinfo is not None
