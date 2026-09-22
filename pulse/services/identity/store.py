"""账号存储（契约 §6 `accounts`）。

协议 + 内存实现：单测不需要数据库；生产实现（PostgreSQL）由部署层注入。
`add()` 复刻主键语义，`save()` 复刻"必须先存在"的更新语义（防止静默插入）。
"""

from __future__ import annotations

from typing import Protocol

from pulse.services.identity.accounts import Account, AccountStatus, as_status
from pulse.services.identity.errors import AccountNotFound, DuplicateAccount


class AccountStore(Protocol):
    """账号存储协议。"""

    def add(self, account: Account) -> Account:
        """插入新账号；主键冲突抛 `DuplicateAccount`。"""

    def get(self, account_id: str) -> Account | None:
        """按 ID 取账号；不存在返回 None。"""

    def save(self, account: Account) -> Account:
        """写回账号；不存在则抛 `AccountNotFound`。"""

    def remove(self, account_id: str) -> bool:
        """删除账号；返回是否删除成功。"""

    def list_accounts(
        self, *, platform: str | None = None, status: AccountStatus | str | None = None
    ) -> tuple[Account, ...]:
        """按平台/状态过滤（过滤条件为 None 时不过滤）。"""


class InMemoryAccountStore:
    """进程内实现（单测与本地开发用）。"""

    def __init__(self, accounts: tuple[Account, ...] = ()) -> None:
        self._accounts: dict[str, Account] = {}
        for account in accounts:
            self.add(account)

    def add(self, account: Account) -> Account:
        account.assert_valid()
        if account.id in self._accounts:
            raise DuplicateAccount(f"账号已存在（accounts.id 主键冲突）：{account.id}")
        self._accounts[account.id] = account
        return account

    def get(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def save(self, account: Account) -> Account:
        account.assert_valid()
        if account.id not in self._accounts:
            raise AccountNotFound(f"账号不存在，无法更新：{account_id_of(account)}")
        self._accounts[account.id] = account
        return account

    def remove(self, account_id: str) -> bool:
        return self._accounts.pop(account_id, None) is not None

    def list_accounts(
        self, *, platform: str | None = None, status: AccountStatus | str | None = None
    ) -> tuple[Account, ...]:
        wanted = as_status(status) if status is not None else None
        out = []
        for account in self._accounts.values():
            if platform is not None and str(account.platform) != str(platform):
                continue
            if wanted is not None and account.status_value is not wanted:
                continue
            out.append(account)
        return tuple(sorted(out, key=lambda a: a.id))


def account_id_of(account: Account) -> str:
    """取账号 ID（错误消息里统一用它，避免直接打印整个对象）。"""

    return account.id


__all__ = ["AccountStore", "InMemoryAccountStore", "account_id_of"]
