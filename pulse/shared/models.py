"""契约数据模型（冻结，v1.1）。

与 contracts/INTERFACES.md §2 一一对应。
**纯标准库实现**（不依赖 pydantic），以保证所有域都能无摩擦导入。
业务域可以在内部用 pydantic，但**跨域传递必须使用本文件的类型**。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping

from pulse.shared.enums import (
    ComplianceSeverity,
    ContentType,
    LicenseStatus,
    MediaKind,
    Platform,
)

#: 契约版本。变更契约时由 root 同步升版（见 INTERFACES.md §10）。
CONTRACT_VERSION = "1.1"


class ContractError(ValueError):
    """契约校验失败。包含全部错误消息，便于一次性修复。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# --------------------------------------------------------------------------
# 子结构
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Caption:
    """文案。英文为对外主文案，中文仅供审校、不外发。"""

    text: str
    lang: str = "en"
    text_zh: str | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.text or not self.text.strip():
            errors.append("caption.text 不能为空")
        if not self.lang:
            errors.append("caption.lang 不能为空")
        return errors


@dataclass(frozen=True, slots=True)
class MediaItem:
    """媒体素材。

    `license_status` 只允许 `owned` / `licensed`：工业件内容**禁止**使用来源不明的素材。
    """

    kind: MediaKind | str
    url: str
    mime: str
    license_status: LicenseStatus | str
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        kind = MediaKind(self.kind) if not isinstance(self.kind, MediaKind) else self.kind

        if not self.url:
            errors.append("media.url 不能为空")
        if not self.mime:
            errors.append("media.mime 不能为空")

        try:
            lic = (
                self.license_status
                if isinstance(self.license_status, LicenseStatus)
                else LicenseStatus(self.license_status)
            )
        except ValueError:
            errors.append(
                f"media.license_status 非法：{self.license_status!r}"
                f"（仅允许 owned / licensed）"
            )
        else:
            if lic is LicenseStatus.PENDING:
                errors.append("media.license_status 为 pending，授权未完成，不得发布")

        if kind is MediaKind.IMAGE:
            if not self.width or not self.height:
                errors.append("图片素材必须提供 width 与 height（用于平台比例校验）")
            elif self.width <= 0 or self.height <= 0:
                errors.append("media.width / height 必须为正数")
        elif kind is MediaKind.VIDEO:
            if self.duration_s is None:
                errors.append("视频素材必须提供 duration_s")
            elif self.duration_s <= 0:
                errors.append("media.duration_s 必须为正数")

        return errors

    @property
    def aspect_ratio(self) -> float | None:
        """宽高比；非图片或尺寸缺失时返回 None。"""
        if self.width and self.height:
            return self.width / self.height
        return None


@dataclass(frozen=True, slots=True)
class ComplianceInfo:
    """合规校验结果快照，随 UnifiedPost 一起下传。

    `blocked=True` 时发布网关**必须拒绝发布**（契约 §2）。
    """

    ai_generated_disclosure: bool = False
    checked_at: datetime | None = None
    blocked: bool = False
    findings_ref: tuple[int, ...] = ()

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.blocked and not self.findings_ref:
            errors.append("compliance.blocked=True 但未提供 findings_ref（无法追溯拦截原因）")
        return errors


# --------------------------------------------------------------------------
# 平台专属选项校验表
# --------------------------------------------------------------------------

#: 必填的 options 字段。值为 True 表示必填。
_REQUIRED_OPTIONS: Mapping[Platform, Mapping[str, bool]] = {
    Platform.LINKEDIN: {"author_urn": True, "linkedin_visibility": True},
    Platform.YOUTUBE: {
        "privacy_status": True,
        "category_id": True,
        "made_for_kids": True,
    },
    Platform.FACEBOOK: {"page_id": True},
    Platform.REDDIT: {"subreddit": True},
    # VK 社区墙发布：owner_id 为社区 ID（社区为负数），from_group 表示以社区名义发布
    Platform.VK: {"owner_id": True},
}

_LINKEDIN_VISIBILITY = {"PUBLIC", "CONNECTIONS", "LOGGED_IN"}
_YOUTUBE_PRIVACY = {"public", "unlisted", "private"}


