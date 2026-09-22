"""配额预检（派工单 §3 W1-A3-4）。

平台日配额是**硬约束**：YouTube 一次视频上传约 **1600** 单位，日配额约 **10000**
（`AGENTS.md` §4 / 派工单 §4）。预检失败时**不投递**，并把可读原因带回给调用方——
绝不"先发出去再看平台报错"，因为那时素材已经上传了一半。

用法（先看后扣，原子在一个方法里）::

    ledger = QuotaLedger()
    decision = ledger.reserve(account_id, "youtube", account.quota_config, now=now)
    if not decision.allowed:
        raise QuotaExceeded(decision.reason, decision=decision)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping

from pulse.services.scheduler.timezones import resolve_timezone

#: YouTube Data API 的日配额与单次上传成本（派工单 §4 的已核实数字）
DEFAULT_DAILY_LIMIT = 10000.0
DEFAULT_UNIT_COSTS: dict[str, float] = {"youtube": 1600.0}

#: 配额窗口默认按 UTC 日切分。
#: TODO(need-real-data)：YouTube 配额实际按**太平洋时间**午夜重置，
#: 待运营确认后把 `quota_window_timezone` 配成 `America/Los_Angeles`。
DEFAULT_WINDOW_TIMEZONE = "UTC"


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """配额策略（可由 `accounts.quota_config` 覆盖）。"""

    daily_limit: float = DEFAULT_DAILY_LIMIT
    unit_costs: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_UNIT_COSTS))
    window_timezone: str = DEFAULT_WINDOW_TIMEZONE
    #: 按平台覆盖的日上限（缺省沿用 `daily_limit`）
    platform_limits: Mapping[str, float] = field(default_factory=dict)

    def cost_for(self, platform: str) -> float:
        """该平台单次操作的配额成本。"""

        return float(self.unit_costs.get(str(platform), 0.0))

    def limit_for(self, platform: str) -> float | None:
        """该平台的日配额上限；未纳入配额的平台返回 None = 不做配额预检。"""

        key = str(platform)
        if key not in self.unit_costs and key not in self.platform_limits:
            return None
        return float(self.platform_limits.get(key, self.daily_limit))

    @classmethod
    def from_config(cls, raw: Mapping[str, Any] | None) -> "QuotaPolicy":
        """从 `accounts.quota_config` 读取覆盖值（缺省即平台默认）。

        支持的键::

            {
              "daily_quota": 10000,                       # 统一上限
              "daily_quota_by_platform": {"youtube": 9000}, # 按平台覆盖
              "unit_costs": {"youtube": 1600},             # 单次成本覆盖
              "quota_window_timezone": "America/Los_Angeles"
            }
        """

        config = dict(raw or {})
        costs = dict(DEFAULT_UNIT_COSTS)
        costs.update(
            {str(k): float(v) for k, v in dict(config.get("unit_costs") or {}).items()}
        )
        return cls(
            daily_limit=float(config.get("daily_quota", DEFAULT_DAILY_LIMIT)),
            unit_costs=costs,
            window_timezone=str(
                config.get("quota_window_timezone") or DEFAULT_WINDOW_TIMEZONE
            ),
            platform_limits={
                str(k): float(v)
                for k, v in dict(config.get("daily_quota_by_platform") or {}).items()
            },
        )

    def limit_for_account(self, platform: str) -> float | None:
        """逐平台覆盖感知的上限查询（`limit_for` 的语义别名）。"""

        return self.limit_for(platform)


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    """一次配额预检的结论（可安全入日志：不含任何凭据）。"""

    allowed: bool
    reason: str
    account_id: str
    platform: str
    cost: float
    limit: float | None
    consumed: float
    remaining: float | None
    window: str
    resets_in_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化（API 直接可返回）。"""

        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "account_id": self.account_id,
            "platform": self.platform,
            "cost": self.cost,
            "limit": self.limit,
            "consumed": self.consumed,
            "remaining": self.remaining,
            "window": self.window,
            "resets_in_s": self.resets_in_s,
        }


