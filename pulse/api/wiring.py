"""部署接线（所有者：A5）。

把六个域接成一个能跑的服务：**账号只有一个真源**（identity 域），调度域通过
`IdentityAccountRegistry` 读账号视图，避免"两份账号数据各自漂移"。

两个入口：

* `build_service_app(...)`：**生产接线**——调用方把已经建好的各域服务传进来；
* `build_demo_app(...)`：**本地联调/演示接线**——内存存储 + 假队列 + 一个演示账号，
  用于 `python -m pulse.api.http_server` 直接起服务试端点（**不含任何真实凭据**）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from pulse.api.app import ApiApp
from pulse.api.approvals import ApprovalService
from pulse.services.compliance import ComplianceService
from pulse.services.content import AccountBinding, ContentService
from pulse.services.publish.semi_auto import SemiAutoBundleExporter
from pulse.services.scheduler import (
    Dispatcher,
    EagerTaskQueue,
    InMemoryJobStore,
    InMemoryScheduleStore,
    QuotaLedger,
)
from pulse.shared.enums import Platform

DEMO_ACCOUNT_ID = "acct_demo_linkedin"
DEMO_ACCOUNT_URN = "urn:li:organization:demo-placeholder"


@dataclass(frozen=True)
class AccountSnapshot:
    """调度域要的账号视图（protocol: id/platform/timezone/status/quota_config）。"""

    id: str
    platform: str
    timezone: str
    status: str
    quota_config: Mapping[str, Any]


class IdentityAccountRegistry:
    """账号注册表：**真源是 identity 域**，这里只做字段翻译。

    identity 的 `Account.status` 是枚举，而调度域的协议要字符串，所以必须显式取值，
    不能直接把枚举丢过去——否则 `str(status)` 会得到 `AccountStatus.ACTIVE` 这种字符串，
    账号状态检查会永远不通过。
    """

    def __init__(self, identity: Any) -> None:
        self._identity = identity

    def account_view(self, account_id: str) -> AccountSnapshot | None:
        try:
            account = self._identity.get_account(account_id)
        except Exception:  # noqa: BLE001 - 协议要求"不存在返回 None"
            return None
        platform = getattr(account, "platform", "")
        return AccountSnapshot(
            id=str(account.id),
            platform=platform.value if hasattr(platform, "value") else str(platform),
            timezone=str(account.timezone),
            status=_status_text(account),
            quota_config=dict(getattr(account, "quota_config", {}) or {}),
        )


def _status_text(account: Any) -> str:
    value = getattr(account, "status_value", None) or getattr(account, "status", "")
    return value.value if hasattr(value, "value") else str(value)


def build_service_app(
    *,
    content: ContentService,
    identity: Any,
    compliance: ComplianceService | None = None,
    queue: Any | None = None,
    quota: QuotaLedger | None = None,
    exporter: Any | None = None,
    bindings: Mapping[str, AccountBinding] | None = None,
    clock: Any | None = None,
    post_registry: Any | None = None,
) -> ApiApp:
    """生产接线：各域服务由调用方注入，这里只负责按契约把它们扣起来。"""
    compliance = compliance or ComplianceService()
    dispatcher = Dispatcher(
        accounts=IdentityAccountRegistry(identity),
        schedules=InMemoryScheduleStore(),
        jobs=InMemoryJobStore(),
        queue=queue or EagerTaskQueue(),
        quota=quota or QuotaLedger(),
        now=clock or (lambda: datetime.now(timezone.utc)),
    )
    app = ApiApp(
        content=content,
        approvals=ApprovalService(content=content.store, compliance=compliance),
        dispatcher=dispatcher,
        identity=identity,
        compliance=compliance,
        exporter=exporter or SemiAutoBundleExporter(),
        bindings=dict(bindings or {}),
        clock=clock or (lambda: datetime.now(timezone.utc)),
    )
    if post_registry is not None:  # pragma: no cover - 预留：外部登记表
        app.posts = post_registry
    return app


def build_demo_app(*, account_id: str = DEMO_ACCOUNT_ID) -> ApiApp:
    """演示接线：内存存储 + 假队列 + 一个 LinkedIn 演示账号。

    ⚠️ 明确标注为**演示**：

    * `author_urn` 是占位值（`urn:li:organization:demo-placeholder`），不是真实公司页；
    * 队列是 eager 记录式，**不会真的发到平台**；
    * 没有平台凭据，OAuth 端点会如实返回"缺配置"。

    它的用途只有一个：让这 11 个端点能被真实 HTTP 调起来验证链路，而不是替代生产接线。
    """
    from pulse.services.identity import IdentityService

    clock = lambda: datetime.now(timezone.utc)  # noqa: E731 - 演示用简写
    identity = IdentityService(now=clock)
    identity.create_account(
        Platform.LINKEDIN,
        "FAMED demo page（演示用）",
        "Asia/Kolkata",
        account_id=account_id,
        region="India",
    )
    binding = AccountBinding(
        account_id=account_id,
        platform=Platform.LINKEDIN,
        options={"author_urn": DEMO_ACCOUNT_URN, "linkedin_visibility": "PUBLIC"},
    )
    content = ContentService(
        accounts={"linkedin": binding},
    )
    return build_service_app(
        content=content,
        identity=identity,
        bindings={"linkedin": binding},
        clock=clock,
    )
