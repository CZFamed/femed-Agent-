"""内容生产域的错误类型（所有者：A1）。

统一起见，本域所有异常都继承 ``ContentError``，便于上层（A5 接口层）
一次性捕获并翻译成契约 §7 的统一错误体
``{"error": {"code": ..., "message": ..., "details": {}}}``。
"""

from __future__ import annotations

from typing import Any


class ContentError(Exception):
    """内容生产域异常基类。"""

    #: 映射到 API 错误体的 code
    code = "content_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.message = message
        self.details = dict(details or {})
        super().__init__(message)

    def as_error_body(self) -> dict[str, Any]:
        """按契约 §7 的统一错误体返回（供 A5 直接用）。"""
        return {"error": {"code": self.code, "message": self.message, "details": dict(self.details)}}


class BriefValidationError(ContentError):
    """brief 不合法。``errors`` 里是全部问题，便于一次性改完。"""

    code = "brief_invalid"

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("brief 校验失败：" + "；".join(self.errors), details={"errors": self.errors})


class UnknownPlatformError(ContentError):
    """平台不在白名单内（或本阶段不投入，如 TikTok）。"""

    code = "platform_unsupported"


class UnknownHashtagError(ContentError):
    """话题标签不在白名单内——白名单是防止标签跑偏的唯一闸门。"""

    code = "hashtag_not_allowed"


class TokenBudgetExceeded(ContentError):
    """token 预算熔断。

    契约与派工单都要求**熔断而非静默截断**：超预算时宁可让这一次生成失败，
    也不能悄悄发出去一段被砍掉一半的文案。
    """

    code = "token_budget_exceeded"


class UnknownTaskError(ContentError):
    """模型路由不认识这个任务类型。"""

    code = "router_task_unknown"


class ContentNotFoundError(ContentError):
    """按 ID 找不到内容 / 变体。"""

    code = "content_not_found"


class MediaSelectionError(ContentError):
    """素材选用失败（平台无素材口径、素材库为空等硬错误）。

    注意：**证据缺口不是错误** —— 缺口要正常返回并标注
    ``TODO(need-real-data)``，只有"平台根本没有素材口径"这类才抛异常。
    """

    code = "media_selection_failed"
