"""A5 接口层测试夹具：真域（内容/调度/账号/合规）+ 假外部（队列、导出器）。"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from pulse.api import ApiApp, ApprovalService
from pulse.services.compliance import ComplianceService
from pulse.services.content import AccountBinding, ContentService
from pulse.services.identity import IdentityService
from pulse.services.publish.semi_auto import SemiAutoBundleExporter
from pulse.services.scheduler import (
    Dispatcher,
    EagerTaskQueue,
    InMemoryJobStore,
    InMemoryScheduleStore,
    QuotaLedger,
)
from pulse.shared.models import SemiAutoBundle

ACCOUNT_ID = "acct_li_01"


class FrozenClock:
    """可推进的固定时钟（返回带偏移时间）。"""

    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> datetime:
        self.value = self.value + timedelta(**kwargs)
        return self.value


@dataclass
class AccountView:
    """调度域眼里的账号视图（结构等同 identity 的 Account）。"""

    id: str
    platform: str = "linkedin"
    timezone: str = "Asia/Kolkata"
    status: str = "active"
    quota_config: Mapping[str, Any] = field(default_factory=dict)


class FakeRegistry:
    def __init__(self, accounts: tuple[AccountView, ...] = ()) -> None:
        self.rows = {item.id: item for item in accounts}

    def account_view(self, account_id: str) -> AccountView | None:
        return self.rows.get(account_id)


class FakeExporter(SemiAutoBundleExporter):
    """半自动导出器：真实现 + 记录调用（不发网络请求）。"""

    def __init__(self) -> None:
        super().__init__(media_url_resolver=lambda url: f"download://{url.split('/')[-1]}")
        self.calls: list[str] = []

    def export(self, post: Any) -> SemiAutoBundle:
        self.calls.append(post.unified_post_id)
        return super().export(post)


@pytest.fixture()
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc))


@pytest.fixture()
def registry() -> FakeRegistry:
    return FakeRegistry((AccountView(id=ACCOUNT_ID),))


@pytest.fixture()
def dispatcher(clock, registry) -> Dispatcher:
    return Dispatcher(
        accounts=registry,
        schedules=InMemoryScheduleStore(),
        jobs=InMemoryJobStore(),
        queue=EagerTaskQueue(),
        quota=QuotaLedger(),
        now=clock,
    )


@pytest.fixture()
def binding() -> AccountBinding:
    from pulse.shared.enums import Platform

    return AccountBinding(
        account_id=ACCOUNT_ID,
        platform=Platform.LINKEDIN,
        options={"author_urn": "urn:li:organization:12345", "linkedin_visibility": "PUBLIC"},
    )


@pytest.fixture()
def content(binding) -> ContentService:
    return ContentService(accounts={"linkedin": binding})


@pytest.fixture()
def compliance() -> ComplianceService:
    return ComplianceService()


@pytest.fixture()
def identity(clock) -> IdentityService:
    service = IdentityService(now=clock)
    service.create_account(
        "linkedin", "FAMED company page", "Asia/Kolkata", account_id=ACCOUNT_ID
    )
    return service


@pytest.fixture()
def exporter() -> FakeExporter:
    return FakeExporter()


@pytest.fixture()
def app(content, compliance, dispatcher, identity, exporter, binding, clock) -> ApiApp:
    return ApiApp(
        content=content,
        approvals=ApprovalService(content=content.store, compliance=compliance),
        dispatcher=dispatcher,
        identity=identity,
        compliance=compliance,
        exporter=exporter,
        bindings={"linkedin": binding},
        clock=clock,
    )


BRIEF: dict[str, Any] = {
    "brief_id": "b_a5_0001",
    "topic": "Valve body castings with pre-machining",
    "target_audience": "machine tool OEMs in India without an in-house foundry",
    "platforms": ["linkedin"],
    "cta": "Send us your drawing",
}


@pytest.fixture()
def created(app) -> dict[str, Any]:
    """一条已生成、已登记 UnifiedPost 的草稿变体。"""
    response = app.handle("POST", "/api/v1/briefs", body=dict(BRIEF), actor="ops")
    assert response.status == 201, response.body
    return response.body


@pytest.fixture()
def approved(app, created) -> str:
    """把草稿走到 approved，返回 variant_id。"""
    variant_id = created["variants"][0]["id"]
    app.handle(
        "PATCH", f"/api/v1/variants/{variant_id}/status", body={"action": "submit", "actor": "ops"}
    )
    response = app.handle(
        "PATCH",
        f"/api/v1/variants/{variant_id}/status",
        body={"action": "approve", "actor": "brand_reviewer"},
    )
    assert response.status == 200, response.body
    return variant_id
