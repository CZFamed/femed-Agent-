"""A3 identity 域（账号与凭据）。

对外只有三样东西：

* `IdentityService` —— 账号 CRUD、状态机、凭据与令牌生命周期
* `Account` / `AccountStatus` —— 契约 §6 `accounts` 的模型与状态机
* `VaultClient` / `CredentialHandle` —— 加密边界：业务层只见句柄不见明文

契约：`pulse/contracts/INTERFACES.md` §3.2 / §6。
本域**不引用** scheduler 的任何代码（派工单 §1：两域各自独立可测）；
账号状态变化通过 `AccountStatusListener` 协议向外部广播。
"""

from pulse.services.identity.accounts import (
    ALLOWED_TRANSITIONS,
    SUSPEND_STATUSES,
    Account,
    AccountStatus,
    as_status,
    assert_transition,
    can_transition,
)
from pulse.services.identity.credentials import (
    Credential,
    CredentialHandle,
    CredentialRecord,
    CredentialStatus,
    CredentialStore,
    InMemoryCredentialStore,
    TokenSet,
    as_credential_status,
)
from pulse.services.identity.errors import (
    AccountNotFound,
    AccountNotPublishable,
    CredentialNotFound,
    DuplicateAccount,
    IdentityError,
    InvalidAccount,
    InvalidAccountTransition,
    InvalidCredential,
    RefreshFailed,
    TokenExpired,
    TokenRevoked,
    VaultError,
)
from pulse.services.identity.service import (
    LOGGER_NAME,
    AccountStatusListener,
    IdentityService,
)
from pulse.services.identity.store import AccountStore, InMemoryAccountStore
from pulse.services.identity.timezones import (
    UnknownTimezone,
    is_valid_timezone,
    resolve_timezone,
)
from pulse.services.identity.tokens import (
    DEFAULT_REFRESH_SKEW_S,
    RefreshDecision,
    TokenRefresher,
    is_refresh_window_open,
    refresh_decision,
)
from pulse.services.identity.vault import (
    REDACTED,
    InMemoryVault,
    RedactingFilter,
    TokenHandle,
    VaultClient,
    redact,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "DEFAULT_REFRESH_SKEW_S",
    "LOGGER_NAME",
    "REDACTED",
    "SUSPEND_STATUSES",
    "Account",
    "AccountNotFound",
    "AccountNotPublishable",
    "AccountStatus",
    "AccountStatusListener",
    "AccountStore",
    "Credential",
    "CredentialHandle",
    "CredentialNotFound",
    "CredentialRecord",
    "CredentialStatus",
    "CredentialStore",
    "DuplicateAccount",
    "IdentityError",
    "IdentityService",
    "InMemoryAccountStore",
    "InMemoryCredentialStore",
    "InMemoryVault",
    "InvalidAccount",
    "InvalidAccountTransition",
    "InvalidCredential",
    "RedactingFilter",
    "RefreshDecision",
    "RefreshFailed",
    "TokenExpired",
    "TokenHandle",
    "TokenRefresher",
    "TokenRevoked",
    "TokenSet",
    "UnknownTimezone",
    "VaultClient",
    "VaultError",
    "as_credential_status",
    "as_status",
    "assert_transition",
    "can_transition",
    "is_refresh_window_open",
    "is_valid_timezone",
    "redact",
    "refresh_decision",
    "resolve_timezone",
]
