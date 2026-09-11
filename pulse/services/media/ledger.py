"""素材使用台账（所有者：root）。

业务决策：**只有实际进入生成内容 / 发布才算被召回**（2026-09-11 #1）。
因此台账只记录 `used` 事件，由内容生成或发布成功时调用 ``mark_used``；
检索命中不写台账、不消耗冷却。

存储用标准库 sqlite3：多线程安全（每次操作独立连接）、支持幂等去重。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id   TEXT NOT NULL,
    brand      TEXT NOT NULL,
    content_id TEXT,
    event      TEXT NOT NULL DEFAULT 'used',
    used_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_usage_asset ON media_usage (asset_id, brand, used_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_usage_idem
    ON media_usage (asset_id, brand, event, content_id)
    WHERE content_id IS NOT NULL;
"""


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class RecallLedger:
    """素材使用台账（品牌维度）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def mark_used(
        self,
        asset_id: str,
        *,
        brand: str,
        content_id: str | None = None,
        event: str = "used",
        used_at: datetime | None = None,
    ) -> bool:
        """记录一次使用；同一 (素材, 内容) 重复调用返回 False（幂等）。"""
        stamp = _iso(used_at or datetime.now(timezone.utc))
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO media_usage (asset_id, brand, content_id, event, used_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (asset_id, brand, content_id, event, stamp),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def last_used_at(self, asset_id: str, *, brand: str) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(used_at) AS last_used FROM media_usage"
                " WHERE asset_id = ? AND brand = ?",
                (asset_id, brand),
            ).fetchone()
        return _parse(row["last_used"] if row else None)

    def usage_count(self, asset_id: str, *, brand: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM media_usage WHERE asset_id = ? AND brand = ?",
                (asset_id, brand),
            ).fetchone()
        return int(row["n"]) if row else 0

    def used_within(
        self,
        asset_id: str,
        *,
        brand: str,
        window: timedelta,
        now: datetime | None = None,
    ) -> bool:
        """该素材在窗口内是否被使用过（冷却判定）。"""
        last_used = self.last_used_at(asset_id, brand=brand)
        if last_used is None:
            return False
        reference = now or datetime.now(timezone.utc)
        return reference - last_used < window

    def history(
        self, *, asset_id: str | None = None, brand: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if asset_id:
            clauses.append("asset_id = ?")
            params.append(asset_id)
        if brand:
            clauses.append("brand = ?")
            params.append(brand)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT asset_id, brand, content_id, event, used_at FROM media_usage{where}"
                " ORDER BY used_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]
