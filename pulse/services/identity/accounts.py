"""账号模型与状态机（契约 §6 `accounts`）。

字段名与契约**逐字一致**：`id / platform / display_name / region / timezone /
status / quota_config`；`status` 取值 `active | paused | revoked`。

状态机（本域定义，熔断语义见 `pulse/services/scheduler/circuit.py`）::

    active ⇄ paused
       ↘        ↘
        revoked（终态）

`revoked` 视为终态：恢复必须显式走管理员通道（`allow_revoked_recovery=True`），
否则凭据吊销后被静默"复活"会破坏安全审计链路。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Mapping

from pulse.services.identity.errors import InvalidAccount, InvalidAccountTransition
from pulse.services.identity.timezones import is_valid_timezone
from pulse.shared.enums import Platform


class AccountStatus(StrEnum):
    """`accounts.status` 的取值（契约 §6）。"""

    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"


#: 允许的状态迁移。`REVOKED` 无出边 = 终态。
ALLOWED_TRANSITIONS: Mapping[AccountStatus, frozenset[AccountStatus]] = {
    AccountStatus.ACTIVE: frozenset({AccountStatus.PAUSED, AccountStatus.REVOKED}),
    AccountStatus.PAUSED: frozenset({AccountStatus.ACTIVE, AccountStatus.REVOKED}),
    AccountStatus.REVOKED: frozenset(),
}

#: 会触发"挂起该账号全部待发任务"的状态（熔断条件，派工单 §3 W1-A3-9）。
SUSPEND_STATUSES: frozenset[AccountStatus] = frozenset(
    {AccountStatus.PAUSED, AccountStatus.REVOKED}
)


def as_status(value: AccountStatus | str) -> AccountStatus:
    """把字符串收敛成 `AccountStatus`；非法值给出可读错误。"""

    if isinstance(value, AccountStatus):
        return value
    try:
        return AccountStatus(str(value))
    except ValueError as exc:
        allowed = ", ".join(s.value for s in AccountStatus)
        raise InvalidAccount(f"账号状态非法：{value!r}（允许：{allowed}）") from exc


def can_transition(current: AccountStatus | str, target: AccountStatus | str) -> bool:
    """状态迁移是否被允许（不考虑管理员豁免）。"""

    return as_status(target) in ALLOWED_TRANSITIONS[as_status(current)]


def assert_transition(
    current: AccountStatus | str,
    target: AccountStatus | str,
    *,
    allow_revoked_recovery: bool = False,
) -> AccountStatus:
    """校验迁移，返回目标状态；不合法则抛 `InvalidAccountTransition`。

    Args:
        current: 当前状态。
        target: 目标状态。
        allow_revoked_recovery: 管理员豁免——允许 `revoked → active/paused`
            （只在持有管理权限的 API 路径上使用，如重新接入 OAuth 后）。
    """

    src = as_status(current)
    dst = as_status(target)
    if can_transition(src, dst):
        return dst
    if allow_revoked_recovery and src is AccountStatus.REVOKED:
        return dst
    raise InvalidAccountTransition(
        f"账号状态不允许 {src.value} → {dst.value}"
        f"（允许：{', '.join(sorted(s.value for s in ALLOWED_TRANSITIONS[src])) or '无（终态）'}）"
    )


@dataclass(frozen=True, slots=True)
class Account:
    """账号矩阵中的一行（契约 §6）。

    `quota_config` 是 JSONB 形态的字典：令牌桶容量/补充速率、发布冷却、
    以及平台日配额覆盖值，解析见 `pulse/services/scheduler/ratelimit.py`。
    """

    id: str
    platform: str
    display_name: str
    timezone: str
    region: str | None = None
    status: AccountStatus | str = AccountStatus.ACTIVE
    quota_config: Mapping[str, Any] = field(default_factory=dict)

    # -- 校验 ---------------------------------------------------------------

    def validate(self) -> list[str]:
        """返回全部字段错误（空列表 = 通过）。"""

        errors: list[str] = []

        if not self.id or not str(self.id).startswith("acct_"):
            errors.append(f"accounts.id 必须以 acct_ 开头：{self.id!r}")
        if not self.display_name or not str(self.display_name).strip():
            errors.append("accounts.display_name 不能为空")
        if not self.timezone or not is_valid_timezone(str(self.timezone)):
            errors.append(
                f"accounts.timezone 非法：{self.timezone!r}"
                "（需为 IANA 名称，如 Asia/Kolkata；无偏移的时区会让排期漂移）"
            )
        try:
            Platform(str(self.platform))
        except ValueError:
            allowed = ", ".join(p.value for p in Platform)
            errors.append(f"accounts.platform 非法：{self.platform!r}（允许：{allowed}）")
        try:
            as_status(self.status)
        except InvalidAccount as exc:
            errors.append(str(exc))
        if self.quota_config is None or not isinstance(self.quota_config, Mapping):
            errors.append("accounts.quota_config 必须是对象（JSONB）")

        return errors

    def assert_valid(self) -> None:
        """校验失败则抛 `InvalidAccount`（含全部错误）。"""

        errors = self.validate()
        if errors:
            raise InvalidAccount("; ".join(errors))

    # -- 便捷属性与不可变更新 ------------------------------------------------

    @property
    def status_value(self) -> AccountStatus:
        """当前状态（枚举形式）。"""

        return as_status(self.status)

    @property
    def is_publishable(self) -> bool:
        """是否允许投递新任务（仅 `active`）。"""

        return self.status_value is AccountStatus.ACTIVE

    def evolve(self, **changes: Any) -> "Account":
        """返回替换了部分字段的新账号对象（本类型不可变）。"""

        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        """契约字段形态的字典（供 API/落库层使用）。"""

        return {
            "id": self.id,
            "platform": str(self.platform),
            "display_name": self.display_name,
            "region": self.region,
            "timezone": self.timezone,
            "status": self.status_value.value,
            "quota_config": dict(self.quota_config or {}),
        }


__all__ = [
    "ALLOWED_TRANSITIONS",
    "SUSPEND_STATUSES",
    "Account",
    "AccountStatus",
    "as_status",
    "assert_transition",
    "can_transition",
]
