"""时区解析工具（identity 域内部实现）。

契约 §6 要求 `accounts.timezone` 是 IANA 名称（如 `Asia/Kolkata`）。
本模块负责把它解析成可用于排期的 `tzinfo`，并在无法解析时**显式报错**：
静默退化成 UTC 会让所有排期整体漂移（契约 §2 / §3.2 明确点名的真实事故）。

> `scheduler` 域有一份等价实现（`pulse/services/scheduler/timezones.py`）。
> 之所以不共用，是因为派工单 §1 要求两个域**各自独立可测**；
> 若 root 认为应上移到 `pulse/shared/`（root 的目录），我这边可以随时删掉本地实现。
"""

from __future__ import annotations

import re
from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class UnknownTimezone(ValueError):
    """无法解析的时区名。"""


#: IANA 名称的合法字符集（`Area/Location` 形式，允许 `Etc/GMT+8` 这类写法）。
#: 先做格式白名单再交给 `zoneinfo`：Windows 上 `ZoneInfo("??")` 会抛 OSError，
#: 而且直接把用户输入当路径存在越界风险。
_TZ_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+)*$")


#: 目标市场的固定偏移兜底：仅在运行环境缺少 tzdata 时才会用到。
#: **固定偏移不处理夏令时**，所以它只是兜底，不是默认路径。
_FALLBACK_OFFSETS: dict[str, timedelta] = {
    "UTC": timedelta(0),
    "Asia/Kolkata": timedelta(hours=5, minutes=30),
    "Asia/Shanghai": timedelta(hours=8),
    "Asia/Taipei": timedelta(hours=8),
    "Europe/Moscow": timedelta(hours=3),
    "America/New_York": timedelta(hours=-5),
    "America/Los_Angeles": timedelta(hours=-8),
}


def resolve_timezone(name: str) -> tzinfo:
    """把 IANA 时区名解析为 `tzinfo`。

    Args:
        name: IANA 时区名，如 `Asia/Kolkata`。

    Returns:
        带夏令时规则的 `ZoneInfo`；系统缺 tzdata 时返回目标市场固定偏移。

    Raises:
        UnknownTimezone: 名称为空或无法解析，且不在兜底表内。
    """

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
    """时区名是否可解析（不抛异常的便捷判断）。"""

    try:
        resolve_timezone(name)
    except UnknownTimezone:
        return False
    return True


__all__ = ["UnknownTimezone", "is_valid_timezone", "resolve_timezone"]
