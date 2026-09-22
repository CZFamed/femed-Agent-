"""IdentityService 行为与"Token 永不落日志"（派工单 §1 / §5.2）。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from pulse.services.identity import (
    REDACTED,
    AccountNotFound,
    AccountNotPublishable,
    AccountStatus,
    CredentialNotFound,
    CredentialStatus,
    IdentityService,
    InMemoryVault,
    InvalidAccountTransition,
    RedactingFilter,
    TokenExpired,
    redact,
)
from pulse.services.identity.service import LOGGER_NAME

from .conftest import FrozenClock, RecordingListener, make_tokens


def test_create_and_get_account(service, account):
    """新建账号 ID 形如 acct_，默认 active。"""

    assert account.id == "acct_test_01"
    assert account.status_value is AccountStatus.ACTIVE
    assert service.get_account(account.id) == account

    generated = service.create_account("youtube", "菲美得 YouTube", "Asia/Kolkata")
    assert generated.id.startswith("acct_")
    assert [a.id for a in service.list_accounts(platform="youtube")] == [generated.id]


def test_get_missing_account_raises(service):
    """不存在的账号要显式报错。"""

    with pytest.raises(AccountNotFound):
        service.get_account("acct_missing")


def test_set_status_notifies_listener(service, account, listener):
    """状态迁移会广播给监听者（熔断器的接入点），同状态不重复广播。"""

    service.pause_account(account.id, actor="ops", reason="合规复核")
    assert listener.events == [(account.id, "paused")]

    service.resume_account(account.id, actor="ops")
    assert listener.events[-1] == (account.id, "active")

    service.resume_account(account.id, actor="ops")
    assert len(listener.events) == 2


def test_listener_registration_is_explicit(service, listener):
    """监听者可增可删（返回是否找到）。"""

    extra = RecordingListener()
    service.add_listener(extra)
    assert extra in service.listeners
    assert service.remove_listener(extra) is True
    assert service.remove_listener(extra) is False
    assert listener in service.listeners


def test_set_status_rejects_illegal_transition(service, account):
    """revoked 是终态，除非走管理员豁免。"""

    service.revoke_account(account.id, actor="ops", reason="违规")
    with pytest.raises(InvalidAccountTransition):
        service.resume_account(account.id, actor="ops")

    recovered = service.set_status(
        account.id, AccountStatus.ACTIVE, actor="admin", allow_revoked_recovery=True
    )
    assert recovered.status_value is AccountStatus.ACTIVE


def test_listener_failure_propagates(service, account, listener):
    """监听者（熔断器）失败必须立刻可见，不能留下状态与任务不一致。"""

    listener.fail_with = RuntimeError("breaker down")
    with pytest.raises(RuntimeError, match="breaker down"):
        service.pause_account(account.id, actor="ops")


def test_bind_returns_credential_and_respects_status(service, account, clock):
    """bind 只在账号 active 且凭据可用时返回凭据。"""

    service.store_tokens(account.id, tokens=make_tokens(clock))
    credential = service.bind(account.id)
    assert credential.access_token()
    assert credential.account_id == account.id

    service.pause_account(account.id, actor="ops")
    with pytest.raises(AccountNotPublishable, match="不允许发布"):
        service.bind(account.id)


def test_bind_requires_fresh_credential(service, account, clock):
    """过期令牌不允许直接 bind（必须先 ensure_fresh）。"""

    service.store_tokens(account.id, tokens=make_tokens(clock, access_in=timedelta(minutes=-1)))
    with pytest.raises(TokenExpired, match="已过期"):
        service.bind(account.id)


def test_bind_requires_credential(service, account):
    """没有凭据记录时显式报错。"""

    with pytest.raises(CredentialNotFound):
        service.bind(account.id)


def test_revoke_credential_triggers_meltdown(service, account, clock, listener):
    """吊销凭据 → 凭据 revoked + 账号 paused + 广播（熔断）。"""

    service.store_tokens(account.id, tokens=make_tokens(clock))
    record = service.revoke_credential(account.id, actor="security", reason="疑似泄露")

    assert record.status_value is CredentialStatus.REVOKED
    assert service.get_account(account.id).status_value is AccountStatus.PAUSED
    assert listener.events[-1] == (account.id, "paused")


def test_reauthorize_recovers_revoked_account(service, account, clock):
    """重新授权（管理员豁免）可把 revoked 账号拉回 active。"""

    service.store_tokens(account.id, tokens=make_tokens(clock))
    service.revoke_credential(account.id, actor="security")
    service.revoke_account(account.id, actor="security")
    assert service.get_account(account.id).status_value is AccountStatus.REVOKED

    service.store_tokens(
        account.id, tokens=make_tokens(clock, access="new-access"), reauthorize=True
    )
    assert service.get_account(account.id).status_value is AccountStatus.ACTIVE


def test_rotate_credential_key_reencrypts(service, account, clock):
    """密钥轮换后密文变化、句柄版本 +1、明文仍可解出。"""

    service.store_tokens(account.id, tokens=make_tokens(clock, access="rotate-me"))
    before = service.credential_record(account.id)
    after = service.rotate_credential_key(account.id)

    assert after.key_version == before.key_version + 1
    assert after.encrypted_payload != before.encrypted_payload
    assert service.bind(account.id).access_token() == "rotate-me"


def test_credential_handle_is_plaintext_free(service, account, clock):
    """句柄可安全交给 scheduler：只有元数据，没有取明文入口。"""

    service.store_tokens(account.id, tokens=make_tokens(clock))
    handle = service.credential_handle(account.id)
    assert handle.account_id == account.id
    assert not hasattr(handle, "access_token")
    assert handle.status_value is CredentialStatus.VALID


def test_redact_helper_and_filter():
    """redact / RedactingFilter：命中即替换，不阻断日志。"""

    assert redact("token=abc123", ["abc123"]) == f"token={REDACTED}"
    assert redact("token=abc", [""]) == "token=abc"

    flt = RedactingFilter(["secret-value"])
    assert flt.secrets_registered == 1
    record = logging.LogRecord(
        "pulse.identity", logging.INFO, __file__, 1, "token=%s", ("secret-value",), None
    )
    assert flt.filter(record) is True
    assert record.getMessage() == f"token={REDACTED}"

    flt.discard_secret("secret-value")
    assert flt.secrets_registered == 0
    empty = logging.LogRecord("pulse.identity", logging.INFO, __file__, 1, "ok", (), None)
    assert flt.filter(empty) is True


def test_logs_never_contain_tokens(service, account, clock, caplog):
    """**本域头号事故源**：任何日志输出都不得含明文令牌。"""

    secret_access = "access-token-9f2c"
    secret_refresh = "refresh-token-77aa"
    tokens = make_tokens(clock, access=secret_access, refresh=secret_refresh)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        service.store_tokens(account.id, tokens=tokens)
        service.log.info("调试用：access=%s refresh=%s", secret_access, secret_refresh)
        service.ensure_fresh(account.id)

    assert caplog.text, "应当有日志产生，否则这条断言是空转"
    assert secret_access not in caplog.text
    assert secret_refresh not in caplog.text
    assert REDACTED in caplog.text


def test_rotated_tokens_are_also_redacted(service, account, clock, caplog):
    """轮换后的新令牌同样进脱敏表。"""

    service.store_tokens(account.id, tokens=make_tokens(clock, access="first-access"))
    service.store_tokens(
        account.id, tokens=make_tokens(clock, access="second-access", refresh="second-refresh")
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        service.ensure_fresh(account.id)
        service.log.warning("轮换后 second-access / second-refresh")
    assert "second-access" not in caplog.text
    assert "second-refresh" not in caplog.text


def test_service_uses_injected_dependencies(vault, clock):
    """注入的 Vault 与时钟会被真正使用（不是偷偷自建）。"""

    service = IdentityService(vault=vault, now=clock)
    assert service.vault is vault
    assert isinstance(service.vault, InMemoryVault)
    assert service.decision_for  # 方法存在即可调用（无凭据时另测）
    with pytest.raises(CredentialNotFound):
        service.decision_for("acct_none")


def test_default_clock_is_utc_aware():
    """不注入时钟时默认返回带偏移的 UTC 时间。"""

    service = IdentityService()
    moment = service._now()
    assert moment.tzinfo is not None


def test_quota_config_round_trips_into_account(service):
    """`quota_config` 原样保留（scheduler 靠它做限流与配额覆盖）。"""

    config = {"rate": {"capacity": 2}, "daily_quota": 5000}
    created = service.create_account(
        "youtube", "菲美得 YouTube", "Asia/Kolkata", quota_config=config
    )
    assert dict(created.quota_config) == config
    assert created.to_dict()["quota_config"] == config


def test_frozen_clock_helper_advances():
    """夹具时钟可推进（保证时间相关用例可控）。"""

    clock = FrozenClock(datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc))
    clock.advance(minutes=30)
    assert clock().minute == 30
