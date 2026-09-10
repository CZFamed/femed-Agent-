"""A2 发布网关域的测试夹具（不写进 pulse/tests/，那是 A6 的地盘）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pulse.shared.enums import ContentType, LicenseStatus, MediaKind, Platform
from pulse.shared.ids import new_unified_post_id
from pulse.shared.models import Caption, ComplianceInfo, MediaItem, UnifiedPost

#: 印度标准时间（契约 §2 示例用 +05:30）
IST = timezone(timedelta(hours=5, minutes=30))


class FakeCredential:
    """凭据句柄替身。

    真实实现由 **A3 identity 域**提供（密文存 Vault/KMS）。这里只暴露
    `account_id` / `provider` / `access_token()` 三个契约成员。
    """

    def __init__(
        self,
        account_id: str = "acct_test_01",
        provider: str = "linkedin",
        token: str = "fake-secret-token",
    ) -> None:
        self.account_id = account_id
        self.provider = provider
        self._token = token

    def access_token(self) -> str:
        return self._token


@pytest.fixture
def credential() -> FakeCredential:
    """默认凭据句柄。"""

    return FakeCredential()


def _platform_defaults(platform: Platform) -> dict[str, Any]:
    if platform is Platform.YOUTUBE:
        return {
            "title": "Ductile iron valve body: casting + machining from one supplier",
            "content_type": ContentType.VIDEO,
            "media": (
                MediaItem(
                    kind=MediaKind.VIDEO,
                    url="s3://pulse-media/videos/foundry-line.mp4",
                    mime="video/mp4",
                    license_status=LicenseStatus.OWNED,
                    duration_s=12.0,
                ),
            ),
            "options": {
                "privacy_status": "unlisted",
                "category_id": "28",
                "made_for_kids": False,
            },
        }
    if platform is Platform.REDDIT:
        return {
            "title": "Casting + machining capacity for machine tool builders",
            "content_type": ContentType.TEXT,
            "media": (),
            "options": {"subreddit": "foundry"},
        }
    return {
        "title": None,
        "content_type": ContentType.IMAGE,
        "media": (
            MediaItem(
                kind=MediaKind.IMAGE,
                url="s3://pulse-media/images/valve-body-01.jpg",
                mime="image/jpeg",
                license_status=LicenseStatus.OWNED,
                width=1200,
                height=1500,
            ),
        ),
        "options": {
            "linkedin_visibility": "PUBLIC",
            "author_urn": "urn:li:organization:1234567",
        },
    }


@pytest.fixture
def make_post():
    """构造通过契约校验的 `UnifiedPost`；`overrides` 里的字段直接覆盖。"""

    def factory(
        platform: Platform | str = Platform.LINKEDIN, **overrides: Any
    ) -> UnifiedPost:
        plat = platform if isinstance(platform, Platform) else Platform(platform)
        fields: dict[str, Any] = {
            "unified_post_id": new_unified_post_id(),
            "platform": plat,
            "account_id": "acct_test_01",
            "source_id": "src_test_001",
            "variant_id": "var_test_003",
            "caption": Caption(
                text="Ductile iron valve bodies, cast and rough-machined in one plant.",
                lang="en",
                text_zh="球墨铸铁阀体，铸造与粗加工一体化完成。",
            ),
            "hashtags": ("#casting", "#machining"),
            "scheduled_at": datetime(2026, 9, 11, 3, 0, tzinfo=IST),
            "compliance": ComplianceInfo(),
        }
        fields.update(_platform_defaults(plat))
        fields.update(overrides)
        return UnifiedPost(**fields)

    return factory
