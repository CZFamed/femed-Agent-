"""A3 identity 域异常（账号与凭据）。

**硬约束**：异常消息里**不得**出现 Token 明文（派工单 §1 / §5.2、`AGENTS.md` §3）。
因此本模块的异常只接受账号 ID、凭据句柄、状态名、时间等**非敏感**信息；
任何"把上游响应体整段塞进异常"的写法都是违规的。
"""

from __future__ import annotations


class IdentityError(Exception):
    """identity 域所有异常的基类。"""


class AccountNotFound(IdentityError, LookupError):
    """账号不存在（`accounts.id` 查不到）。"""


class DuplicateAccount(IdentityError, ValueError):
    """账号 ID 已存在（对应 `accounts.id` 主键冲突）。"""


class InvalidAccount(IdentityError, ValueError):
    """账号字段不合法（ID 前缀、平台白名单、时区等）。"""


class InvalidAccountTransition(IdentityError, ValueError):
    """账号状态机不允许的迁移（契约 §6 `accounts.status`）。"""


class AccountNotPublishable(IdentityError, RuntimeError):
    """账号已暂停或已吊销，**不得**为其投递新的发布任务（熔断）。"""


class CredentialNotFound(IdentityError, LookupError):
    """该账号没有凭据记录（`credentials.account_id` 不存在）。"""


class InvalidCredential(IdentityError, ValueError):
    """凭据字段不合法（缺 refresh token、时间顺序颠倒等）。"""


class VaultError(IdentityError, RuntimeError):
    """Vault/KMS 操作失败（密文篡改、密钥版本不匹配、信封格式非法）。"""


class TokenExpired(IdentityError, RuntimeError):
    """凭据已过期且无法自动刷新，需要人工重新授权。"""


class TokenRevoked(IdentityError, RuntimeError):
    """凭据已吊销：任何使用（含刷新）都必须被拒绝。"""


class RefreshFailed(IdentityError, RuntimeError):
    """刷新失败（平台拒绝或网络问题）。**不自动重试到底**，转人工。"""


__all__ = [
    "AccountNotFound",
    "AccountNotPublishable",
    "CredentialNotFound",
    "DuplicateAccount",
    "IdentityError",
    "InvalidAccount",
    "InvalidAccountTransition",
    "InvalidCredential",
    "RefreshFailed",
    "TokenExpired",
    "TokenRevoked",
    "VaultError",
]
