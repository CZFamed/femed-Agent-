"""模型路由与 token 预算熔断（所有者：A1，任务包 W1-A1-6）。

两件事，都是需求文档里的硬要求：

1. **模型路由**：贵模型写长文、轻模型做改写与自评（需求 §5.2 技术栈"模型路由"）。
2. **token 预算熔断**：默认 60k、可配置；**超预算要熔断，绝不静默截断**
   （派工单 W1-A1-6 的 DoD 原文）。

熔断的实现口径：``TokenBudget.consume`` 在超出上限时抛
``TokenBudgetExceeded``，并带上"已用 / 上限 / 本次请求"三个数字——
运营看到的是"这次生成没做完，因为预算不够"，而不是"文案莫名少了一段"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from pulse.services.content.brief import DEFAULT_TOKEN_BUDGET
from pulse.services.content.errors import TokenBudgetExceeded, UnknownTaskError

#: 任务类型：长文生成 / 改写 / 自评。自评只做一轮（需求 §3.3："不做无限采样"）
TASK_LONG_FORM = "long_form"
TASK_REWRITE = "rewrite"
TASK_SELF_REVIEW = "self_review"


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """一个任务类型对应的模型与输出上限。"""

    task: str
    model: str
    max_output_tokens: int


#: 默认路由表：贵模型写长文，轻模型做改写与自评
DEFAULT_ROUTES: Mapping[str, ModelRoute] = {
    TASK_LONG_FORM: ModelRoute(task=TASK_LONG_FORM, model="heavy-writer", max_output_tokens=4_000),
    TASK_REWRITE: ModelRoute(task=TASK_REWRITE, model="light-rewriter", max_output_tokens=1_500),
    TASK_SELF_REVIEW: ModelRoute(task=TASK_SELF_REVIEW, model="light-reviewer", max_output_tokens=800),
}


class TokenBudget:
    """单条 brief 的 token 预算账本。"""

    def __init__(self, limit: int = DEFAULT_TOKEN_BUDGET) -> None:
        if limit <= 0:
            raise ValueError(f"token 预算必须为正数：{limit}")
        self.limit = int(limit)
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    def can_afford(self, tokens: int) -> bool:
        return tokens >= 0 and self.used + tokens <= self.limit

    def consume(self, tokens: int, *, task: str = "") -> None:
        """记账；超出预算即熔断。

        Raises:
            TokenBudgetExceeded: 已用 + 本次 > 上限。
        """
        if tokens < 0:
            raise ValueError(f"token 用量不能为负：{tokens}")
        if not self.can_afford(tokens):
            raise TokenBudgetExceeded(
                f"token 预算不足已熔断（任务 {task or '未标注'}）："
                f"已用 {self.used}，本次需要 {tokens}，上限 {self.limit}",
                details={"used": self.used, "requested": tokens, "limit": self.limit, "task": task},
            )
        self.used += int(tokens)

    def as_payload(self) -> dict[str, int]:
        return {"used": self.used, "limit": self.limit, "remaining": self.remaining}


@dataclass
class ModelRouter:
    """按任务类型选模型，并统一走预算账本。"""

    budget: TokenBudget = field(default_factory=TokenBudget)
    routes: Mapping[str, ModelRoute] = field(default_factory=lambda: dict(DEFAULT_ROUTES))

    def route(self, task: str) -> ModelRoute:
        """取路由；未知任务抛 ``UnknownTaskError``（不静默退化到默认模型）。"""
        found = self.routes.get(str(task or "").strip())
        if found is None:
            raise UnknownTaskError(
                f"未知任务类型 {task!r}（可用：{'、'.join(sorted(self.routes))}）",
                details={"available": sorted(self.routes)},
            )
        return found

    def charge(self, task: str, tokens: int) -> ModelRoute:
        """选路由并记账；预算不足即熔断（先选路由，未知任务不会白扣预算）。"""
        route = self.route(task)
        self.budget.consume(tokens, task=task)
        return route

    def can_afford(self, tokens: int) -> bool:
        return self.budget.can_afford(tokens)

    def as_payload(self) -> dict[str, object]:
        return {
            "budget": self.budget.as_payload(),
            "routes": {
                task: {"model": item.model, "max_output_tokens": item.max_output_tokens}
                for task, item in sorted(self.routes.items())
            },
        }
