"""best-time 表（派工单 §3 W1-A3-5）。

键：`account / platform / timezone / weekday / slot`；查询返回的 `scheduled_at`
**必须带时区偏移**（契约 §2），并且要落在**账号所在时区**的本地时刻上——
多时区排期漂移是真实事故，不是理论风险。

表为空时**不编数据**：回退到默认本地时间 `09:30`，并在原因里写明
`TODO(need-real-data)`，等运营侧补实测最佳时段（`AGENTS.md` §3.7）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable

from pulse.services.scheduler.timezones import resolve_timezone

#: 无 best-time 记录时的回退本地时间
DEFAULT_LOCAL_TIME = time(9, 30)

#: 回退时使用的槽位名
FALLBACK_SLOT_NAME = "fallback"

#: 星期名（0 = 周一，与 `datetime.weekday()` 一致）
WEEKDAY_NAMES: tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


@dataclass(frozen=True, slots=True)
class BestTimeSlot:
    """best-time 表的一行。`local_time` 是**账号时区的本地时间**。"""

    account_id: str
    platform: str
    timezone: str
    weekday: int
    slot: str
    local_time: time
    score: float = 1.0

    @property
    def key(self) -> tuple[str, str, str, int, str]:
        """行的唯一键（与派工单 §3 的键定义一致）。"""

        return (self.account_id, self.platform, self.timezone, self.weekday, self.slot)

    def validate(self) -> list[str]:
        """返回全部字段错误（空列表 = 通过）。"""

        errors: list[str] = []
        if not self.account_id:
            errors.append("best_time.account_id 不能为空")
        if not self.platform:
            errors.append("best_time.platform 不能为空")
        if not self.timezone:
            errors.append("best_time.timezone 不能为空")
        else:
            try:
                resolve_timezone(self.timezone)
            except ValueError as exc:
                errors.append(f"best_time.timezone 非法：{exc}")
        if not 0 <= int(self.weekday) <= 6:
            errors.append(f"best_time.weekday 必须在 0..6（0=周一）：{self.weekday}")
        if not self.slot:
            errors.append("best_time.slot 不能为空")
        if not isinstance(self.local_time, time):
            errors.append(f"best_time.local_time 必须是 datetime.time：{self.local_time!r}")
        return errors

    def describe(self) -> str:
        """可读描述（排期原因里用）。"""

        name = WEEKDAY_NAMES[self.weekday] if 0 <= self.weekday <= 6 else str(self.weekday)
        return f"{name} {self.local_time.strftime('%H:%M')}（{self.slot}）"


@dataclass(frozen=True, slots=True)
class ResolvedSlot:
    """一次 best-time 查询的结论。"""

    scheduled_at: datetime
    timezone: str
    slot: str
    used_fallback: bool
    reason: str
    score: float = 0.0

    def to_dict(self) -> dict[str, object]:
        """序列化（`scheduled_at` 为带偏移的 ISO8601）。"""

        return {
            "scheduled_at": self.scheduled_at.isoformat(),
            "timezone": self.timezone,
            "slot": self.slot,
            "used_fallback": self.used_fallback,
            "reason": self.reason,
            "score": self.score,
        }


class BestTimeTable:
    """best-time 查询表（内存实现；生产为 DB 表，接口不变）。

    Args:
        slots: 初始行。
        fallback_local_time: 表内查不到时使用的本地时间。
    """

    def __init__(
        self,
        slots: Iterable[BestTimeSlot] = (),
        *,
        fallback_local_time: time = DEFAULT_LOCAL_TIME,
    ) -> None:
        self._rows: dict[tuple[str, str, str, int, str], BestTimeSlot] = {}
        self.fallback_local_time = fallback_local_time
        self.extend(slots)

    def __len__(self) -> int:
        return len(self._rows)

    def add(self, slot: BestTimeSlot) -> BestTimeSlot:
        """插入/覆盖一行。"""

        errors = slot.validate()
        if errors:
            raise ValueError("; ".join(errors))
        self._rows[slot.key] = slot
        return slot

    def extend(self, slots: Iterable[BestTimeSlot]) -> None:
        """批量插入。"""

        for slot in slots:
            self.add(slot)

    def slots_for(
        self, account_id: str, platform: str, timezone_name: str
    ) -> tuple[BestTimeSlot, ...]:
        """该账号/平台/时区下的全部槽位（按 weekday、local_time 排序）。"""

        rows = [
            row
            for row in self._rows.values()
            if row.account_id == account_id
            and row.platform == str(platform)
            and row.timezone == timezone_name
        ]
        return tuple(sorted(rows, key=lambda r: (r.weekday, r.local_time, r.slot)))

    def lookup(
        self, account_id: str, platform: str, timezone_name: str, weekday: int
    ) -> BestTimeSlot | None:
        """查该星期几的最佳槽位；没有记录返回 None。

        多行时取 `score` 最高者；同分取本地时间更早者（稳定且可预测）。
        """

        rows = [
            row
            for row in self.slots_for(account_id, platform, timezone_name)
            if row.weekday == int(weekday)
        ]
        if not rows:
            return None
        return sorted(rows, key=lambda r: (-r.score, r.local_time, r.slot))[0]

    def resolve(
        self,
        account_id: str,
        platform: str,
        timezone_name: str,
        *,
        after: datetime,
        weekdays_ahead: int = 7,
    ) -> ResolvedSlot:
        """从 `after` 之后找最近的 best-time 时刻。

        Args:
            account_id: 账号 ID。
            platform: 平台。
            timezone_name: 账号时区（IANA）。
            after: 起始时刻（**必须带偏移**）。
            weekdays_ahead: 向后搜索的天数（默认覆盖一周）。

        Returns:
            `ResolvedSlot`，其 `scheduled_at` 带时区偏移、且严格晚于 `after`。
        """

        if after.tzinfo is None:
            raise ValueError("after 必须携带时区偏移（否则多时区排期会漂移）")

        tz = resolve_timezone(timezone_name)
        reference = after.astimezone(tz)

        for offset in range(0, max(1, weekdays_ahead) + 1):
            day = (reference + timedelta(days=offset)).date()
            weekday = day.weekday()
            row = self.lookup(account_id, platform, timezone_name, weekday)
            local_time = row.local_time if row is not None else self.fallback_local_time
            candidate = datetime.combine(day, local_time, tzinfo=tz)
            if candidate <= reference:
                continue

            if row is None:
                return ResolvedSlot(
                    scheduled_at=candidate,
                    timezone=timezone_name,
                    slot=FALLBACK_SLOT_NAME,
                    used_fallback=True,
                    reason=(
                        f"best-time 表无 {platform} / {WEEKDAY_NAMES[weekday]} 记录，"
                        f"回退默认本地时间 {local_time.strftime('%H:%M')}；"
                        "TODO(need-real-data)：待运营侧补充实测最佳时段"
                    ),
                )
            return ResolvedSlot(
                scheduled_at=candidate,
                timezone=timezone_name,
                slot=row.slot,
                used_fallback=False,
                reason=f"命中 best-time 槽位 {row.describe()}（score={row.score:g}）",
                score=row.score,
            )

        raise ValueError(
            f"在 {weekdays_ahead} 天内没有可用的 best-time 时刻（account={account_id}，"
            f"platform={platform}，tz={timezone_name}）"
        )


__all__ = [
    "DEFAULT_LOCAL_TIME",
    "FALLBACK_SLOT_NAME",
    "WEEKDAY_NAMES",
    "BestTimeSlot",
    "BestTimeTable",
    "ResolvedSlot",
]