class QuotaLedger:
    """滚动窗口用量台账（内存实现；生产替换为 DB 计数器，接口不变）。

    Args:
        policy: 默认策略；单次调用可用 `config` 覆盖。
        now: 时间源（返回带偏移时间）。
    """

    def __init__(
        self,
        policy: QuotaPolicy | None = None,
        *,
        now: Any = None,
    ) -> None:
        self._policy = policy or QuotaPolicy()
        self._now = now
        self._usage: dict[tuple[str, str, str], float] = {}

    @property
    def policy(self) -> QuotaPolicy:
        """当前默认策略。"""

        return self._policy

    # -- 窗口计算 -----------------------------------------------------------

    def policy_for(self, config: Mapping[str, Any] | None) -> QuotaPolicy:
        """取本次调用使用的策略（账号配置优先）。"""

        return QuotaPolicy.from_config(config) if config else self._policy

    def window_key(self, policy: QuotaPolicy, now: datetime) -> tuple[str, datetime]:
        """返回 `(窗口标识, 窗口重置时刻)`。"""

        tz = resolve_timezone(policy.window_timezone)
        local = now.astimezone(tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        day: date = local.date()
        reset_at = datetime.combine(day, time(0, 0), tzinfo=tz) + timedelta(days=1)
        return f"{day.isoformat()} ({policy.window_timezone} 日)", reset_at

    def consumed(
        self,
        account_id: str,
        platform: str,
        *,
        now: datetime,
        config: Mapping[str, Any] | None = None,
    ) -> float:
        """当前窗口已用配额（窗口按本次生效策略的时区切分）。"""

        key, _ = self.window_key(self.policy_for(config), now)
        return self._usage.get((account_id, str(platform), key), 0.0)

    def remaining(
        self,
        account_id: str,
        platform: str,
        *,
        now: datetime,
        config: Mapping[str, Any] | None = None,
    ) -> float | None:
        """当前窗口剩余配额；该平台无配额约束时为 None。"""

        limit = self.policy_for(config).limit_for_account(platform)
        if limit is None:
            return None
        return limit - self.consumed(account_id, platform, now=now, config=config)

    # -- 预检 / 扣减 --------------------------------------------------------

    def precheck(
        self,
        account_id: str,
        platform: str,
        config: Mapping[str, Any] | None = None,
        *,
        now: datetime,
    ) -> QuotaDecision:
        """只做预检，**不扣减**（用于"先看能不能发"）。"""

        policy = self.policy_for(config)
        window, reset_at = self.window_key(policy, now)
        resets_in = (reset_at - now).total_seconds()

        limit = policy.limit_for_account(platform)
        cost = policy.cost_for(platform)
        used = self.consumed(account_id, platform, now=now, config=config)

        if limit is None:
            return QuotaDecision(
                allowed=True,
                reason=f"{platform} 未配置日配额成本，跳过配额预检",
                account_id=account_id,
                platform=str(platform),
                cost=0.0,
                limit=None,
                consumed=used,
                remaining=None,
                window=window,
                resets_in_s=resets_in,
            )

        need = used + cost
        if need > limit:
            return QuotaDecision(
                allowed=False,
                reason=(
                    f"{platform} 日配额不足：本次约需 {cost:.0f} 单位，"
                    f"本窗口已用 {used:.0f}/{limit:.0f}，剩余 {limit - used:.0f} 单位"
                    f"（配额窗口 {window}，{resets_in / 3600:.1f} 小时后重置）。"
                    "为避免发到一半被平台拒绝，本次不投递"
                ),
                account_id=account_id,
                platform=str(platform),
                cost=cost,
                limit=limit,
                consumed=used,
                remaining=limit - used,
                window=window,
                resets_in_s=resets_in,
            )

        return QuotaDecision(
            allowed=True,
            reason=(
                f"配额充足：{need:.0f}/{limit:.0f}（本次 {cost:.0f} 单位，"
                f"窗口 {window}）"
            ),
            account_id=account_id,
            platform=str(platform),
            cost=cost,
            limit=limit,
            consumed=used,
            remaining=limit - need,
            window=window,
            resets_in_s=resets_in,
        )

    def reserve(
        self,
        account_id: str,
        platform: str,
        config: Mapping[str, Any] | None = None,
        *,
        now: datetime,
    ) -> QuotaDecision:
        """预检通过则扣减并返回结论；不通过则原样返回（不扣减）。

        返回的 `consumed` / `remaining` 是**扣减后**的值（"这次发完会用掉多少"），
        被拦时则是当前值。
        """

        decision = self.precheck(account_id, platform, config, now=now)
        if decision.allowed and decision.limit is not None:
            policy = self.policy_for(config)
            key, _ = self.window_key(policy, now)
            slot = (account_id, str(platform), key)
            self._usage[slot] = self._usage.get(slot, 0.0) + decision.cost
            used_after = self._usage[slot]
            return replace(
                decision,
                consumed=used_after,
                remaining=decision.limit - used_after,
                reason=(
                    f"配额已扣减：{used_after:.0f}/{decision.limit:.0f}"
                    f"（本次 {decision.cost:.0f} 单位，窗口 {decision.window}）"
                ),
            )
        return decision

    def reset(self, account_id: str | None = None) -> None:
        """清空台账（运维/测试用）。"""

        if account_id is None:
            self._usage.clear()
            return
        for slot in [s for s in self._usage if s[0] == account_id]:
            del self._usage[slot]


__all__ = [
    "DEFAULT_DAILY_LIMIT",
    "DEFAULT_UNIT_COSTS",
    "DEFAULT_WINDOW_TIMEZONE",
    "QuotaDecision",
    "QuotaLedger",
    "QuotaPolicy",
]
