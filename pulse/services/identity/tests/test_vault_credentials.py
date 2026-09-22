"""Vault 加密边界与凭据模型（派工单 §3 W1-A3-8）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pulse.services.identity import (
    Credential,
    CredentialHandle,
    CredentialRecord,
    CredentialStatus,
    InMemoryCredentialStore,
    InMemoryVault,
    InvalidCredential,
    TokenHandle,
    TokenSet,
    VaultError,
)

from .conftest import MASTER_KEY


def test_vault_roundtrip_and_rotate():
    """密文可解密；轮换后旧句柄解不开新密文、新句柄解得开。"""

    vault = InMemoryVault(master_key=MASTER_KEY)
    handle = TokenHandle("acct_vault_01", 1)
    plaintext = b'{"access_token":"plain-access-token"}'

    blob = vault.encrypt(handle, plaintext)
    assert blob != plaintext
    assert plaintext not in blob
    assert vault.decrypt(handle, blob) == plaintext

    new_handle, new_blob = vault.rotate(handle, blob)
    assert new_handle.key_version == 2
    assert vault.decrypt(new_handle, new_blob) == plaintext
    with pytest.raises(VaultError, match="密钥版本不匹配"):
        vault.decrypt(handle, new_blob)


def test_vault_rejects_tampered_and_foreign_blobs():
    """篡改与非法信封都必须显式失败（不能悄悄返回垃圾明文）。"""

    vault = InMemoryVault(master_key=MASTER_KEY)
    handle = TokenHandle("acct_vault_02", 1)
    blob = bytearray(vault.encrypt(handle, b"token-value"))
    blob[-1] ^= 0x01

    with pytest.raises(VaultError, match="认证失败"):
        vault.decrypt(handle, bytes(blob))
    with pytest.raises(VaultError, match="密文格式非法"):
        vault.decrypt(handle, b"not-an-envelope")
    with pytest.raises(VaultError, match="密钥版本非法"):
        vault.encrypt(TokenHandle("acct_vault_02", 0), b"x")


def test_vault_rejects_empty_plaintext_nothing_leaked():
    """空明文也能往返（边界情况不炸）。"""

    vault = InMemoryVault(master_key=MASTER_KEY)
    handle = TokenHandle("acct_vault_04", 1)
    assert vault.decrypt(handle, vault.encrypt(handle, b"")) == b""


def test_handle_and_record_repr_never_leak_plaintext():
    """句柄与记录的 repr/str 里不能出现明文或密文载荷。"""

    handle = TokenHandle("acct_vault_03", 3)
    assert str(handle) == "<TokenHandle acct_vault_03 v3>"

    record = CredentialRecord(
        account_id="acct_vault_03",
        provider="youtube",
        encrypted_payload=b"cipher-bytes",
        key_version=3,
    )
    assert "cipher-bytes" not in repr(record)
    assert "payload=«encrypted»" in repr(record)
    assert record.handle.key_version == 3
    assert record.handle.token_handle == TokenHandle("acct_vault_03", 3)

    tokens = TokenSet(access_token="super-secret-access", refresh_token="super-secret-refresh")
    text = repr(tokens) + str(tokens)
    assert "super-secret-access" not in text
    assert "super-secret-refresh" not in text
    assert "tokens=«redacted»" in text


def test_credential_handle_has_no_plaintext_accessor():
    """业务层句柄不提供任何取明文的方法（只有 vault 句柄）。"""

    handle = CredentialHandle(account_id="acct_x", provider="linkedin")
    assert not hasattr(handle, "access_token")
    assert handle.status_value is CredentialStatus.VALID
    assert "acct_x" in str(handle)


def test_credential_wraps_tokens_but_repr_is_redacted():
    """`Credential` 能取明文（发布瞬间），但 repr 不含明文。"""

    tokens = TokenSet(access_token="access-123", refresh_token="refresh-456")
    record = CredentialRecord(
        account_id="acct_cred_01", provider="linkedin", encrypted_payload=b"x"
    )
    credential = Credential(record, InMemoryVault(master_key=MASTER_KEY), tokens)

    assert credential.access_token() == "access-123"
    assert credential.refresh_token() == "refresh-456"
    assert credential.account_id == "acct_cred_01"
    assert credential.provider == "linkedin"
    assert credential.record is record
    assert "access-123" not in repr(credential)
    assert "token=«redacted»" in repr(credential)


def test_credential_store_is_keyed_by_account():
    """`credentials.account_id` 是主键：一个账号一条凭据，put 覆盖。"""

    store = InMemoryCredentialStore()
    first = CredentialRecord(
        account_id="acct_store_01", provider="linkedin", encrypted_payload=b"a"
    )
    second = first.evolve(encrypted_payload=b"b")

    store.put(first)
    store.put(second)
    assert store.get("acct_store_01") == second
    assert len(store.all_records()) == 1
    assert store.delete("acct_store_01") is True
    assert store.get("acct_store_01") is None


@pytest.mark.parametrize(
    "tokens, expected",
    [
        (TokenSet(access_token=""), "access_token 不能为空"),
        (
            TokenSet(access_token="a", expires_at=datetime(2026, 9, 22, 4, 0)),
            "必须携带时区偏移",
        ),
        (
            TokenSet(
                access_token="a",
                refresh_expires_at=datetime(2026, 9, 22, 4, 0),
            ),
            "必须携带时区偏移",
        ),
        (
            TokenSet(
                access_token="a",
                expires_at=datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc),
                refresh_expires_at=datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc),
            ),
            "早于 expires_at",
        ),
    ],
)
def test_tokenset_validation(tokens, expected):
    """令牌时间必须带偏移且顺序合理。"""

    assert any(expected in msg for msg in tokens.validate())
    with pytest.raises(InvalidCredential):
        tokens.assert_valid()


@pytest.mark.parametrize(
    "record, expected",
    [
        (
            CredentialRecord(account_id="bad", provider="linkedin", encrypted_payload=b"x"),
            "acct_ 开头",
        ),
        (
            CredentialRecord(account_id="acct_a", provider="", encrypted_payload=b"x"),
            "provider 不能为空",
        ),
        (
            CredentialRecord(account_id="acct_a", provider="linkedin", encrypted_payload=b""),
            "encrypted_payload 不能为空",
        ),
        (
            CredentialRecord(
                account_id="acct_a",
                provider="linkedin",
                encrypted_payload=b"x",
                key_version=0,
            ),
            "key_version 非法",
        ),
        (
            CredentialRecord(
                account_id="acct_a",
                provider="linkedin",
                encrypted_payload=b"x",
                status="broken",
            ),
            "凭据状态非法",
        ),
    ],
)
def test_credential_record_validation(record, expected):
    """凭据记录字段校验（明文不得入库 = 密文不能为空）。"""

    assert any(expected in msg for msg in record.validate())
    with pytest.raises(InvalidCredential):
        record.assert_valid()


def test_credential_record_dict_exposes_metadata_only():
    """摘要只给元数据，不含密文与明文。"""

    expires = datetime(2026, 10, 22, 4, 0, tzinfo=timezone.utc)
    record = CredentialRecord(
        account_id="acct_a",
        provider="linkedin",
        encrypted_payload=b"x",
        expires_at=expires,
        refresh_expires_at=expires + timedelta(days=30),
    )
    payload = record.to_dict()
    assert payload["expires_at"] == "2026-10-22T04:00:00+00:00"
    assert payload["payload_bytes"] == 1
    assert "encrypted_payload" not in payload
    assert record.is_usable is True
