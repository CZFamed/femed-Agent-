"""发布记录的存储协议与内存实现。

落库由 **A3** 负责（契约 §6 的 `publish_jobs` / `publish_results`），
A2 只依赖这里的最小协议，好处是单测不需要数据库。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from pulse.shared.enums import TERMINAL_JOB_STATUSES, PublishJobStatus
from pulse.shared.models import PublishResult


@dataclass(frozen=True, slots=True)
class PublishRecord:
    """一条发布任务的落库快照（publish_jobs + 最近一条 publish_results）。"""

    unified_post_id: str
    job_id: str
    platform: str
    status: PublishJobStatus = PublishJobStatus.QUEUED
    attempts: int = 0
    platform_post_id: str | None = None
    post_url: str | None = None
    error_class: str | None = None
    error_message: str | None = None
    next_retry_at: datetime | None = None
    finalize_deadline: datetime | None = None
    result: PublishResult | None = None

    @property
    def is_terminal(self) -> bool:
        """是否已到终态（published / failed / rejected / cancelled）。"""

        return self.status in TERMINAL_JOB_STATUSES

    def evolve(self, **changes: object) -> "PublishRecord":
        """返回替换了部分字段的新记录（本类型不可变）。"""

        return replace(self, **changes)


class PublishStore(Protocol):
    """最小存储协议：唯一索引语义 + 读写。"""

    def claim(self, record: PublishRecord) -> bool:
        """按 `publish_jobs.unified_post_id` 唯一索引语义占位；已存在则返回 False。"""

    def get(self, unified_post_id: str) -> PublishRecord | None:
        """按幂等键取记录；不存在返回 None。"""

    def save(self, record: PublishRecord) -> PublishRecord:
        """写回记录（不新增记录，键必须已存在）。"""


class InMemoryPublishStore:
    """进程内实现。

    `claim()` 复刻 `publish_jobs.unified_post_id` 的**唯一索引**语义——
    这是幂等的第一层防线（第二层是 Adapter 的 `find_existing()`）。
    同时统计 `publish_results` 的写入次数，供"重复投递只落一条结果"的断言使用。
    """

    def __init__(self) -> None:
        self._records: dict[str, PublishRecord] = {}
        self._result_writes: dict[str, int] = {}

    def claim(self, record: PublishRecord) -> bool:
        if record.unified_post_id in self._records:
            return False
        self._records[record.unified_post_id] = record
        return True

    def get(self, unified_post_id: str) -> PublishRecord | None:
        return self._records.get(unified_post_id)

    def save(self, record: PublishRecord) -> PublishRecord:
        if record.unified_post_id not in self._records:
            raise KeyError(f"记录不存在，必须先 claim：{record.unified_post_id}")
        self._records[record.unified_post_id] = record
        if record.result is not None:
            self._result_writes[record.unified_post_id] = (
                self._result_writes.get(record.unified_post_id, 0) + 1
            )
        return record

    # -- 仅测试/调试使用 ---------------------------------------------------

    def result_writes(self, unified_post_id: str) -> int:
        """该幂等键累计写入 publish_results 的次数。"""

        return self._result_writes.get(unified_post_id, 0)

    def all_records(self) -> tuple[PublishRecord, ...]:
        """全部记录（无序）。"""

        return tuple(self._records.values())
