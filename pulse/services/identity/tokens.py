"""Token 生命周期（契约 §6 `credentials.expires_at` / `refresh_expires_at`）。

两条独立的时间线：

* `expires_at` —— 访问令牌过期。**进入提前刷新窗口就要刷新**，不要等到 401。
* `refresh_expires_at` —— 刷新令牌过期。一旦过期，自动刷新已无意义，必须人工重新授权。

不在日志里出现的纪律同 `vault.py`：本模块只处理时间与状态，永不回显令牌。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pulse.services.identity.credentials import (
    CredentialRecord,
    CredentialStatus,
    TokenSet,
)

#: 默认提前刷新窗口：过期前 10 分钟就刷新（避开"刚好卡在 401 上"的抖动）。
DEFAULT_REFRESH_SKEW_S = 600


@dataclass(frozen=True, slots=True)
class RefreshDecision:
    """一次"是否需要刷新"的判定结果（可安全入日志）。"""

    needed: bool
    reason: str
    expires_in_s: float | None = None
    refresh_expired: bool = False
    revoked: bool = False

    @property
    def must_reauthorize(self) -> bool:
        """是否必须人工重新授权（刷新已无法解决）。"""

        return self.refresh_expired or self.revoked


class TokenRefresher(Protocol):
    """刷新令牌的服务协议（由平台 OAuth 客户端实现；测试注入假实现）。"""

    def refresh(self, account_id: str) -> TokenSet:
        """用 refresh token 换一组新的令牌。失败时抛 `RefreshFailed`。"""


def refresh_decision(
    record: CredentialRecord,
    *,
    now: datetime,
    skew_s: int = DEFAULT_REFRESH_SKEW_S,
) -> RefreshDecision:
    """判定该凭据是否需要（以及是否还能）刷新。

    Args:
        record: 凭据记录（只读时间与状态，不解密）。
        now: 当前时间（**必须带时区偏移**，否则过期判断会漂移）。
        skew_s: 提前刷新窗口秒数。

    Returns:
        `RefreshDecision`。`needed=False` 且有 `must_reauthorize=True` 时，
        调用方应直接转人工，而不是调用刷新接口。
    """

    if now.tzinfo is None:
        raise ValueError("now 必须携带时区偏移（无偏移时间会让过期判断漂移）")

    status = record.status_value
    if status is CredentialStatus.REVOKED:
        return RefreshDecision(
            needed=False,
            reason="凭据已吊销（revoked），拒绝刷新，需人工重新授权",
            revoked=True,
        )

    refresh_expired = (
        record.refresh_expires_at is not None and now >= record.refresh_expires_at
    )
    if refresh_expired:
        return RefreshDecision(
            needed=False,
            reason=(
                "刷新令牌已过期"
                f"（refresh_expires_at={record.refresh_expires_at.isoformat()}），"
                "需人工重新授权"
            ),
            refresh_expired=True,
        )

    if record.expires_at is None:
        return RefreshDecision(
            needed=True,
            reason="凭据缺少 expires_at，无法判断剩余有效期，保守刷新",
        )

    remaining = (record.expires_at - now).total_seconds()
    if remaining <= 0:
        return RefreshDecision(
            needed=True,
            reason=f"访问令牌已过期 {abs(remaining):.0f}s，立即刷新",
            expires_in_s=remaining,
        )
    if remaining <= skew_s:
        return RefreshDecision(
            needed=True,
            reason=f"距过期仅剩 {remaining:.0f}s，进入提前刷新窗口（skew={skew_s}s）",
            expires_in_s=remaining,
        )
    return RefreshDecision(
        needed=False,
        reason=f"令牌有效，剩余 {remaining:.0f}s",
        expires_in_s=remaining,
    )


def is_refresh_window_open(
    record: CredentialRecord, *, now: datetime, skew_s: int = DEFAULT_REFRESH_SKEW_S
) -> bool:
    """便捷判断：现在是否处于"应当刷新"的状态。"""

    return refresh_decision(record, now=now, skew_s=skew_s).needed


__all__ = [
    "DEFAULT_REFRESH_SKEW_S",
    "RefreshDecision",
    "TokenRefresher",
    "is_refresh_window_open",
    "refresh_decision",
]
