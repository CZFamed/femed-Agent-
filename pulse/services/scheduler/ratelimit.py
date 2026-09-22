"""令牌桶限流 + 发布冷却（派工单 §3 W1-A3-3）。

两个闸门一起放行才算通过：

* **冷却**（人类语义）—— 同一账号同一平台刚发过就别再发，键 `account/platform`；
* **令牌桶**（突发保护）—— 容量与补充速率来自 `accounts.quota_config`。

两者都由配置驱动：`account.quota_config` 形如::

    {"rate": {"capacity": 5, "refill_per_minute": 1}, "cooldown_seconds": 1800}

配置缺失时用默认值（容量 5、每分钟补 1 个、冷却 30 分钟），
**默认值偏保守**：宁可让运营等一下，也不要在平台上打出疑似机器人节奏。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_CAPACITY = 5.0
DEFAULT_REFILL_PER_MINUTE = 1.0
DEFAULT_COOLDOWN_S = 1800.0


def rate_key(account_id: str, platform: str) -> str:
    """限流键：账号 + 平台（同一账号在不同平台互不影响）。"""

    return f"{account_id}:{platform}"


@dataclass(frozen=True, slots=True)
class RateConfig:
    """限流配置（可由 `accounts.quota_config` 覆盖）。"""

    capacity: float = DEFAULT_CAPACITY
    refill_per_minute: float = DEFAULT_REFILL_PER_MINUTE
    cooldown_seconds: float = DEFAULT_COOLDOWN_S

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError(f"capacity 必须为正数：{self.capacity}")
        if self.refill_per_minute < 0:
            raise ValueError(f"refill_per_minute 不能为负数：{self.refill_per_minute}")
        if self.cooldown_seconds < 0:
            raise ValueError(f"cooldown_seconds 不能为负数：{self.cooldown_seconds}")

    @property
    def refill_per_second(self) -> float:
        """每秒补充的令牌数。"""

        return self.refill_per_minute / 60.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "RateConfig":
        """从 `quota_config` 读取限流配置（嵌套 `rate` 或平铺两种写法都支持）。"""

        config = dict(raw or {})
        rate = dict(config.get("rate") or {})
        return cls(
            capacity=float(rate.get("capacity", config.get("capacity", DEFAULT_CAPACITY))),
            refill_per_minute=float(
                rate.get(
                    "refill_per_minute",
                    config.get("refill_per_minute", DEFAULT_REFILL_PER_MINUTE),
                )
            ),
            cooldown_seconds=float(
                rate.get(
                    "cooldown_seconds",
                    config.get("cooldown_seconds", DEFAULT_COOLDOWN_S),
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class RateDecision:
    """限流结论（可安全入日志）。"""

    allowed: bool
    reason: str
    retry_after_s: float | None = None
    remaining_tokens: float | None = None


class TokenBucket:
    """按 key 隔离的令牌桶，惰性补充（不需要后台线程）。

    每次 `try_acquire` 先按经过的时间补令牌，再尝试扣减，
    因此时间必须由调用方显式传入（可注入固定时钟，单测可复现）。
    """

    def __init__(
        self,
        *,
        capacity: float,
        refill_per_second: float,
        initial: float | None = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity 必须为正数：{capacity}")
        if refill_per_second < 0:
            raise ValueError(f"refill_per_second 不能为负数：{refill_per_second}")
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self._initial = self.capacity if initial is None else float(initial)
        self._tokens: dict[str, float] = {}
        self._stamps: dict[str, Any] = {}

    def _refill(self, key: str, now: Any) -> None:
        """按经过时间补充令牌（懒计算）。"""

        tokens = self._tokens.get(key, self._initial)
        previous = self._stamps.get(key)
        if previous is not None and self.refill_per_second > 0:
            elapsed = (now - previous).total_seconds()
            if elapsed > 0:
                tokens = min(self.capacity, tokens + elapsed * self.refill_per_second)
        self._tokens[key] = tokens
        self._stamps[key] = now

    def tokens(self, key: str, *, now: Any) -> float:
        """当前可用令牌数（只读，会更新补充时间戳）。"""

        self._refill(key, now)
        return self._tokens[key]

    def try_acquire(self, key: str, *, now: Any, amount: float = 1.0) -> tuple[bool, float]:
        """尝试取走令牌，返回 `(是否成功, 建议等待秒数)`。"""

        if amount <= 0:
            raise ValueError(f"amount 必须为正数：{amount}")
        self._refill(key, now)
        tokens = self._tokens[key]
        if tokens >= amount:
            self._tokens[key] = tokens - amount
            return True, 0.0

        if self.refill_per_second <= 0:
            # 容量耗尽且不再补充：只能等人工重置
            return False, float("inf")
        missing = amount - tokens
        return False, missing / self.refill_per_second

    def reset(self, key: str | None = None) -> None:
        """重置某个 key（或全部）。"""

        if key is None:
            self._tokens.clear()
            self._stamps.clear()
            return
        self._tokens.pop(key, None)
        self._stamps.pop(key, None)


class CooldownTracker:
    """发布冷却：记录每个 key 的最近一次投递时刻。"""

    def __init__(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"冷却秒数不能为负数：{seconds}")
        self.seconds = float(seconds)
        self._last: dict[str, Any] = {}

    def allows(self, key: str, *, now: Any) -> tuple[bool, float]:
        """是否已过冷却；返回 `(是否放行, 还需等待秒数)`。"""

        if self.seconds <= 0:
            return True, 0.0
        last = self._last.get(key)
        if last is None:
            return True, 0.0
        elapsed = (now - last).total_seconds()
        remaining = self.seconds - elapsed
        if remaining <= 0:
            return True, 0.0
        return False, remaining

    def mark(self, key: str, *, now: Any) -> None:
        """记录一次投递（冷却起点）。"""

        self._last[key] = now

    def last_at(self, key: str) -> Any:
        """最近一次投递时刻（无记录返回 None）。"""

        return self._last.get(key)

    def reset(self, key: str | None = None) -> None:
        """清空冷却记录。"""

        if key is None:
            self._last.clear()
            return
        self._last.pop(key, None)


class RateLimiter:
    """令牌桶 + 冷却的组合闸门。

    判定顺序刻意是"先冷却、后令牌桶"：冷却拒绝的原因对运营最直观
    （"刚刚发过，还要等 27 分钟"），而令牌桶拒绝更像系统保护。
    """

    def __init__(
        self,
        *,
        config: RateConfig | None = None,
        bucket: TokenBucket | None = None,
        cooldown: CooldownTracker | None = None,
    ) -> None:
        self.config = config or RateConfig()
        self.bucket = bucket or TokenBucket(
            capacity=self.config.capacity,
            refill_per_second=self.config.refill_per_second,
        )
        self.cooldown = cooldown or CooldownTracker(self.config.cooldown_seconds)

    @classmethod
    def from_config(cls, raw: Mapping[str, Any] | None) -> "RateLimiter":
        """按 `accounts.quota_config` 构造。"""

        return cls(config=RateConfig.from_mapping(raw))

    def check(self, key: str, *, now: Any) -> RateDecision:
        """只读检查（不扣令牌、不记冷却）。"""

        allowed, remaining = self.cooldown.allows(key, now=now)
        if not allowed:
            return RateDecision(
                allowed=False,
                reason=(
                    f"发布冷却中：冷却窗口 {self.config.cooldown_seconds:.0f}s，"
                    f"还需 {remaining:.0f}s"
                ),
                retry_after_s=remaining,
            )

        tokens = self.bucket.tokens(key, now=now)
        if tokens < 1.0:
            retry = (
                float("inf")
                if self.config.refill_per_second <= 0
                else (1.0 - tokens) / self.config.refill_per_second
            )
            return RateDecision(
                allowed=False,
                reason=(
                    f"令牌桶不足：容量 {self.config.capacity:.0f}，"
                    f"补充 {self.config.refill_per_minute:.2f}/分钟，当前 {tokens:.2f}"
                ),
                retry_after_s=retry,
                remaining_tokens=tokens,
            )
        return RateDecision(
            allowed=True,
            reason=f"通过：令牌桶剩余 {tokens:.2f}，冷却已过",
            remaining_tokens=tokens,
        )

    def acquire(self, key: str, *, now: Any) -> RateDecision:
        """原子地"检查 + 扣令牌 + 记冷却"。失败时状态不变。"""

        allowed, remaining = self.cooldown.allows(key, now=now)
        if not allowed:
            return RateDecision(
                allowed=False,
                reason=(
                    f"发布冷却中：冷却窗口 {self.config.cooldown_seconds:.0f}s，"
                    f"还需 {remaining:.0f}s"
                ),
                retry_after_s=remaining,
            )

        ok, retry_after = self.bucket.try_acquire(key, now=now, amount=1.0)
        if not ok:
            return RateDecision(
                allowed=False,
                reason=(
                    f"令牌桶不足：容量 {self.config.capacity:.0f}，"
                    f"补充 {self.config.refill_per_minute:.2f}/分钟"
                ),
                retry_after_s=retry_after,
                remaining_tokens=self.bucket.tokens(key, now=now),
            )

        self.cooldown.mark(key, now=now)
        return RateDecision(
            allowed=True,
            reason="通过：已扣减 1 个令牌并进入冷却窗口",
            remaining_tokens=self.bucket.tokens(key, now=now),
        )

    def reset(self, key: str | None = None) -> None:
        """清空限流状态（运维用）。"""

        self.bucket.reset(key)
        self.cooldown.reset(key)


__all__ = [
    "DEFAULT_CAPACITY",
    "DEFAULT_COOLDOWN_S",
    "DEFAULT_REFILL_PER_MINUTE",
    "CooldownTracker",
    "RateConfig",
    "RateDecision",
    "RateLimiter",
    "TokenBucket",
    "rate_key",
]
