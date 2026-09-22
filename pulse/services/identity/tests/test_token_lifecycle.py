"""Token 生命周期：双过期 + 提前刷新 + 吊销（派工单 §3 W1-A3-7）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pulse.services.identity import (
    CredentialRecord,
    CredentialStatus,
    IdentityService,
    RefreshFailed,
    TokenExpired,
    TokenRevoked,
    refresh_decision,
)

from .conftest import FrozenClock, make_tokens


def _record(
    clock: FrozenClock,
    *,
    access_in: timedelta,
    refresh_in: timedelta,
    status: CredentialStatus | str = CredentialStatus.VALID,
) -> CredentialRecord:
    """按相对时间造一条凭据记录（不需要真正加密）。"""

    now = clock()
    return CredentialRecord(
        account_id="acct_clock_01",
        provider="linkedin",
        encrypted_payload=b"cipher",
        expires_at=now + access_in,
        refresh_expires_at=now + refresh_in,
        status=status,
    )


def test_refresh_window_opens_before_expiry(clock):
    """提前刷新窗口：剩余 <= skew 时就要刷新，而不是等 401。"""

    record = _record(clock, access_in=timedelta(minutes=5), refresh_in=timedelta(days=30))
    decision = refresh_decision(record, now=clock(), skew_s=600)
    assert decision.needed is True
    assert "提前刷新窗口" in decision.reason
    assert decision.expires_in_s == pytest.approx(300.0)
    assert decision.must_reauthorize is False


def test_valid_token_needs_no_refresh(clock):
    """剩余充足时不做无谓刷新。"""

    record = _record(clock, access_in=timedelta(days=10), refresh_in=timedelta(days=30))
    decision = refresh_decision(record, now=clock())
    assert decision.needed is False
    assert "令牌有效" in decision.reason


def test_expired_access_token_needs_refresh(clock):
    """已过期的访问令牌 → 立即刷新。"""

    record = _record(clock, access_in=timedelta(minutes=-1), refresh_in=timedelta(days=30))
    decision = refresh_decision(record, now=clock())
    assert decision.needed is True
    assert "已过期" in decision.reason
    assert decision.expires_in_s is not None and decision.expires_in_s < 0


def test_expired_refresh_token_requires_reauthorization(clock):
    """刷新令牌过期：刷新已无意义，必须人工重新授权。"""

    record = _record(clock, access_in=timedelta(minutes=5), refresh_in=timedelta(minutes=-1))
    decision = refresh_decision(record, now=clock())
    assert decision.needed is False
    assert decision.refresh_expired is True
    assert decision.must_reauthorize is True
    assert "需人工重新授权" in decision.reason


def test_revoked_credential_refuses_refresh(clock):
    """吊销的凭据：连刷新都要拒绝。"""

    record = _record(
        clock,
        access_in=timedelta(days=1),
        refresh_in=timedelta(days=30),
        status=CredentialStatus.REVOKED,
    )
    decision = refresh_decision(record, now=clock())
    assert decision.revoked is True
    assert decision.needed is False
    assert "已吊销" in decision.reason


def test_missing_expiry_is_refreshed_conservatively(clock):
    """缺少 expires_at 时保守刷新，而不是当作永久有效。"""

    record = CredentialRecord(
        account_id="acct_clock_01", provider="linkedin", encrypted_payload=b"cipher"
    )
    decision = refresh_decision(record, now=clock())
    assert decision.needed is True
    assert "保守刷新" in decision.reason


def test_naive_now_is_rejected(clock):
    """无偏移的 now 会让过期判断漂移，直接拒绝。"""

    record = _record(clock, access_in=timedelta(days=1), refresh_in=timedelta(days=2))
    with pytest.raises(ValueError, match="时区偏移"):
        refresh_decision(record, now=datetime(2026, 9, 22, 4, 0))


def test_ensure_fresh_refreshes_and_reencrypts(service, account, clock, refresher):
    """进入刷新窗口 → 调刷新器 → 重新加密入库（旧明文搜不到）。"""

    service.store_tokens(
        account.id,
        tokens=make_tokens(clock, access="old-access", refresh="old-refresh", access_in=timedelta(minutes=1)),
    )

    handle = service.ensure_fresh(account.id)
    assert refresher.calls == [account.id]
    assert handle.status_value is CredentialStatus.VALID

    record = service.credential_record(account.id)
    assert b"old-access" not in record.encrypted_payload
    assert service.bind(account.id).access_token() == "refreshed-access-token"


def test_ensure_fresh_is_noop_when_token_is_healthy(service, account, clock, refresher):
    """不需要刷新时不得打扰刷新器（避免无谓的 OAuth 往返）。"""

    service.store_tokens(account.id, tokens=make_tokens(clock))
    service.ensure_fresh(account.id)
    assert refresher.calls == []


def test_ensure_fresh_marks_expired_when_refresh_token_dead(service, account, clock):
    """刷新令牌过期 → 记录标记 expired，并抛 TokenExpired。"""

    service.store_tokens(
        account.id,
        tokens=make_tokens(
            clock, access_in=timedelta(hours=-1), refresh_in=timedelta(minutes=-5)
        ),
    )
    with pytest.raises(TokenExpired, match="重新授权"):
        service.ensure_fresh(account.id)
    assert service.credential_record(account.id).status_value is CredentialStatus.EXPIRED


def test_ensure_fresh_rejects_revoked_credential(service, account, clock):
    """吊销凭据 → TokenRevoked（而不是去刷新）。"""

    service.store_tokens(account.id, tokens=make_tokens(clock))
    service.revoke_credential(account.id, actor="tester", reason="疑似泄露")
    with pytest.raises(TokenRevoked):
        service.ensure_fresh(account.id)


def test_ensure_fresh_without_refresher_is_explicit(clock, vault, service, account):
    """未接线刷新器时要显式报错，不能静默当作有效。"""

    plain = IdentityService(vault=vault, now=clock)
    created = plain.create_account(
        "linkedin", "无刷新器的账号", "Asia/Kolkata", account_id="acct_no_refresher"
    )
    plain.store_tokens(
        created.id, tokens=make_tokens(clock, access_in=timedelta(seconds=30))
    )
    with pytest.raises(RefreshFailed, match="未配置 TokenRefresher"):
        plain.ensure_fresh(created.id)


def test_refresh_failure_is_redacted(service, account, clock, refresher):
    """刷新失败时，异常消息里不得回显令牌明文。"""

    secret = "leaked-access-token-abc"
    service.store_tokens(
        account.id, tokens=make_tokens(clock, access=secret, access_in=timedelta(seconds=10))
    )
    refresher.fail_with = RuntimeError(f"platform rejected token {secret}")

    with pytest.raises(RefreshFailed) as excinfo:
        service.ensure_fresh(account.id)
    message = str(excinfo.value)
    assert secret not in message
    assert "«redacted»" in message
    assert f"账号 {account.id} 刷新失败" in message
