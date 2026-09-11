"""平台控制台（所有者：root / 原 A5 域，2026-09-11 归口）。

当前提供素材库可视化控制台：上传实拍图入库、查看冷却状态、容量红色预警。

启动器（``launcher``）采用惰性导出：``python -m pulse.console.launcher`` 时
不会在包初始化阶段重复导入自身，避免 runpy 的 "found in sys.modules" 警告。
"""

from typing import Any

from pulse.console.media_console import MediaConsoleApp, create_server

__all__ = [
    "ConsolePaths",
    "MediaConsoleApp",
    "build_app",
    "build_url",
    "create_server",
    "pick_port",
    "resolve_paths",
    "run",
]

_LAZY_EXPORTS = frozenset(
    {"ConsolePaths", "build_app", "build_url", "pick_port", "resolve_paths", "run"}
)


def __getattr__(name: str) -> Any:
    """按需从启动器导入（PEP 562）。"""
    if name in _LAZY_EXPORTS:
        from pulse.console import launcher

        return getattr(launcher, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
