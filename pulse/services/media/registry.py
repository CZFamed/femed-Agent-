"""新增素材登记表（所有者：root）。

为什么需要它：既有图库（`RAG知识库/图片描述/`）里没有任何"入库时间"信息，
而业务决策要求"既有图全部视为老图"（2026-09-11 #5）。因此：

- 登记表里**有**记录 → 新图，`added_at` 生效，享受新鲜度加权；
- 登记表里**没有**记录 → 老图，不加权，但同样参与召回（除非处于冷却期）。

存储为 JSON，写入采用"临时文件 + 原子替换"，避免控制台并发写坏文件。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY_NAME = "_媒体登记表.json"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class MediaRegistry:
    """新增素材登记表。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, dict[str, Any]]:
        """读取登记表；文件不存在时返回空表。"""
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}

    def get(self, asset_id: str) -> dict[str, Any] | None:
        return self.load().get(asset_id)

    def registered_at(self, asset_id: str) -> datetime | None:
        """返回入库时间；老图返回 None。"""
        entry = self.get(asset_id)
        if not entry:
            return None
        raw = entry.get("added_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            return None

    def add(
        self,
        asset_id: str,
        *,
        added_at: datetime,
        file_name: str,
        process: str,
        sub_process: str,
        source_path: str,
        brand: str,
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        """登记一张新图（幂等：同 asset_id 重复登记时保留最早入库时间）。"""
        entries = self.load()
        existing = entries.get(asset_id)
        if existing:
            return existing
        entry = {
            "asset_id": asset_id,
            "added_at": _iso(added_at),
            "file_name": file_name,
            "process": process,
            "sub_process": sub_process,
            "source_path": source_path,
            "brand": brand,
            "content_hash": content_hash,
        }
        entries[asset_id] = entry
        self._write(entries)
        return entry

    def find_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        """按内容哈希查重，避免同一张图重复入库。"""
        for entry in self.load().values():
            if entry.get("content_hash") == content_hash:
                return entry
        return None

    def _write(self, entries: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=".registry-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            tmp_name = handle.name
        os.replace(tmp_name, self.path)
