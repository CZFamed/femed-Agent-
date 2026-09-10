"""A2 发布网关的抽象（契约 §4，签名逐字对齐）。

`SemiAutoExporter` 与 `PlatformAdapter` 是**平级**的 P0 能力，不是降级方案：
MVP 阶段所有未过审平台（Facebook 群组等）全靠它出货。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable

from pulse.shared.models import PublishResult, SemiAutoBundle, UnifiedPost


@runtime_checkable
class Credential(Protocol):
    """凭据句柄（契约 §6 `credentials`）。

    实现由 **A3 identity 域**提供：密文存 Vault/KMS，业务层只在发请求的一瞬间取明文。
    A2 只依赖这三个成员，**不得**假设凭据的存储形式。
    """

    account_id: str
    provider: str

    def access_token(self) -> str:
        """取出访问令牌明文。**调用方不得写入日志、异常或测试输出**。"""


class PlatformAdapter(ABC):
    """每个平台一个实现。所有方法必须幂等（契约 §4）。"""

    platform: str

    @abstractmethod
    async def validate(self, post: UnifiedPost) -> list[str]:
        """发布前校验 payload 合法性，返回错误消息列表（空 = 通过）。不发起网络请求。"""

    @abstractmethod
    async def find_existing(self, post: UnifiedPost) -> str | None:
        """幂等兜底：查平台是否已存在该 unified_post_id 的贴文，返回 platform_post_id。

        平台不支持透传幂等键时必须实现。
        """

    @abstractmethod
    async def publish(self, post: UnifiedPost, credential: Credential) -> PublishResult:
        """执行发布。返回 publishing / pending_finalize / published / failed / rejected。"""

    @abstractmethod
    async def poll_finalize(self, platform_post_id: str, credential: Credential) -> PublishResult:
        """收敛 pending_finalize 的终态。平台为同步返回时返回 ok=True, status='published'。"""

    @abstractmethod
    async def fetch_metrics(self, platform_post_id: str, credential: Credential) -> dict:
        """FR-8 数据回捞（P1）。M1/M2 返回 {}。"""


class SemiAutoExporter(ABC):
    """生成可复制内容 + 平台官方发布入口，供人工确认后发布（契约 §4）。"""

    @abstractmethod
    def export(self, post: UnifiedPost) -> SemiAutoBundle:
        """返回 {text, media_paths, deep_link, checklist}。"""


def options_of(post: UnifiedPost) -> dict[str, Any]:
    """安全取 `post.options`（契约里是 Mapping，可能缺省或为 None）。"""

    return dict(post.options or {})
