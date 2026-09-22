"""凭据模型与存储（契约 §6 `credentials`）。

表结构（字段名逐字一致）::

    account_id (PK) | provider | encrypted_payload | expires_at
                    | refresh_expires_at | status

三层对象刻意分开，目的是让"业务层只见句柄不见明文"成为**类型层面**的事实：

* `TokenSet` —— 明文令牌集合。`repr` 被屏蔽，**只在发布瞬间存在**。
* `CredentialHandle` —— 业务层（如 scheduler）拿到的句柄：账号 / 提供方 / 密钥版本。
* `Credential` —— 实现 A2 `PlatformAdapter` 需要的 `Credential` 协议
  （`account_id` / `provider` / `access_token()`），只在调用平台 API 时构造。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pulse.services.identity.errors import InvalidCredential
from pulse.services.identity.vault import TokenHandle, VaultClient


class CredentialStatus(StrEnum):
    """`credentials.status` 的取值（契约 §6）。"""

    VALID = "valid"
    EXPIRED = "expired"
    REVOKED = "revoked"


def as_credential_status(value: CredentialStatus | str) -> CredentialStatus:
    """把字符串收敛成 `CredentialStatus`；非法值给出可读错误。"""

    if isinstance(value, CredentialStatus):
        return value
    try:
        return CredentialStatus(str(value))
    except ValueError as exc:
        allowed = ", ".join(s.value for s in CredentialStatus)
        raise InvalidCredential(f"凭据状态非法：{value!r}（允许：{allowed}）") from exc


@dataclass(frozen=True, slots=True)
class TokenSet:
    """明文令牌集合。

    **绝不允许**出现在日志、异常、测试输出里：两个令牌字段都是 `repr=False`，
    且本类自定义了屏蔽版 `repr`（调试器/打印时也只看到摘要）。
    """

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None

    def __repr__(self) -> str:
        return (
            f"<TokenSet expires_at={self.expires_at.isoformat() if self.expires_at else None}"
            " refresh_expires_at="
            f"{self.refresh_expires_at.isoformat() if self.refresh_expires_at else None}"
            f" has_refresh={self.refresh_token is not None} tokens=«redacted»>"
        )

    __str__ = __repr__

    def validate(self) -> list[str]:
        """返回全部字段错误（空列表 = 通过）。"""

        errors: list[str] = []
        if not self.access_token:
            errors.append("access_token 不能为空")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            errors.append("expires_at 必须携带时区偏移（否则过期判断会漂移）")
        if self.refresh_expires_at is not None and self.refresh_expires_at.tzinfo is None:
            errors.append("refresh_expires_at 必须携带时区偏移")
        if (
            self.expires_at is not None
            and self.refresh_expires_at is not None
            and self.refresh_expires_at < self.expires_at
        ):
            errors.append("refresh_expires_at 早于 expires_at：时间顺序不合理（疑似字段错位）")
        return errors

    def assert_valid(self) -> None:
        """校验失败则抛 `InvalidCredential`（含全部错误）。"""

        errors = self.validate()
        if errors:
            raise InvalidCredential("; ".join(errors))


@dataclass(frozen=True, slots=True)
class CredentialHandle:
    """业务层可见的凭据句柄。**没有取明文的方法。**

    scheduler 只依赖本类型；明文只经 `Credential.access_token()` 在发布瞬间取出。
    """

    account_id: str
    provider: str
    key_version: int = 1
    status: CredentialStatus | str = CredentialStatus.VALID

    @property
    def status_value(self) -> CredentialStatus:
        """当前凭据状态。"""

        return as_credential_status(self.status)

    @property
    def token_handle(self) -> TokenHandle:
        """转换成 Vault 句柄（加密层用）。"""

        return TokenHandle(self.account_id, self.key_version)

    def __str__(self) -> str:
        return (
            f"<CredentialHandle {self.account_id} provider={self.provider}"
            f" v{self.key_version} {self.status_value.value}>"
        )

    __repr__ = __str__


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    """`credentials` 表的一行。**密文以 bytes 存放，`repr` 不暴露载荷。**"""

    account_id: str
    provider: str
    encrypted_payload: bytes = field(repr=False, default=b"")
    expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None
    status: CredentialStatus | str = CredentialStatus.VALID
    key_version: int = 1

    def __repr__(self) -> str:
        return (
            f"<CredentialRecord {self.account_id} provider={self.provider}"
            f" status={self.status_value.value} key_version={self.key_version}"
            f" payload_bytes={len(self.encrypted_payload)} payload=«encrypted»>"
        )

    __str__ = __repr__

    @property
    def status_value(self) -> CredentialStatus:
        """当前凭据状态。"""

        return as_credential_status(self.status)

    @property
    def is_usable(self) -> bool:
        """是否处于可用状态（`valid`）。"""

        return self.status_value is CredentialStatus.VALID

    @property
    def handle(self) -> CredentialHandle:
        """本记录的对外句柄。"""

        return CredentialHandle(
            account_id=self.account_id,
            provider=self.provider,
            key_version=self.key_version,
            status=self.status_value,
        )

    def validate(self) -> list[str]:
        """返回全部字段错误（空列表 = 通过）。"""

        errors: list[str] = []
        if not self.account_id or not str(self.account_id).startswith("acct_"):
            errors.append(f"credentials.account_id 必须以 acct_ 开头：{self.account_id!r}")
        if not self.provider:
            errors.append("credentials.provider 不能为空")
        if not self.encrypted_payload:
            errors.append("credentials.encrypted_payload 不能为空（明文不得入库）")
        if self.key_version < 1:
            errors.append(f"credentials.key_version 非法：{self.key_version}")
        try:
            as_credential_status(self.status)
        except InvalidCredential as exc:
            errors.append(str(exc))
        return errors

    def assert_valid(self) -> None:
        """校验失败则抛 `InvalidCredential`（含全部错误）。"""

        errors = self.validate()
        if errors:
            raise InvalidCredential("; ".join(errors))

    def evolve(self, **changes: Any) -> "CredentialRecord":
        """返回替换了部分字段的新记录（本类型不可变）。"""

        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        """可安全外发/入日志的摘要（**不含密文与明文**）。"""

        return {
            "account_id": self.account_id,
            "provider": self.provider,
            "status": self.status_value.value,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "refresh_expires_at": (
                self.refresh_expires_at.isoformat() if self.refresh_expires_at else None
            ),
            "key_version": self.key_version,
            "payload_bytes": len(self.encrypted_payload),
        }


class Credential:
    """发布瞬间的凭据对象（实现 A2 `PlatformAdapter` 需要的 `Credential` 协议）。

    协议成员只有三个：`account_id`、`provider`、`access_token()`。
    `access_token()` 每次调用都返回本次解密出的明文，对象本身可长期持有而不泄露。
    """

    def __init__(
        self,
        record: CredentialRecord,
        vault: VaultClient,
        tokens: TokenSet,
    ) -> None:
        self.account_id = record.account_id
        self.provider = record.provider
        self._record = record
        self._vault = vault
        self._tokens = tokens

    def access_token(self) -> str:
        """取出访问令牌明文。**调用方不得写入日志、异常或测试输出。**"""

        return self._tokens.access_token

    def refresh_token(self) -> str | None:
        """取出刷新令牌明文（仅在刷新流程内使用）。"""

        return self._tokens.refresh_token

    @property
    def expires_at(self) -> datetime | None:
        """访问令牌过期时间。"""

        return self._tokens.expires_at

    @property
    def refresh_expires_at(self) -> datetime | None:
        """刷新令牌过期时间。"""

        return self._tokens.refresh_expires_at

    @property
    def record(self) -> CredentialRecord:
        """底层记录（不含明文）。"""

        return self._record

    def __repr__(self) -> str:
        return (
            f"<Credential {self.account_id} provider={self.provider}"
            f" status={self._record.status_value.value} token=«redacted»>"
        )

    __str__ = __repr__


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------


class CredentialStore(Protocol):
    """凭据存储协议（`account_id` 是主键：一个账号一条凭据）。"""

    def put(self, record: CredentialRecord) -> CredentialRecord:
        """写入或覆盖该账号的凭据记录。"""

    def get(self, account_id: str) -> CredentialRecord | None:
        """按账号取凭据；不存在返回 None。"""

    def delete(self, account_id: str) -> bool:
        """删除凭据记录；返回是否删除成功。"""

    def all_records(self) -> tuple[CredentialRecord, ...]:
        """全部凭据记录（无序）。"""


class InMemoryCredentialStore:
    """进程内实现（单测与本地开发用）。"""

    def __init__(self) -> None:
        self._records: dict[str, CredentialRecord] = {}

    def put(self, record: CredentialRecord) -> CredentialRecord:
        record.assert_valid()
        self._records[record.account_id] = record
        return record

    def get(self, account_id: str) -> CredentialRecord | None:
        return self._records.get(account_id)

    def delete(self, account_id: str) -> bool:
        return self._records.pop(account_id, None) is not None

    def all_records(self) -> tuple[CredentialRecord, ...]:
        return tuple(self._records.values())


__all__ = [
    "Credential",
    "CredentialHandle",
    "CredentialRecord",
    "CredentialStatus",
    "CredentialStore",
    "InMemoryCredentialStore",
    "TokenSet",
    "as_credential_status",
]