# --------------------------------------------------------------------------
# 主对象
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnifiedPost:
    """发布网关对外统一对象（契约 §2）。

    这是 **A2 与 A3 之间唯一的接口**，双方都不得绕过它直接传平台参数。
    """

    unified_post_id: str
    platform: Platform | str
    account_id: str
    source_id: str
    variant_id: str
    caption: Caption
    media: tuple[MediaItem, ...] = ()
    content_type: ContentType | str = ContentType.TEXT
    title: str | None = None
    hashtags: tuple[str, ...] = ()
    scheduled_at: datetime | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    compliance: ComplianceInfo = field(default_factory=ComplianceInfo)

    # -- 校验 ---------------------------------------------------------------

    def validate(self) -> list[str]:
        """返回全部错误消息（空列表 = 通过）。**不发网络请求。**"""
        errors: list[str] = []

        if not self.unified_post_id.startswith("up_"):
            errors.append(f"unified_post_id 必须以 up_ 开头：{self.unified_post_id!r}")
        if not self.account_id:
            errors.append("account_id 不能为空")
        if not self.variant_id:
            errors.append("variant_id 不能为空")
        if not self.source_id:
            errors.append("source_id 不能为空（溯源需要）")

        try:
            platform = self.platform if isinstance(self.platform, Platform) else Platform(self.platform)
        except ValueError:
            errors.append(
                f"platform 非法：{self.platform!r}（允许：{', '.join(p.value for p in Platform)}）"
            )
            return errors  # 平台非法时后续校验无意义

        errors.extend(self.caption.validate())

        try:
            content_type = (
                self.content_type
                if isinstance(self.content_type, ContentType)
                else ContentType(self.content_type)
            )
        except ValueError:
            errors.append(f"content_type 非法：{self.content_type!r}")
            content_type = None

        for idx, item in enumerate(self.media):
            errors.extend(f"media[{idx}]: {msg}" for msg in item.validate())

        # content_type 与实际素材一致性
        if content_type is not None:
            kinds = {MediaKind(m.kind) if not isinstance(m.kind, MediaKind) else m.kind for m in self.media}
            if content_type is ContentType.TEXT and kinds:
                errors.append("content_type=text 但提供了媒体素材")
            if content_type is ContentType.IMAGE and MediaKind.IMAGE not in kinds:
                errors.append("content_type=image 但未提供图片素材")
            if content_type is ContentType.VIDEO and MediaKind.VIDEO not in kinds:
                errors.append("content_type=video 但未提供视频素材")

        # 时区：scheduled_at 必须带 offset，否则排期会漂移
        if self.scheduled_at is not None and self.scheduled_at.tzinfo is None:
            errors.append("scheduled_at 必须携带时区偏移（如 +05:30），否则多时区排期会漂移")

        # hashtag 规范：含 #、不含空格
        for tag in self.hashtags:
            if not tag.startswith("#"):
                errors.append(f"hashtag 必须以 # 开头：{tag!r}")
            if " " in tag:
                errors.append(f"hashtag 不能含空格：{tag!r}")

        # 平台专属必填项
        required = _REQUIRED_OPTIONS.get(platform, {})
        for key, is_required in required.items():
            if is_required and key not in self.options:
                errors.append(f"platform={platform.value} 缺少必填 options.{key}")

        errors.extend(self._validate_platform_options(platform))
        errors.extend(self.compliance.validate())

        # 合规硬拦截：阻断发布
        if self.compliance.blocked:
            errors.append("compliance.blocked=True，网关必须拒绝发布")

        # 平台特有限制
        if platform is Platform.YOUTUBE and not self.title:
            errors.append("platform=youtube 必须提供 title")
        if platform is Platform.REDDIT and not self.title:
            errors.append("platform=reddit 必须提供 title")
        if platform is Platform.YOUTUBE:
            if not any(
                (m.kind if isinstance(m.kind, MediaKind) else MediaKind(m.kind)) is MediaKind.VIDEO
                for m in self.media
            ):
                errors.append("platform=youtube 必须提供视频素材")

        return errors

    def _validate_platform_options(self, platform: Platform) -> list[str]:
        errors: list[str] = []
        opts = self.options or {}

        if platform is Platform.LINKEDIN:
            vis = opts.get("linkedin_visibility")
            if vis is not None and vis not in _LINKEDIN_VISIBILITY:
                errors.append(
                    f"options.linkedin_visibility 非法：{vis!r}"
                    f"（允许：{', '.join(sorted(_LINKEDIN_VISIBILITY))}）"
                )
            urn = opts.get("author_urn")
            if urn is not None and not str(urn).startswith("urn:li:"):
                errors.append(f"options.author_urn 必须以 urn:li: 开头：{urn!r}")

        if platform is Platform.YOUTUBE:
            privacy = opts.get("privacy_status")
            if privacy is not None and privacy not in _YOUTUBE_PRIVACY:
                errors.append(
                    f"options.privacy_status 非法：{privacy!r}"
                    f"（允许：{', '.join(sorted(_YOUTUBE_PRIVACY))}）"
                )
            if opts.get("made_for_kids") is not None and not isinstance(opts["made_for_kids"], bool):
                errors.append("options.made_for_kids 必须是布尔值")

        if platform is Platform.VK:
            owner = opts.get("owner_id")
            if owner is not None:
                try:
                    owner_id = int(owner)
                except (TypeError, ValueError):
                    errors.append(f"options.owner_id 必须是整数（社区 ID）：{owner!r}")
                else:
                    if owner_id == 0:
                        errors.append("options.owner_id 不能为 0")
                    elif owner_id > 0:
                        # VK 的 wall.post 用负数表示社区，正数会发到个人墙
                        errors.append(
                            f"options.owner_id 应为负数的社区 ID（社区墙发布），当前为 {owner_id}"
                        )
            if opts.get("from_group") is not None and not isinstance(opts["from_group"], bool):
                errors.append("options.from_group 必须是布尔值")

        return errors

    def assert_valid(self) -> None:
        """校验失败则抛 ContractError（含全部错误）。"""
        errors = self.validate()
        if errors:
            raise ContractError(errors)

    # -- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["platform"] = Platform(self.platform).value
        d["content_type"] = ContentType(self.content_type).value
        d["media"] = [
            {
                **asdict(m),
                "kind": (m.kind if isinstance(m.kind, MediaKind) else MediaKind(m.kind)).value,
                "license_status": (
                    m.license_status
                    if isinstance(m.license_status, LicenseStatus)
                    else LicenseStatus(m.license_status)
                ).value,
            }
            for m in self.media
        ]
        d["hashtags"] = list(self.hashtags)
        if self.scheduled_at is not None:
            d["scheduled_at"] = self.scheduled_at.isoformat()
        if self.compliance.checked_at is not None:
            d["compliance"] = {
                **asdict(self.compliance),
                "checked_at": self.compliance.checked_at.isoformat(),
                "findings_ref": list(self.compliance.findings_ref),
            }
        else:
            d["compliance"] = {
                **asdict(self.compliance),
                "findings_ref": list(self.compliance.findings_ref),
            }
        d["options"] = dict(self.options or {})
        return d


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Adapter 的统一返回值（契约 §4）。"""

    ok: bool
    status: str
    platform_post_id: str | None = None
    post_url: str | None = None
    error_class: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # 关键约束：受理 ≠ 发布成功，必须经 pending_finalize 收敛
        if self.ok and self.status == "publishing" and not self.platform_post_id:
            raise ContractError(
                ["ok=True 且 status=publishing 时必须提供 platform_post_id 才能轮询收敛"]
            )


@dataclass(frozen=True, slots=True)
class SemiAutoBundle:
    """半自动发布包（P0 能力，契约 §4）。"""

    text: str
    media_paths: tuple[str, ...] = ()
    deep_link: str | None = None
    checklist: tuple[str, ...] = ()
    platform: str | None = None


#: 合规发现的级别取值（供 A4 使用）
SEVERITIES: tuple[str, ...] = tuple(s.value for s in ComplianceSeverity)
