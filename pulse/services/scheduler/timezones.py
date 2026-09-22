"""时区解析工具（scheduler 域内部实现）。

排期必须落在**账号所在时区**，并且 `scheduled_at` **必须带偏移**（契约 §2）。
解析失败时显式报错，绝不静默退化成 UTC —— 静默退化会让多时区排期整体漂移。

> 与 `pulse/services/identity/timezones.py` 是同构实现：派工单 §1 要求两个域
> 各自独立可测，因此各自持有一份；若 root 决定上移到 `pulse/shared/`，本地实现可删。
"""

from __future__ import annotations

import re
from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: 目标市场的固定偏移兜底（仅在运行环境缺 tzdata 时使用；不含夏令时规则）
_FALLBACK_OFFSETS: dict[str, timedelta] = {
    "UTC": timedelta(0),
    "Asia/Kolkata": timedelta(hours=5, minutes=30),
    "Asia/Shanghai": timedelta(hours=8),
    "Asia/Taipei": timedelta(hours=8),
    "Europe/Moscow": timedelta(hours=3),
    "America/New_York": timedelta(hours=-5),
    "America/Los_Angeles": timedelta(hours=-8),
}


class UnknownTimezone(ValueError):
    """无法解析的时区名。"""


#: IANA 名称的合法字符集；先做格式白名单再交给 `zoneinfo`
#: （Windows 上 `ZoneInfo("??")` 抛 OSError，且直接当路径用有越界风险）。
_TZ_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+)*$")


def resolve_timezone(name: str) -> tzinfo:
    """把 IANA 时区名解析为 `tzinfo`；无法解析时抛 `UnknownTimezone`。"""

    if not name or not str(name).strip():
        raise UnknownTimezone("时区不能为空：请填写 IANA 名称，如 Asia/Kolkata")

    key = str(name).strip()
    if ".." in key or not _TZ_PATTERN.match(key):
        raise UnknownTimezone(
            f"时区名格式非法：{key!r}（只允许 IANA 名称，如 Asia/Kolkata、America/New_York）"
        )

    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        pass

    fallback = _FALLBACK_OFFSETS.get(key)
    if fallback is not None:
        return timezone(fallback, key)

    raise UnknownTimezone(
        f"未知时区：{key!r}。请使用 IANA 名称（如 Asia/Kolkata、America/New_York、UTC）"
    )


def is_valid_timezone(name: str) -> bool:
    """时区名是否可解析。"""

    try:
        resolve_timezone(name)
    except UnknownTimezone:
        return False
    return True


__all__ = ["UnknownTimezone", "is_valid_timezone", "resolve_timezone"]
