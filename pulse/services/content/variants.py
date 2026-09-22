"""Variant 派生（所有者：A1，任务包 W1-A1-7）。

一条核心内容（``contents`` 行）派生到多个平台的 ``variants``，并且**每条派生结果
都必须能通过契约 §2 的 ``UnifiedPost.validate()``** —— 这是本任务包的验收标准，
所以这里不自己写一套校验，而是先组装再用契约的校验器把关，把错误一次报全。

两条工程约定：

1. 平台必填 ``options`` 里凡是**账号事实**（``author_urn`` / ``owner_id`` /
   ``page_id`` / ``subreddit``）都必须由调用方传入，**不给默认值**——
   编一个 URN 出来只会把内容发到错误的账号上。可给的默认值只有非标识类字段
   （如 YouTube 的 ``category_id=28``、``privacy_status=public``）。
2. ``source_id`` 全平台共享，``variant_id`` 每条独立：这是 FR-3"同一 source_id
   可追溯全部 variant"的落点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Sequence

from pulse.services.content.errors import ContentError
from pulse.services.content.prompts import prompt_template
from pulse.shared.enums import ContentType, Platform
from pulse.shared.ids import new_unified_post_id, new_variant_id
from pulse.shared.models import Caption, ContractError, MediaItem, UnifiedPost

#: 非标识类的平台默认 options。**账号事实（URN / 社区 ID / 主页 ID）不在此列**，
#: 必须由调用方给出，否则本条变体直接报缺项。
DEFAULT_OPTIONS: Mapping[Platform, Mapping[str, Any]] = {
    Platform.LINKEDIN: {"linkedin_visibility": "PUBLIC"},
    Platform.YOUTUBE: {"privacy_status": "public", "category_id": "28", "made_for_kids": False},
    Platform.VK: {"from_group": True},
}


@dataclass(frozen=True, slots=True)
class AccountBinding:
    """某平台要发到哪个账号，以及该账号特有的 options。"""

    account_id: str
    platform: Platform
    options: Mapping[str, Any] = field(default_factory=dict)

    def resolved_options(self) -> dict[str, Any]:
        merged: dict[str, Any] = dict(DEFAULT_OPTIONS.get(self.platform, {}))
        merged.update(dict(self.options or {}))
        return merged


@dataclass(frozen=True, slots=True)
class VariantDraft:
    """生成阶段产出的单平台草稿（不含发布参数）。"""

    platform: Platform
    text: str
    text_zh: str | None = None
    title: str | None = None
    hashtags: tuple[str, ...] = ()
    media: tuple[MediaItem, ...] = ()
    content_type: ContentType | None = None
    ai_score: float | None = None
    extra_options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DerivedVariant:
    """一条派生结果：域内记录 + 可直接交给 A2/A3 的统一对象。"""

    variant_id: str
    source_id: str
    platform: Platform
    post: UnifiedPost

    def fields(self) -> dict[str, Any]:
        """契约 §6 ``variants.fields`` 的内容（caption/title/hashtags/…）。"""
        return {
            "caption": {
                "text": self.post.caption.text,
                "text_zh": self.post.caption.text_zh,
                "lang": self.post.caption.lang,
            },
            "title": self.post.title,
            "hashtags": list(self.post.hashtags),
            "content_type": (
                self.post.content_type.value
                if hasattr(self.post.content_type, "value")
                else str(self.post.content_type)
            ),
            "media_count": len(self.post.media),
        }

    def as_payload(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "source_id": self.source_id,
            "platform": self.platform.value,
            "post": self.post.to_dict(),
        }


def _content_type_of(draft: VariantDraft) -> ContentType:
    if draft.content_type is not None:
        return (
            draft.content_type
            if isinstance(draft.content_type, ContentType)
            else ContentType(draft.content_type)
        )
    template = prompt_template(draft.platform)
    return ContentType(template.content_type)


def build_unified_post(
    *,
    draft: VariantDraft,
    source_id: str,
    account: AccountBinding,
    variant_id: str | None = None,
    scheduled_at: datetime | None = None,
    unified_post_id: str | None = None,
) -> UnifiedPost:
    """把单平台草稿组装成通过契约校验的 ``UnifiedPost``。

    Raises:
        ContentError: 平台不匹配、缺必填 options，或契约校验不通过（错误一次报全）。
    """
    template = prompt_template(draft.platform)
    platform = Platform(template.platform)
    if account.platform is not platform:
        raise ContentError(
            f"账号绑定平台（{account.platform.value}）与草稿平台（{platform.value}）不一致",
            details={"account_platform": account.platform.value, "draft_platform": platform.value},
        )

    options = account.resolved_options()
    options.update(dict(draft.extra_options or {}))

    post = UnifiedPost(
        unified_post_id=unified_post_id or new_unified_post_id(),
        platform=platform,
        account_id=account.account_id,
        source_id=source_id,
        variant_id=variant_id or new_variant_id(),
        caption=Caption(text=draft.text, lang="ru" if platform is Platform.VK else "en", text_zh=draft.text_zh),
        media=tuple(draft.media),
        content_type=_content_type_of(draft),
        title=draft.title,
        hashtags=tuple(draft.hashtags),
        scheduled_at=scheduled_at,
        options=options,
    )
    try:
        post.assert_valid()
    except ContractError as exc:
        # 统一成本域异常：上层（A5）只需要捕 ContentError 一种
        raise ContentError(
            f"平台 {platform.value} 的变体未通过契约校验：" + "；".join(exc.errors),
            details={"platform": platform.value, "errors": list(exc.errors)},
        ) from exc
    return post


def derive_variants(
    *,
    source_id: str,
    drafts: Sequence[VariantDraft],
    accounts: Mapping[str, AccountBinding],
    scheduled_at: datetime | None = None,
) -> tuple[DerivedVariant, ...]:
    """按平台批量派生，任一平台不合法就整体失败（避免半套草稿流到审批台）。

    Args:
        source_id: 共享的核心内容 ID（FR-3 的追溯键）。
        drafts: 各平台草稿。
        accounts: 平台 key → 账号绑定。
        scheduled_at: 目标发布时间（带时区偏移；不带偏移会被契约拦下）。

    Raises:
        ContentError: 缺账号绑定，或任一变体未通过契约校验。
    """
    results: list[DerivedVariant] = []
    for draft in drafts:
        platform = Platform(prompt_template(draft.platform).platform)
        account = accounts.get(platform.value)
        if account is None:
            raise ContentError(
                f"平台 {platform.value} 没有对应的账号绑定，无法派生（accounts 里缺 {platform.value}）",
                details={"platform": platform.value, "provided": sorted(accounts)},
            )
        variant_id = new_variant_id()
        post = build_unified_post(
            draft=draft,
            source_id=source_id,
            account=account,
            variant_id=variant_id,
            scheduled_at=scheduled_at,
        )
        results.append(
            DerivedVariant(variant_id=variant_id, source_id=source_id, platform=platform, post=post)
        )
    return tuple(results)


def media_items_from_picks(
    picks: Iterable[Any],
    *,
    size_of: Callable[[str], tuple[int, int] | None],
    duration_of: Callable[[str], float | None] | None = None,
    base_url: str = "s3://pulse-media",
) -> tuple[MediaItem, ...]:
    """把素材选用结果转成契约的 ``MediaItem``。

    **尺寸与时长必须由调用方提供真实值**（``size_of`` / ``duration_of`` 通常包装
    ``media.describe.probe_image`` / ``probe_video``）。这里不写死 1200×900 或 30 秒——
    契约要求图片必须带宽高、视频必须带时长，靠默认值凑数等于伪造素材参数，
    上层还会拿它做比例校验，编出来的数字会一路错到平台侧。

    Raises:
        ContentError: 素材尺寸或时长取不到真实值（附上文件名，便于补数据）。
    """
    items: list[MediaItem] = []
    for pick in picks:
        file_name = str(getattr(pick, "file_name", "") or "")
        if not file_name:
            continue
        is_video = file_name.lower().endswith((".mp4", ".mov", ".avi", ".mkv", ".webm"))
        if is_video:
            duration = duration_of(file_name) if duration_of else None
            if duration is None:
                raise ContentError(
                    f"视频素材 {file_name} 取不到真实时长，无法生成 UnifiedPost"
                    "（契约 §2 要求视频必须带 duration_s）",
                    details={"file_name": file_name, "missing": "duration_s"},
                )
            items.append(
                MediaItem(
                    kind="video",
                    url=f"{base_url}/{file_name}",
                    mime="video/mp4",
                    license_status="owned",
                    duration_s=float(duration),
                )
            )
            continue

        size = size_of(file_name)
        if not size or size[0] <= 0 or size[1] <= 0:
            raise ContentError(
                f"图片素材 {file_name} 取不到真实尺寸，无法生成 UnifiedPost"
                "（契约 §2 要求图片必须带 width/height 用于比例校验）",
                details={"file_name": file_name, "missing": "width/height"},
            )
        items.append(
            MediaItem(
                kind="image",
                url=f"{base_url}/{file_name}",
                mime="image/jpeg",
                license_status="owned",
                width=int(size[0]),
                height=int(size[1]),
            )
        )
    return tuple(items)
