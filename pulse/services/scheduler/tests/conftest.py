"""A3 scheduler 域的测试夹具（域内单测，不写进 `pulse/tests/`）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

import pytest

from pulse.services.scheduler import (
    BestTimeTable,
    Dispatcher,
    EagerTaskQueue,
    InMemoryJobStore,
    InMemoryScheduleStore,
    QuotaLedger,
)

#: 印度标准时间（契约 §2 示例用的 +05:30）
IST = timezone(timedelta(hours=5, minutes=30))

#: 美东时间（用于夏令时相关断言：2026-09 为 EDT = -04:00）
NEW_YORK = "America/New_York"


class FrozenClock:
    """可推进的固定时钟（返回带偏移时间）。"""

    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> datetime:
        self.value = self.value + timedelta(**kwargs)
        return self.value


@dataclass
class FakeAccount:
    """scheduler 眼里的账号视图（结构等同 identity 的 `Account`）。"""

    id: str
    platform: str = "linkedin"
    timezone: str = "Asia/Kolkata"
    status: str = "active"
    quota_config: Mapping[str, Any] = field(default_factory=dict)


class FakeRegistry:
    """假账号注册表（协议：不存在返回 None）。"""

    def __init__(self, accounts: Iterable[FakeAccount] = ()) -> None:
        self.rows: dict[str, FakeAccount] = {a.id: a for a in accounts}

    def account_view(self, account_id: str) -> FakeAccount | None:
        return self.rows.get(account_id)

    def add(self, account: FakeAccount) -> FakeAccount:
        self.rows[account.id] = account
        return account

    def set_status(self, account_id: str, status: str) -> None:
        self.rows[account_id].status = status


class RecordingSink:
    """`pulse.publish.dispatch` 的消费端替身（记录调用 + 按脚本返回）。"""

    def __init__(self, results: Iterable[Any] = ()) -> None:
        self.calls: list[tuple[str, str]] = []
        self.poll_calls: list[str] = []
        self.results: list[Any] = list(results)
        self.default: Any = {
            "ok": True,
            "status": "published",
            "platform_post_id": "pf_default_1",
        }
        self.poll_result: Any = None

    def push(self, result: Any) -> "RecordingSink":
        self.results.append(result)
        return self

    def dispatch(self, job_id: str, unified_post_id: str) -> Any:
        self.calls.append((job_id, unified_post_id))
        if self.results:
            return self.results.pop(0)
        return self.default

    def poll_finalize(self, job_id: str) -> Any:
        self.poll_calls.append(job_id)
        return self.poll_result


@pytest.fixture
def clock() -> FrozenClock:
    """2026-09-22 04:00 UTC（= 印度时间 09:30）。"""

    return FrozenClock(datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc))


@pytest.fixture
def queue() -> EagerTaskQueue:
    """记录式假队列（不连 Redis）。"""

    return EagerTaskQueue()


@pytest.fixture
def schedules() -> InMemoryScheduleStore:
    """内存排期存储。"""

    return InMemoryScheduleStore()


@pytest.fixture
def jobs() -> InMemoryJobStore:
    """内存发布任务存储。"""

    return InMemoryJobStore()


@pytest.fixture
def registry() -> FakeRegistry:
    """两个账号：LinkedIn（印度）与 YouTube（印度）。"""

    return FakeRegistry(
        [
            FakeAccount(id="acct_linkedin_01", platform="linkedin", timezone="Asia/Kolkata"),
            FakeAccount(id="acct_youtube_01", platform="youtube", timezone="Asia/Kolkata"),
        ]
    )


@pytest.fixture
def sink() -> RecordingSink:
    """发布网关替身。"""

    return RecordingSink()


@pytest.fixture
def dispatcher(clock, queue, schedules, jobs, registry) -> Dispatcher:
    """默认调度器（限额 5/桶、冷却 30 分钟，best-time 表为空）。"""

    return Dispatcher(
        accounts=registry,
        schedules=schedules,
        jobs=jobs,
        queue=queue,
        quota=QuotaLedger(now=clock),
        best_time=BestTimeTable(),
        now=clock,
    )


@pytest.fixture
def scheduled(dispatcher):
    """快捷创建一条排期，返回 record。"""

    def factory(
        *,
        account_id: str = "acct_linkedin_01",
        variant_id: str = "var_test_001",
        scheduled_at: datetime | None = None,
        at_best_time: bool = False,
    ):
        moment = scheduled_at or datetime(2026, 9, 23, 9, 30, tzinfo=IST)
        return dispatcher.create_schedule(
            variant_id=variant_id,
            account_id=account_id,
            scheduled_at=None if at_best_time else moment,
            at_best_time=at_best_time,
        )

    return factory
