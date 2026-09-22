"""A6 独立验证域的共享夹具（唯一可写目录：``pulse/tests/``）。

派工单 ``docs/dispatch/A6_verifier.md`` §5 的三条硬约束落在这里：

1. **独立**：不复用各域 ``services/*/tests/conftest.py`` 里的夹具与断言，
   所有替身在本文件重写一遍——用被测方的夹具验证被测方，等于没有验证。
2. **离线**：不发真实网络请求、不连 Redis、不调真实模型（全部走 Fake Adapter
   与 ``StubCopywriter``）。
3. **不改被测代码**：夹具只做"部署接线"（identity ↔ scheduler ↔ publish），
   被验证的行为一律来自被测模块自身。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pulse.services.content import AccountBinding, ContentService
from pulse.services.identity import IdentityService, InMemoryVault, TokenSet
from pulse.services.publish import (
    FakeAdapter,
    PublishGateway,
    SemiAutoBundleExporter,
)
from pulse.services.publish.adapters import FakeBehaviour
from pulse.services.scheduler import (
    BestTimeTable,
    Dispatcher,
    EagerTaskQueue,
    InMemoryJobStore,
    InMemoryScheduleStore,
    QuotaLedger,
)
from pulse.services.scheduler.identity_adapter import (
    IdentityAccountRegistry,
    wire_account_status_listener,
)
from pulse.shared.enums import ContentType, LicenseStatus, MediaKind, Platform
from pulse.shared.models import (
    Caption,
    ComplianceInfo,
    MediaItem,
    UnifiedPost,
)

#: 印度标准时间（契约 §2 示例用的 +05:30）
IST = timezone(timedelta(hours=5, minutes=30))

#: 测试主密钥（32 字节，与 identity 域的假 Vault 同规格；两域各自持有自己的夹具）
A6_MASTER_KEY = b"pulse-test-master-key-32-bytes!!"

#: 账号 ID / 幂等键等固定 ID，便于断言"队列只传 ID"
ACCOUNT_ID = "acct_a6_linkedin"


class FrozenClock:
    """可推进的固定时钟（返回值一律带 UTC 偏移）。"""

    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> datetime:
        self.value = self.value + timedelta(**kwargs)
        return self.value


class GatewaySink:
    """把 A3 的 ``publish.dispatch`` / ``publish.finalize`` 接到 A2 发布网关。

    真实部署里这一层由 A2 自己实现（从库里按 ``unified_post_id`` 取 ``UnifiedPost``、
    按 ``account_id`` 取凭据）。这里做同样的事，只是数据源换成进程内字典，
    因此端到端链路**不依赖数据库也不发网络请求**。

    网关是异步的、调度器是同步的：桥接点固定在 ``asyncio.run``，
    与生产里"Celery worker 内部跑 asyncio 事件循环"等价。
    """

    def __init__(
        self,
        gateway: PublishGateway,
        *,
        posts: dict[str, UnifiedPost],
        credentials: dict[str, Any],
    ) -> None:
        self.gateway = gateway
        self._posts = posts
        self._credentials = credentials
        self.job_to_post: dict[str, str] = {}

    def dispatch(self, job_id: str, unified_post_id: str) -> Any:
        """``pulse.publish.dispatch {job_id, unified_post_id}`` 的消费端。"""
        post = self._posts[unified_post_id]
        self.job_to_post[job_id] = unified_post_id
        return asyncio.run(
            self.gateway.dispatch(post, self._credentials[post.account_id], job_id=job_id)
        )

    def poll_finalize(self, job_id: str) -> Any:
        """``pulse.publish.finalize {job_id}`` 的消费端（按 job → 幂等键反查）。"""
        unified_post_id = self.job_to_post[job_id]
        post = self._posts[unified_post_id]
        return asyncio.run(
            self.gateway.finalize(
                unified_post_id, self._credentials[post.account_id], job_id=job_id
            )
        )


class CredentialStub:
    """A2 只需要 ``account_id`` / ``provider`` / ``access_token()``（契约 §4）。

    identity 域的真实 ``Credential`` 用在端到端链路里；这里保留一个最小替身，
    供只关心网关行为的用例使用（不依赖 Vault 与令牌生命周期）。
    """

    account_id = "acct_a6_stub"
    provider = "linkedin"

    def access_token(self) -> str:
        return "stub-token"


def run_async(coro: Any) -> Any:
    """在同步测试里跑一段协程（测试内唯一的异步桥接点）。"""
    return asyncio.run(coro)


def make_media_item(
    *,
    url: str = "s3://pulse-media/a6/sample.jpg",
    kind: MediaKind | str = MediaKind.IMAGE,
    license_status: LicenseStatus | str = LicenseStatus.OWNED,
    width: int | None = 1200,
    height: int | None = 1500,
    duration_s: float | None = None,
) -> MediaItem:
    """构造一个素材项（默认是合规的实拍图）。"""
    return MediaItem(
        kind=kind,
        url=url,
        mime="image/jpeg" if str(kind) == "image" else "video/mp4",
        license_status=license_status,
        width=width,
        height=height,
        duration_s=duration_s,
    )


def make_post(
    platform: Platform | str = Platform.LINKEDIN,
    *,
    unified_post_id: str = "up_a6_0001",
    account_id: str = ACCOUNT_ID,
    variant_id: str = "var_a6_0001",
    source_id: str = "src_a6_0001",
    text: str = "Casting plus pre-machining from one plant for machine tool builders.",
    hashtags: tuple[str, ...] = ("#casting", "#machining"),
    options: dict[str, Any] | None = None,
    media: tuple[MediaItem, ...] = (),
    content_type: ContentType | str = ContentType.TEXT,
    title: str | None = None,
    scheduled_at: datetime | None = None,
    blocked: bool = False,
    findings_ref: tuple[int, ...] = (),
) -> UnifiedPost:
    """构造一个**合法**的 ``UnifiedPost``（契约 §2），供各用例按需覆盖字段。"""
    if options is None:
        options = {
            Platform.LINKEDIN.value: {
                "author_urn": "urn:li:organization:10086",
                "linkedin_visibility": "PUBLIC",
            },
            Platform.YOUTUBE.value: {
                "privacy_status": "public",
                "category_id": "28",
                "made_for_kids": False,
            },
            Platform.FACEBOOK.value: {"page_id": "famed-page"},
            Platform.REDDIT.value: {"subreddit": "manufacturing"},
            Platform.VK.value: {"owner_id": -123456},
            Platform.INSTAGRAM.value: {},
        }[str(platform)]

    compliance_ref = findings_ref or ((9001,) if blocked else ())
    return UnifiedPost(
        unified_post_id=unified_post_id,
        platform=platform,
        account_id=account_id,
        source_id=source_id,
        variant_id=variant_id,
        caption=Caption(text=text, lang="ru" if str(platform) == "vk" else "en"),
        media=media,
        content_type=content_type,
        title=title,
        hashtags=hashtags,
        scheduled_at=scheduled_at,
        options=options,
        compliance=ComplianceInfo(
            blocked=blocked,
            checked_at=datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc),
            findings_ref=compliance_ref,
        ),
    )


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
def clock() -> FrozenClock:
    """2026-09-22 04:00 UTC（= 印度时间 09:30）。"""
    return FrozenClock(datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc))


@pytest.fixture
def queue() -> EagerTaskQueue:
    """记录式假队列（不连 Redis）。"""
    return EagerTaskQueue()


@pytest.fixture
def schedules() -> InMemoryScheduleStore:
    """内存排期存储。"""
    return InMemoryScheduleStore()


@pytest.fixture
def jobs() -> InMemoryJobStore:
    """内存发布任务存储（复刻 publish_jobs 唯一索引语义）。"""
    return InMemoryJobStore()


@pytest.fixture
def identity(clock: FrozenClock) -> IdentityService:
    """identity 服务（内存 Vault、内存凭据存储、固定时钟）。"""
    return IdentityService(
        vault=InMemoryVault(master_key=A6_MASTER_KEY), now=clock
    )


@pytest.fixture
def account(identity: IdentityService, clock: FrozenClock) -> Any:
    """一个 active 的 LinkedIn 账号 + 一组有效令牌（30 天有效）。"""
    created = identity.create_account(
        "linkedin",
        "菲美得 LinkedIn 公司页（A6 验证）",
        "Asia/Kolkata",
        region="India",
        account_id=ACCOUNT_ID,
    )
    identity.store_tokens(
        ACCOUNT_ID,
        tokens=TokenSet(
            access_token="a6-access-token",
            refresh_token="a6-refresh-token",
            expires_at=clock() + timedelta(days=30),
            refresh_expires_at=clock() + timedelta(days=60),
        ),
    )
    return created


@pytest.fixture
def registry(identity: IdentityService, account: Any) -> IdentityAccountRegistry:
    """把 identity 服务适配成 scheduler 的 ``AccountRegistry``。"""
    return IdentityAccountRegistry(identity)


@pytest.fixture
def dispatcher(
    identity: IdentityService,
    registry: IdentityAccountRegistry,
    queue: EagerTaskQueue,
    schedules: InMemoryScheduleStore,
    jobs: InMemoryJobStore,
    clock: FrozenClock,
) -> Dispatcher:
    """已接线 account 状态监听者的调度器（账号暂停 → 即时熔断）。"""
    created = Dispatcher(
        accounts=registry,
        schedules=schedules,
        jobs=jobs,
        queue=queue,
        quota=QuotaLedger(now=clock),
        best_time=BestTimeTable(),
        now=clock,
    )
    wire_account_status_listener(identity, created)
    return created


@pytest.fixture
def adapter() -> FakeAdapter:
    """默认行为：先受理（pending_finalize）、轮询后 published。"""
    return FakeAdapter(Platform.LINKEDIN.value, FakeBehaviour.PENDING_THEN_PUBLISHED)


@pytest.fixture
def gateway(adapter: FakeAdapter, clock: FrozenClock) -> PublishGateway:
    """发布网关（只注册 LinkedIn 的 Fake Adapter）。"""
    return PublishGateway(adapters=[adapter], now=clock)


@pytest.fixture
def exporter() -> SemiAutoBundleExporter:
    """半自动导出器（P0 能力）。"""
    return SemiAutoBundleExporter()


@pytest.fixture
def credential(identity: IdentityService, account: Any) -> Any:
    """发布瞬间的凭据对象（identity 产出的真实实现，非替身）。"""
    return identity.bind(ACCOUNT_ID)


@pytest.fixture
def content() -> ContentService:
    """内容服务（无素材库 → 纯文本；固定文案桩，不调真实模型）。"""
    return ContentService()


@pytest.fixture
def binding() -> AccountBinding:
    """LinkedIn 账号绑定（URN 是账号事实，必须显式给出）。"""
    return AccountBinding(
        account_id=ACCOUNT_ID,
        platform=Platform.LINKEDIN,
        options={"author_urn": "urn:li:organization:10086"},
    )
