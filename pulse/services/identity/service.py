"""identity 域的统一入口：账号 CRUD + 状态机 + 凭据/令牌生命周期。

对外只暴露这一层：

* `IdentityService.create_account / get_account / list_accounts / set_status`
* `IdentityService.store_tokens / credential_handle / bind / ensure_fresh`
* `IdentityService.revoke_credential`（吊销凭据并熔断账号）

**状态变化会广播给监听者**（`AccountStatusListener`）：派工单 §3 W1-A3-9 的
"账号停用 → 即时挂起该账号全部待发任务"由 scheduler 域的熔断器实现该协议后注册进来。
本域**不引用** scheduler 的任何代码，两个域因此都能单独起测。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from pulse.services.identity.accounts import (
    Account,
    AccountStatus,
    assert_transition,
    as_status,
)
from pulse.services.identity.credentials import (
    Credential,
    CredentialHandle,
    CredentialRecord,
    CredentialStatus,
    InMemoryCredentialStore,
    TokenSet,
)
from pulse.services.identity.errors import (
    AccountNotFound,
    AccountNotPublishable,
    CredentialNotFound,
    RefreshFailed,
    TokenExpired,
    TokenRevoked,
    VaultError,
)
from pulse.services.identity.store import AccountStore, InMemoryAccountStore
from pulse.services.identity.tokens import (
    DEFAULT_REFRESH_SKEW_S,
    RefreshDecision,
    TokenRefresher,
    refresh_decision,
)
from pulse.services.identity.vault import (
    InMemoryVault,
    RedactingFilter,
    TokenHandle,
    VaultClient,
    redact,
)
from pulse.shared.enums import Platform
from pulse.shared.ids import new_account_id

#: 本域日志器名（`AGENTS.md` §7：模块名做前缀）
LOGGER_NAME = "pulse.identity"

#: 每个日志器一份共享脱敏表。
#: 为什么共享：若同一日志器上挂多个 `RedactingFilter` 的私有实例，
#: 后创建的实例登记的明文就轮不到过滤（真实事故：测试里第二个服务不脱敏）。
_REDACTORS: dict[str, RedactingFilter] = {}


def redactor_for(logger: logging.Logger) -> RedactingFilter:
    """取（必要时创建并挂载）该日志器的共享脱敏过滤器。"""

    existing = next(
        (f for f in logger.filters if isinstance(f, RedactingFilter)), None
    )
    if existing is None:
        existing = RedactingFilter()
        logger.addFilter(existing)
    _REDACTORS[logger.name] = existing
    return existing


@runtime_checkable
class AccountStatusListener(Protocol):
    """账号状态变化监听者（scheduler 的熔断器实现此协议）。"""

    def on_account_status_changed(self, account_id: str, status: str) -> None:
        """账号状态变化时的回调。`status` 是 `AccountStatus` 的字面值。"""


def _utcnow() -> datetime:
    """默认时钟：带时区偏移的当前时间。"""

    return datetime.now(timezone.utc)


class IdentityService:
    """账号与凭据的统一服务（A3 的入口）。

    Args:
        accounts: 账号存储；缺省内存实现。
        credentials: 凭据存储；缺省内存实现。
        vault: Vault/KMS 客户端；缺省内存假实现（仅测试/本地）。
        listeners: 账号状态变化监听者（如 scheduler 熔断器）。
        refresher: 刷新令牌的服务；缺省 None 表示未接线（刷新时显式报错）。
        now: 时间源，便于测试注入固定时钟（**必须返回带时区的时间**）。
        refresh_skew_s: 提前刷新窗口秒数。
        logger: 日志器；缺省 `pulse.identity`，会自动挂上脱敏过滤器。
    """

    def __init__(
        self,
        *,
        accounts: AccountStore | None = None,
        credentials: InMemoryCredentialStore | None = None,
        vault: VaultClient | None = None,
        listeners: Iterable[AccountStatusListener] = (),
        refresher: TokenRefresher | None = None,
        now: Any = None,
        refresh_skew_s: int = DEFAULT_REFRESH_SKEW_S,
        logger: logging.Logger | None = None,
    ) -> None:
        self._accounts: AccountStore = accounts or InMemoryAccountStore()
        self._credentials = credentials or InMemoryCredentialStore()
        self._vault: VaultClient = vault or InMemoryVault()
        self._listeners: list[AccountStatusListener] = list(listeners)
        self._refresher = refresher
        self._now = now or _utcnow
        self._skew_s = refresh_skew_s

        self.log = logger or logging.getLogger(LOGGER_NAME)
        self.redactor = redactor_for(self.log)

    # -- 只读属性（测试与部署接线用） ---------------------------------------

    @property
    def accounts(self) -> AccountStore:
        """账号存储。"""

        return self._accounts

    @property
    def credentials(self) -> InMemoryCredentialStore:
        """凭据存储。"""

        return self._credentials

    @property
    def vault(self) -> VaultClient:
        """Vault/KMS 客户端。"""

        return self._vault

    def add_listener(self, listener: AccountStatusListener) -> None:
        """注册账号状态变化监听者（如 scheduler 的熔断器）。"""

        self._listeners.append(listener)

    def remove_listener(self, listener: AccountStatusListener) -> bool:
        """注销监听者；返回是否找到并移除。"""

        for index, existing in enumerate(self._listeners):
            if existing is listener:
                del self._listeners[index]
                return True
        return False

    @property
    def listeners(self) -> tuple[AccountStatusListener, ...]:
        """当前监听者列表。"""

        return tuple(self._listeners)

    @property
    def refresher(self) -> TokenRefresher | None:
        """当前刷新器（未接线时为 None）。"""

        return self._refresher

    def set_refresher(self, refresher: TokenRefresher | None) -> None:
        """接线/替换刷新器（部署层在拿到平台 OAuth 客户端后调用）。"""

        self._refresher = refresher

    # -- 账号 CRUD ----------------------------------------------------------

    def create_account(
        self,
        platform: Platform | str,
        display_name: str,
        timezone_name: str,
        *,
        region: str | None = None,
        quota_config: Mapping[str, Any] | None = None,
        account_id: str | None = None,
    ) -> Account:
        """新建账号（`accounts` 一行）。

        Args:
            platform: 平台，须在 `Platform` 白名单内。
            display_name: 账号显示名。
            timezone_name: IANA 时区名（如 `Asia/Kolkata`）。
            region: 面向区域（自由文本，如 `India`）。
            quota_config: 令牌桶 / 冷却 / 平台日配额覆盖值。
            account_id: 指定 ID（测试用）；缺省生成 `acct_` + ULID。
        """

        account = Account(
            id=account_id or new_account_id(),
            platform=str(platform),
            display_name=display_name,
            timezone=timezone_name,
            region=region,
            status=AccountStatus.ACTIVE,
            quota_config=dict(quota_config or {}),
        )
        created = self._accounts.add(account)
        self.log.info(
            "账号已创建 %s platform=%s tz=%s", created.id, created.platform, created.timezone
        )
        return created

    def get_account(self, account_id: str) -> Account:
        """取账号；不存在抛 `AccountNotFound`。"""

        account = self._accounts.get(account_id)
        if account is None:
            raise AccountNotFound(f"账号不存在：{account_id}")
        return account

    def list_accounts(
        self,
        *,
        platform: Platform | str | None = None,
        status: AccountStatus | str | None = None,
    ) -> tuple[Account, ...]:
        """按平台/状态列账号。"""

        return self._accounts.list_accounts(platform=platform, status=status)

    def set_status(
        self,
        account_id: str,
        status: AccountStatus | str,
        *,
        actor: str,
        reason: str = "",
        allow_revoked_recovery: bool = False,
    ) -> Account:
        """迁移账号状态并广播给监听者。

        Args:
            account_id: 账号 ID。
            status: 目标状态（`active` / `paused` / `revoked`）。
            actor: 操作者（审计留痕）。
            reason: 变更原因（审计留痕）。
            allow_revoked_recovery: 管理员豁免，允许 `revoked` 恢复。

        Raises:
            InvalidAccountTransition: 迁移不被允许。
        """

        account = self.get_account(account_id)
        if as_status(status) is account.status_value:
            # 幂等：同状态重复设置不改动、不重复广播
            return account

        target = assert_transition(
            account.status_value, status, allow_revoked_recovery=allow_revoked_recovery
        )
        updated = self._accounts.save(account.evolve(status=target))
        self.log.info(
            "账号状态变更 %s %s→%s actor=%s reason=%s",
            updated.id,
            account.status_value.value,
            target.value,
            actor,
            reason or "-",
        )
        self._notify_status_changed(updated.id, target)
        return updated

    def pause_account(self, account_id: str, *, actor: str, reason: str = "") -> Account:
        """暂停账号（触发熔断）。"""

        return self.set_status(account_id, AccountStatus.PAUSED, actor=actor, reason=reason)

    def resume_account(self, account_id: str, *, actor: str, reason: str = "") -> Account:
        """恢复账号（仅 `paused` 可恢复）。"""

        return self.set_status(account_id, AccountStatus.ACTIVE, actor=actor, reason=reason)

    def revoke_account(self, account_id: str, *, actor: str, reason: str = "") -> Account:
        """吊销账号（终态，触发熔断）。"""

        return self.set_status(account_id, AccountStatus.REVOKED, actor=actor, reason=reason)

    def _notify_status_changed(self, account_id: str, status: AccountStatus) -> None:
        """广播状态变化。

        监听者异常**向上抛出**：熔断器失败必须立刻可见，
        不能留下"账号已暂停但任务照发"的静默不一致。
        """

        for listener in self._listeners:
            listener.on_account_status_changed(account_id, status.value)

    # -- 凭据写入 -----------------------------------------------------------

    def store_tokens(
        self,
        account_id: str,
        *,
        provider: str | None = None,
        tokens: TokenSet,
        actor: str = "system",
        reauthorize: bool = False,
    ) -> CredentialRecord:
        """加密保存一组令牌（`credentials` 一行）。

        明文**只**在本方法内部存在：入库的是 Vault 产出的密文信封。
        `reauthorize=True` 时，若账号处于 `revoked` 终态，将走管理员豁免恢复为 `active`
        （只在重新完成 OAuth 后使用）。
        """

        account = self.get_account(account_id)
        tokens.assert_valid()

        previous = self._credentials.get(account_id)
        key_version = previous.key_version if previous is not None else 1
        handle = TokenHandle(account_id, key_version)
        payload = json.dumps(
            {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        record = CredentialRecord(
            account_id=account_id,
            provider=provider or str(account.platform),
            encrypted_payload=self._vault.encrypt(handle, payload),
            expires_at=tokens.expires_at,
            refresh_expires_at=tokens.refresh_expires_at,
            status=CredentialStatus.VALID,
            key_version=key_version,
        )
        stored = self._credentials.put(record)

        # 登记脱敏串：即便有人误把令牌塞进日志，也会被替换掉。
        self.redactor.add_secret(tokens.access_token)
        self.redactor.add_secret(tokens.refresh_token)

        self.log.info(
            "凭据已更新 %s provider=%s expires_at=%s refresh_expires_at=%s",
            stored.account_id,
            stored.provider,
            stored.expires_at.isoformat() if stored.expires_at else None,
            stored.refresh_expires_at.isoformat() if stored.refresh_expires_at else None,
        )

        if reauthorize and account.status_value is AccountStatus.REVOKED:
            self.set_status(
                account_id,
                AccountStatus.ACTIVE,
                actor=actor,
                reason="重新授权后恢复（管理员豁免）",
                allow_revoked_recovery=True,
            )
        return stored

    def rotate_credential_key(self, account_id: str) -> CredentialRecord:
        """轮换该账号凭据的加密密钥版本（`VaultClient.rotate`）。"""

        record = self.credential_record(account_id)
        handle, blob = self._vault.rotate(record.handle.token_handle, record.encrypted_payload)
        updated = self._credentials.put(record.evolve(encrypted_payload=blob, key_version=handle.key_version))
        self.log.info("凭据密钥已轮换 %s v%d", updated.account_id, updated.key_version)
        return updated

    # -- 凭据读取 -----------------------------------------------------------

    def credential_record(self, account_id: str) -> CredentialRecord:
        """取凭据记录（除密文外均为元数据）；不存在抛 `CredentialNotFound`。"""

        record = self._credentials.get(account_id)
        if record is None:
            raise CredentialNotFound(f"账号没有凭据记录：{account_id}")
        return record

    def credential_handle(self, account_id: str) -> CredentialHandle:
        """取凭据句柄（**不含明文、不能解密**）。"""

        return self.credential_record(account_id).handle

    def decision_for(self, account_id: str, *, now: datetime | None = None) -> RefreshDecision:
        """取该账号凭据的刷新判定（不触发刷新）。"""

        return refresh_decision(
            self.credential_record(account_id), now=now or self._now(), skew_s=self._skew_s
        )

    def bind(self, account_id: str, *, now: datetime | None = None) -> Credential:
        """绑定出可用的 `Credential`（发布瞬间调用）。

        任何不满足条件的情形都**显式报错**，绝不返回"可能已经失效"的凭据：
        账号非 `active` → `AccountNotPublishable`；凭据吊销 → `TokenRevoked`；
        令牌过期或刷新令牌过期 → `TokenExpired`。
        """

        account = self.get_account(account_id)
        if not account.is_publishable:
            raise AccountNotPublishable(
                f"账号 {account_id} 当前状态为 {account.status_value.value}，不允许发布"
            )

        record = self.credential_record(account_id)
        moment = now or self._now()
        decision = refresh_decision(record, now=moment, skew_s=self._skew_s)

        if decision.revoked:
            raise TokenRevoked(f"账号 {account_id} 的凭据已吊销：{decision.reason}")
        if decision.refresh_expired:
            raise TokenExpired(f"账号 {account_id} 需重新授权：{decision.reason}")
        if decision.needed and (decision.expires_in_s or 0) <= 0:
            raise TokenExpired(
                f"账号 {account_id} 的访问令牌已过期（{decision.reason}）；"
                "请先 ensure_fresh() 刷新后再发布"
            )

        return Credential(record, self._vault, self._decrypt(record))

    def ensure_fresh(self, account_id: str, *, now: datetime | None = None) -> CredentialHandle:
        """确保凭据处于"不需要刷新"的状态，返回句柄。

        * 已吊销 → `TokenRevoked`
        * 刷新令牌过期 → 记录标记 `expired` → `TokenExpired`
        * 处于提前刷新窗口 → 调 `TokenRefresher` 刷新并重新加密入库

        **日志里只出现账号 ID、剩余秒数与原因，绝不出现令牌。**
        """

        record = self.credential_record(account_id)
        moment = now or self._now()
        decision = refresh_decision(record, now=moment, skew_s=self._skew_s)

        if decision.revoked:
            raise TokenRevoked(f"账号 {account_id} 的凭据已吊销：{decision.reason}")
        if decision.refresh_expired:
            self._credentials.put(record.evolve(status=CredentialStatus.EXPIRED))
            raise TokenExpired(f"账号 {account_id} 需重新授权：{decision.reason}")
        if not decision.needed:
            return record.handle

        if self._refresher is None:
            raise RefreshFailed(
                f"账号 {account_id} 需要刷新凭据，但未配置 TokenRefresher（部署接线缺失）"
            )

        self.log.info("刷新凭据 %s：%s", account_id, decision.reason)
        try:
            tokens = self._refresher.refresh(account_id)
        except Exception as exc:  # noqa: BLE001 - 统一转成 RefreshFailed，且做脱敏
            detail = redact(str(exc), self._known_secrets(record))
            raise RefreshFailed(
                f"账号 {account_id} 刷新失败：{type(exc).__name__}: {detail}"
            ) from None

        updated = self.store_tokens(account_id, provider=record.provider, tokens=tokens)
        return updated.handle

    def revoke_credential(
        self, account_id: str, *, actor: str, reason: str = ""
    ) -> CredentialRecord:
        """吊销凭据并熔断账号（对应 `DELETE /api/v1/accounts/{id}/credential`）。

        顺序是刻意的：**先吊销凭据，再暂停账号**；账号状态变化会广播给熔断器，
        由它把该账号所有待发任务即时挂起。
        """

        record = self.credential_record(account_id)
        revoked = self._credentials.put(record.evolve(status=CredentialStatus.REVOKED))
        self.log.warning("凭据已吊销 %s actor=%s reason=%s", account_id, actor, reason or "-")

        account = self.get_account(account_id)
        if account.status_value is not AccountStatus.REVOKED:
            self.set_status(
                account_id,
                AccountStatus.PAUSED,
                actor=actor,
                reason=reason or "凭据已吊销，熔断该账号",
            )
        return revoked

    # -- 内部：解密与脱敏 ---------------------------------------------------

    def _decrypt(self, record: CredentialRecord) -> TokenSet:
        """解密凭据载荷成 `TokenSet`（明文只在此处短暂存在）。"""

        raw = self._vault.decrypt(record.handle.token_handle, record.encrypted_payload)
        data = json.loads(raw.decode("utf-8"))
        return TokenSet(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token"),
            expires_at=record.expires_at,
            refresh_expires_at=record.refresh_expires_at,
        )

    def _known_secrets(self, record: CredentialRecord) -> tuple[str, ...]:
        """尽力取回当前凭据的明文串，仅用于把异常消息里的明文洗干净。"""

        try:
            tokens = self._decrypt(record)
        except (VaultError, ValueError, KeyError):
            return ()
        return tuple(s for s in (tokens.access_token, tokens.refresh_token) if s)


__all__ = [
    "LOGGER_NAME",
    "AccountStatusListener",
    "IdentityService",
    "redactor_for",
]
