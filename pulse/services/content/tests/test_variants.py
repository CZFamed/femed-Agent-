"""W1-A1-7：Variant 派生（每条都必须通过契约校验）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pulse.services.content.errors import ContentError
from pulse.services.content.variants import (
    AccountBinding,
    VariantDraft,
    build_unified_post,
    derive_variants,
    media_items_from_picks,
)
from pulse.shared.enums import ContentType, Platform
from pulse.shared.models import MediaItem


def _linkedin_account() -> AccountBinding:
    return AccountBinding(
        account_id="acct_li_1",
        platform=Platform.LINKEDIN,
        options={"author_urn": "urn:li:organization:12345", "linkedin_visibility": "PUBLIC"},
    )


def _youtube_account() -> AccountBinding:
    return AccountBinding(account_id="acct_yt_1", platform=Platform.YOUTUBE)


def _image() -> MediaItem:
    return MediaItem(
        kind="image",
        url="s3://pulse-media/IMG_0001.jpg",
        mime="image/jpeg",
        license_status="owned",
        width=1200,
        height=1500,
    )


def _video() -> MediaItem:
    return MediaItem(
        kind="video",
        url="s3://pulse-media/clip.mp4",
        mime="video/mp4",
        license_status="owned",
        duration_s=42.0,
    )


def test_build_unified_post_passes_contract_validation():
    draft = VariantDraft(
        platform=Platform.LINKEDIN,
        text="Most machine tool builders don't own a foundry.",
        text_zh="多数机床厂没有自己的铸造厂。",
        hashtags=("#casting", "#machining", "#machinetools"),
        media=(_image(),),
    )

    post = build_unified_post(draft=draft, source_id="src_1", account=_linkedin_account())

    assert post.validate() == []
    assert post.platform is Platform.LINKEDIN
    assert post.source_id == "src_1"
    assert post.unified_post_id.startswith("up_")
    assert post.variant_id.startswith("var_")
    assert post.options["author_urn"] == "urn:li:organization:12345"


def test_missing_account_options_are_reported():
    draft = VariantDraft(
        platform=Platform.LINKEDIN,
        text="text",
        hashtags=("#casting",),
        media=(_image(),),
    )
    account = AccountBinding(account_id="acct_li_2", platform=Platform.LINKEDIN)

    with pytest.raises(ContentError) as exc:
        build_unified_post(draft=draft, source_id="src_1", account=account)

    message = str(exc.value)
    # author_urn 是账号事实，没有默认值，必须报缺；visibility 有安全默认值（PUBLIC）
    assert "author_urn" in message
    assert "linkedin_visibility" not in message


def test_account_platform_must_match_draft():
    draft = VariantDraft(platform=Platform.LINKEDIN, text="text", media=(_image(),))

    with pytest.raises(ContentError) as exc:
        build_unified_post(draft=draft, source_id="src_1", account=_youtube_account())

    assert "不一致" in str(exc.value)


def test_youtube_requires_title_and_video():
    draft = VariantDraft(platform=Platform.YOUTUBE, text="text", hashtags=("#casting",), media=(_video(),))

    with pytest.raises(ContentError) as exc:
        build_unified_post(draft=draft, source_id="src_1", account=_youtube_account())

    assert "title" in str(exc.value)


def test_youtube_with_title_and_video_is_valid():
    draft = VariantDraft(
        platform=Platform.YOUTUBE,
        text="description",
        title="Casting + pre-machining",
        hashtags=("#casting", "#machining"),
        media=(_video(),),
    )

    post = build_unified_post(draft=draft, source_id="src_1", account=_youtube_account())

    assert post.validate() == []
    assert post.options["category_id"] == "28"
    assert post.options["made_for_kids"] is False


def test_vk_caption_language_is_russian():
    draft = VariantDraft(
        platform=Platform.VK,
        text="Мы производим чугунное литьё.",
        hashtags=("#casting", "#machining"),
        media=(_image(),),
    )
    account = AccountBinding(
        account_id="acct_vk_1", platform=Platform.VK, options={"owner_id": -123456}
    )

    post = build_unified_post(draft=draft, source_id="src_1", account=account)

    assert post.caption.lang == "ru"
    assert post.validate() == []


def test_naive_scheduled_at_is_rejected_by_contract():
    draft = VariantDraft(platform=Platform.LINKEDIN, text="t", media=(_image(),))

    with pytest.raises(ContentError) as exc:
        build_unified_post(
            draft=draft,
            source_id="src_1",
            account=_linkedin_account(),
            scheduled_at=datetime(2026, 9, 23, 10, 0),
        )

    assert "时区偏移" in str(exc.value)


def test_aware_scheduled_at_is_accepted():
    draft = VariantDraft(platform=Platform.LINKEDIN, text="t", media=(_image(),))
    ist = timezone(timedelta(hours=5, minutes=30))

    post = build_unified_post(
        draft=draft,
        source_id="src_1",
        account=_linkedin_account(),
        scheduled_at=datetime(2026, 9, 23, 10, 0, tzinfo=ist),
    )

    assert post.validate() == []
    assert post.scheduled_at is not None and post.scheduled_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_derive_variants_shares_source_id_and_keeps_ids_distinct():
    drafts = (
        VariantDraft(platform=Platform.LINKEDIN, text="linkedin copy", media=(_image(),)),
        VariantDraft(
            platform=Platform.YOUTUBE,
            text="youtube copy",
            title="Video title",
            media=(_video(),),
        ),
    )
    accounts = {"linkedin": _linkedin_account(), "youtube": _youtube_account()}

    derived = derive_variants(source_id="src_shared", drafts=drafts, accounts=accounts)

    assert len(derived) == 2
    assert {item.source_id for item in derived} == {"src_shared"}
    assert len({item.variant_id for item in derived}) == 2
    assert all(item.post.validate() == [] for item in derived)
    assert derived[0].fields()["caption"]["text"] == "linkedin copy"


def test_derive_variants_requires_account_for_every_platform():
    drafts = (VariantDraft(platform=Platform.LINKEDIN, text="t", media=(_image(),)),)

    with pytest.raises(ContentError) as exc:
        derive_variants(source_id="src_1", drafts=drafts, accounts={})

    assert "账号绑定" in str(exc.value)


def test_derive_variants_is_all_or_nothing():
    """一条不合法就不要产出半套草稿（避免半套流进审批台）。"""
    drafts = (
        VariantDraft(platform=Platform.LINKEDIN, text="ok", media=(_image(),)),
        VariantDraft(platform=Platform.YOUTUBE, text="no title", media=(_video(),)),
    )
    accounts = {"linkedin": _linkedin_account(), "youtube": _youtube_account()}

    with pytest.raises(ContentError):
        derive_variants(source_id="src_1", drafts=drafts, accounts=accounts)


def test_media_items_require_real_size_instead_of_defaults():
    class _Pick:
        file_name = "IMG_9999.jpg"

    with pytest.raises(ContentError) as exc:
        media_items_from_picks([_Pick()], size_of=lambda _name: None)

    assert "取不到真实尺寸" in str(exc.value)


def test_media_items_require_real_duration_for_video():
    class _Pick:
        file_name = "clip.mp4"

    with pytest.raises(ContentError) as exc:
        media_items_from_picks([_Pick()], size_of=lambda _name: (1200, 900), duration_of=lambda _n: None)

    assert "取不到真实时长" in str(exc.value)


def test_media_items_are_registered_as_owned():
    class _Pick:
        file_name = "IMG_0001.jpg"

    items = media_items_from_picks([_Pick()], size_of=lambda _name: (1200, 1500))

    assert items[0].license_status == "owned"
    assert items[0].width == 1200
    assert items[0].validate() == []
