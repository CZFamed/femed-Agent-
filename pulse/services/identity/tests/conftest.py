"""A3 identity 域的测试夹具（域内单测，不写进 `pulse/tests/`）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pulse.services.identity import IdentityService, InMemoryVault, TokenSet

#: 印度标准时间（契约 §2 示例用的 +05:30）
IST = timezone(timedelta(hours=5, minutes=30))

#: 测试里固定使用的主密钥（32 字节，保证可复现）
MASTER_KEY = b"pulse-test-master-key-32-bytes!!"


class FrozenClock:
    """可推进的固定时钟（所有时间都带 UTC 偏移）。"""

    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> datetime:
        self.value = self.value + timedelta(**kwargs)
        return self.value


class FakeRefresher:
    """假刷新服务：记录调用、可注入失败。"""

    def __init__(
        self,
        clock: FrozenClock,
        *,
        access_lifetime: timedelta = timedelta(days=30),
        refresh_lifetime: timedelta = timedelta(days=60),
    ) -> None:
        self._clock = clock
        self._access_lifetime = access_lifetime
        self._refresh_lifetime = refresh_lifetime
        self.calls: list[str] = []
        self.fail_with: Exception | None = None
        self.access_token_value = "refreshed-access-token"
        self.refresh_token_value = "refreshed-refresh-token"

    def refresh(self, account_id: str) -> TokenSet:
        self.calls.append(account_id)
        if self.fail_with is not None:
            raise self.fail_with
        now = self._clock()
        return TokenSet(
            access_token=self.access_token_value,
            refresh_token=self.refresh_token_value,
            expires_at=now + self._access_lifetime,
            refresh_expires_at=now + self._refresh_lifetime,
        )


class RecordingListener:
    """记录账号状态变化（模拟 scheduler 的熔断器）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.fail_with: Exception | None = None

    def on_account_status_changed(self, account_id: str, status: str) -> None:
        self.events.append((account_id, status))
        if self.fail_with is not None:
            raise self.fail_with


def make_tokens(
    clock: FrozenClock,
    *,
    access: str = "plain-access-token",
    refresh: str | None = "plain-refresh-token",
    access_in: timedelta = timedelta(days=30),
    refresh_in: timedelta = timedelta(days=60),
) -> TokenSet:
    """构造一组令牌（默认 30 天有效）。"""

    now = clock()
    return TokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_at=now + access_in,
        refresh_expires_at=now + refresh_in,
    )


@pytest.fixture
def clock() -> FrozenClock:
    """2026-09-22 04:00 UTC（= 印度时间 09:30）。"""

    return FrozenClock(datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc))


@pytest.fixture
def vault() -> InMemoryVault:
    """固定主密钥的内存假 Vault。"""

    return InMemoryVault(master_key=MASTER_KEY)


@pytest.fixture
def refresher(clock: FrozenClock) -> FakeRefresher:
    """假刷新服务。"""

    return FakeRefresher(clock)


@pytest.fixture
def listener() -> RecordingListener:
    """账号状态监听者替身。"""

    return RecordingListener()


@pytest.fixture
def service(
    clock: FrozenClock,
    vault: InMemoryVault,
    refresher: FakeRefresher,
    listener: RecordingListener,
) -> IdentityService:
    """已装好假 Vault / 假刷新器 / 监听者的 identity 服务。"""

    return IdentityService(vault=vault, refresher=refresher, listeners=[listener], now=clock)


@pytest.fixture
def account(service: IdentityService) -> Any:
    """一个 active 的 LinkedIn 账号（时区 Asia/Kolkata）。"""

    return service.create_account(
        "linkedin",
        "菲美得 LinkedIn 公司页",
        "Asia/Kolkata",
        region="India",
        quota_config={"rate": {"capacity": 3, "refill_per_minute": 1}},
        account_id="acct_test_01",
    )


@pytest.fixture
def tokens(clock: FrozenClock) -> TokenSet:
    """默认令牌组合。"""

    return make_tokens(clock)
