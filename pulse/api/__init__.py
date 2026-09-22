"""接口层（A5）：契约 §7 的 11 个端点 + 审批工作流 + 半自动导出入口。

设计口径：

* **框架无关**：`ApiApp.handle()` 直接吃 `(method, path, body)` 返回 `ApiResponse`，
  方便在任意运行时（标准库 http.server、Cloudflare Worker、测试）上挂载。
* **不依赖具体 Adapter**：只调用各域的入口，具体平台实现由部署层注入。
* **统一错误体**：`{"error": {"code", "message", "details"}}`（契约 §7）。
* **KPI 口径**：看板用询盘与触达，不用点赞数。
"""

from pulse.api.app import ApiApp, PostRegistry
from pulse.api.approvals import (
    ACTIONS,
    APPROVE,
    BATCH_APPROVE,
    NEEDS_REVISION,
    REJECT,
    ApprovalRecord,
    ApprovalService,
    BatchOutcome,
)
from pulse.api.errors import (
    ApiError,
    ApiResponse,
    bad_request,
    conflict,
    forbidden,
    map_exception,
    not_found,
    too_many,
    unsupported,
)

__all__ = [
    "ACTIONS",
    "APPROVE",
    "ApiApp",
    "ApiError",
    "ApiResponse",
    "ApprovalRecord",
    "ApprovalService",
    "BATCH_APPROVE",
    "BatchOutcome",
    "NEEDS_REVISION",
    "PostRegistry",
    "REJECT",
    "bad_request",
    "conflict",
    "forbidden",
    "map_exception",
    "not_found",
    "too_many",
    "unsupported",
]
