"""scheduler 域对外的结构性协议（避免硬编码依赖其他域的具体类）。

scheduler 需要知道"这个账号是什么平台、什么时区、什么状态、配额怎么配"。
它**不 import** identity 域，而是按下面的协议消费一个账号视图；
identity 的 `Account` 天然满足 `AccountView`（字段同名），
接线见 `pulse/services/scheduler/identity_adapter.py`。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class AccountView(Protocol):
    """账号视图（identity `Account` 的结构性超集）。"""

    id: str
    platform: str
    timezone: str
    status: str
    quota_config: Mapping[str, Any]


@runtime_checkable
class AccountRegistry(Protocol):
    """账号查询入口。**不存在时返回 None**（不抛异常）。"""

    def account_view(self, account_id: str) -> AccountView | None:
        """按 ID 取账号视图。"""


@runtime_checkable
class PublishDispatchSink(Protocol):
    """`pulse.publish.dispatch` 的消费端（A2 发布网关；测试用记录桩）。

    返回 `PublishResult`、结果字典，或带 `.result` 的对象（如 A2 的 `DispatchOutcome`），
    统一由 `pulse/services/scheduler/state.py::coerce_publish_result` 收敛。
    """

    def dispatch(self, job_id: str, unified_post_id: str) -> Any:
        """执行发布并返回结果。"""


__all__ = ["AccountRegistry", "AccountView", "PublishDispatchSink"]
