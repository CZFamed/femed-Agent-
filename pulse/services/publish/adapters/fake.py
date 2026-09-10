"""Fake Adapter：供其他域与全部单测使用（派工单 §3 W1-A2-5）。

可注入的压力行为覆盖完整 `ErrorClass` 谱：**延迟完成**（pending_finalize）、
**政策拒绝**、**限流**、**401 认证失效**，另外还有同步发布、瞬时故障、
异步处理与 payload 非法。W1 阶段所有测试都必须走它，不发真实请求。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pulse.services.publish.base import PlatformAdapter
from pulse.shared.enums import ErrorClass
from pulse.shared.models import PublishResult, UnifiedPost


class FakeBehaviour(StrEnum):
    """Fake Adapter 的可注入行为。"""

    PENDING_THEN_PUBLISHED = "pending_then_published"  # 延迟完成：先受理，轮询后发布
    PUBLISHED = "published"                            # 同步平台：直接已发布
    POLICY_REJECTED = "policy_rejected"                # 政策拒绝（不可重试）
    RATE_LIMITED = "rate_limited"                      # 限流（可重试）
    AUTH_EXPIRED = "auth_expired"                      # 401（刷新凭据后重试）
    TRANSIENT = "transient"                            # 5xx（可重试）
    MEDIA_PROCESSING = "media_processing"              # 异步处理中（转轮询）
    VALIDATION_ERROR = "validation_error"              # payload 不合法（不可重试）


_ERROR_BEHAVIOURS: dict[FakeBehaviour, tuple[str, ErrorClass]] = {
    FakeBehaviour.POLICY_REJECTED: ("rejected", ErrorClass.POLICY_REJECTED),
    FakeBehaviour.RATE_LIMITED: ("failed", ErrorClass.RATE_LIMITED),
    FakeBehaviour.AUTH_EXPIRED: ("failed", ErrorClass.AUTH_EXPIRED),
    FakeBehaviour.TRANSIENT: ("failed", ErrorClass.TRANSIENT),
    FakeBehaviour.MEDIA_PROCESSING: ("pending_finalize", ErrorClass.MEDIA_PROCESSING),
    FakeBehaviour.VALIDATION_ERROR: ("failed", ErrorClass.VALIDATION_ERROR),
}


class FakeAdapter(PlatformAdapter):
    """不发任何请求的 Adapter。

    除行为注入外，它还记录调用轨迹，让"重复投递不重复发布"这类断言可以直接数次数。
    """

    def __init__(
        self,
        platform: str = "linkedin",
        behaviour: FakeBehaviour | str = FakeBehaviour.PENDING_THEN_PUBLISHED,
        *,
        existing: dict[str, str] | None = None,
        validate_errors: list[str] | None = None,
        finalize_behaviour: FakeBehaviour | str | None = None,
    ) -> None:
        self.platform = platform
        self.behaviour = FakeBehaviour(behaviour)
        self.finalize_behaviour = (
            FakeBehaviour(finalize_behaviour) if finalize_behaviour else None
        )
        self._existing: dict[str, str] = dict(existing or {})
        self._validate_errors = list(validate_errors or [])
        self.publish_calls: list[str] = []
        self.finalize_calls: list[str] = []

    # -- 测试辅助 ----------------------------------------------------------

    def seed_existing(self, unified_post_id: str, platform_post_id: str) -> None:
        """预置"平台上已存在"的映射，用于幂等兜底测试。"""

        self._existing[unified_post_id] = platform_post_id

    @property
    def publish_calls_count(self) -> int:
        """真实发布调用次数（重复投递应保持为 1）。"""

        return len(self.publish_calls)

    # -- PlatformAdapter ---------------------------------------------------

    async def validate(self, post: UnifiedPost) -> list[str]:
        """返回预置的校验错误（默认为空 = 通过）。"""

        return list(self._validate_errors)

    async def find_existing(self, post: UnifiedPost) -> str | None:
        return self._existing.get(post.unified_post_id)

    async def publish(self, post: UnifiedPost, credential: Any) -> PublishResult:
        self.publish_calls.append(post.unified_post_id)
        pid = f"{self.platform}_fake_{len(self.publish_calls)}"

        if self.behaviour is FakeBehaviour.PUBLISHED:
            self._existing[post.unified_post_id] = pid
            return PublishResult(
                ok=True,
                status="published",
                platform_post_id=pid,
                post_url=f"https://example.test/{pid}",
            )

        if self.behaviour is FakeBehaviour.PENDING_THEN_PUBLISHED:
            self._existing[post.unified_post_id] = pid
            return PublishResult(
                ok=True, status="pending_finalize", platform_post_id=pid
            )

        status, error_class = _ERROR_BEHAVIOURS[self.behaviour]
        return PublishResult(
            ok=False,
            status=status,
            error_class=error_class.value,
            error_message=f"fake:{self.behaviour.value}",
        )

    async def poll_finalize(self, platform_post_id: str, credential: Any) -> PublishResult:
        self.finalize_calls.append(platform_post_id)
        behaviour = self.finalize_behaviour or FakeBehaviour.PUBLISHED

        if behaviour is FakeBehaviour.PUBLISHED:
            return PublishResult(
                ok=True,
                status="published",
                platform_post_id=platform_post_id,
                post_url=f"https://example.test/{platform_post_id}",
            )
        if behaviour is FakeBehaviour.PENDING_THEN_PUBLISHED:
            return PublishResult(
                ok=True, status="pending_finalize", platform_post_id=platform_post_id
            )

        status, error_class = _ERROR_BEHAVIOURS[behaviour]
        return PublishResult(
            ok=False,
            status=status,
            platform_post_id=platform_post_id,
            error_class=error_class.value,
            error_message=f"fake:{behaviour.value}",
        )

    async def fetch_metrics(self, platform_post_id: str, credential: Any) -> dict:
        return {}
