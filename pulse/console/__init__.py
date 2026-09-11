"""平台控制台（所有者：root / 原 A5 域，2026-09-11 归口）。

当前提供素材库可视化控制台：上传实拍图入库、查看冷却状态、容量红色预警。
"""

from pulse.console.media_console import MediaConsoleApp, create_server

__all__ = ["MediaConsoleApp", "create_server"]
