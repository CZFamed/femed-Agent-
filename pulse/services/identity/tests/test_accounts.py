"""账号模型与状态机（契约 §6 `accounts`）。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from pulse.services.identity import (
    Account,
    AccountNotFound,
    AccountStatus,
    DuplicateAccount,
    InMemoryAccountStore,
    InvalidAccount,
    InvalidAccountTransition,
    can_transition,
)
from pulse.services.identity.accounts import assert_transition


def _account(**overrides):
    """基准账号；`overrides` 直接覆盖字段。"""

    base = Account(
        id="acct_unit_01",
        platform="linkedin",
        display_name="菲美得 LinkedIn",
        timezone="Asia/Kolkata",
        region="India",
    )
    return replace(base, **overrides)


def test_account_fields_match_contract():
    """字段名与契约 §6 逐字一致（缺一即接口漂移）。"""

    account = _account()
    assert set(account.to_dict()) == {
        "id",
        "platform",
        "display_name",
        "region",
        "timezone",
        "status",
        "quota_config",
    }
    assert account.to_dict()["status"] == "active"
    assert account.validate() == []


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"id": "acct"}, "acct_ 开头"),
        ({"display_name": "  "}, "display_name"),
        ({"timezone": "Mars/Phobos"}, "timezone"),
        ({"timezone": ""}, "timezone"),
        ({"platform": "tiktok"}, "platform"),
        ({"status": "sleeping"}, "账号状态非法"),
        ({"quota_config": None}, "quota_config"),
    ],
)
def test_account_validation_errors(overrides, expected):
    """非法字段必须给出可读错误，而不是静默接受。"""

    errors = _account(**overrides).validate()
    assert errors, f"应当报错：{overrides}"
    assert any(expected in msg for msg in errors), errors


def test_assert_valid_raises_with_all_errors():
    """一次性报出全部错误，便于一次修完。"""

    broken = _account(id="acct", platform="tiktok", timezone="??", display_name="")
    with pytest.raises(InvalidAccount) as excinfo:
        broken.assert_valid()
    message = str(excinfo.value)
    assert "acct_ 开头" in message
    assert "tiktok" in message
    assert "timezone" in message


def test_status_transitions():
    """active ⇄ paused，二者都可 → revoked；revoked 是终态。"""

    assert can_transition(AccountStatus.ACTIVE, AccountStatus.PAUSED)
    assert can_transition(AccountStatus.PAUSED, AccountStatus.ACTIVE)
    assert can_transition(AccountStatus.ACTIVE, AccountStatus.REVOKED)
    assert can_transition(AccountStatus.PAUSED, AccountStatus.REVOKED)
    assert not can_transition(AccountStatus.REVOKED, AccountStatus.ACTIVE)
    assert not can_transition(AccountStatus.REVOKED, AccountStatus.PAUSED)

    with pytest.raises(InvalidAccountTransition):
        assert_transition(AccountStatus.REVOKED, AccountStatus.ACTIVE)

    assert (
        assert_transition(
            AccountStatus.REVOKED, AccountStatus.ACTIVE, allow_revoked_recovery=True
        )
        is AccountStatus.ACTIVE
    )


def test_is_publishable_only_when_active():
    """只有 active 允许发布。"""

    assert _account().is_publishable
    assert not _account(status=AccountStatus.PAUSED).is_publishable
    assert not _account(status=AccountStatus.REVOKED).is_publishable


def test_store_add_duplicate_and_save_missing():
    """主键语义：重复插入报错；更新不存在的账号报错（防止静默插入）。"""

    store = InMemoryAccountStore()
    store.add(_account())
    with pytest.raises(DuplicateAccount):
        store.add(_account(display_name="另一个名字"))

    with pytest.raises(AccountNotFound):
        store.save(_account(id="acct_other"))


def test_store_list_filters():
    """按平台 / 状态过滤。"""

    store = InMemoryAccountStore(
        (
            _account(id="acct_a", platform="linkedin"),
            _account(id="acct_b", platform="youtube", status=AccountStatus.PAUSED),
            _account(id="acct_c", platform="youtube"),
        )
    )
    assert [a.id for a in store.list_accounts(platform="youtube")] == ["acct_b", "acct_c"]
    assert [a.id for a in store.list_accounts(status="paused")] == ["acct_b"]
    assert [a.id for a in store.list_accounts(platform="youtube", status="active")] == [
        "acct_c"
    ]
    assert len(store.list_accounts()) == 3
    assert store.remove("acct_a") is True
    assert store.remove("acct_a") is False
