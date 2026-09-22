"""identity ↔ scheduler 的可选接线适配器。

两个域互不 import（派工单 §1：各自独立可测），部署接线时由本模块把它们扣在一起：

* `IdentityAccountRegistry` —— 把 `IdentityService` 适配成 scheduler 的 `AccountRegistry`
  （`get_account` 抛 `AccountNotFound`，协议要求返回 `None`，在这里转换）；
* `wire_account_status_listener` —— 把调度器注册为账号状态监听者，
  账号 `paused` / `revoked` 时**即时挂起该账号全部待发任务**（派工单 §3 W1-A3-9）。

本模块只在部署/接线测试里导入；两个域各自的单测都不依赖它。
"""

from __future__ import annotations

from typing import Any

from pulse.services.identity.errors import AccountNotFound
from pulse.services.identity.service import IdentityService
from pulse.services.scheduler.dispatcher import Dispatcher


class IdentityAccountRegistry:
    """把 identity 的账号查询适配成 scheduler 需要的协议。"""

    def __init__(self, service: IdentityService) -> None:
        self._service = service

    @property
    def service(self) -> IdentityService:
        """被适配的 identity 服务。"""

        return self._service

    def account_view(self, account_id: str) -> Any:
        """按 ID 取账号视图；不存在返回 None（协议要求，不抛异常）。"""

        try:
            return self._service.get_account(account_id)
        except AccountNotFound:
            return None


def wire_account_status_listener(
    identity: IdentityService, dispatcher: Dispatcher
) -> IdentityService:
    """把调度器的熔断器注册为账号状态监听者。"""

    identity.add_listener(dispatcher)
    return identity


__all__ = ["IdentityAccountRegistry", "wire_account_status_listener"]
