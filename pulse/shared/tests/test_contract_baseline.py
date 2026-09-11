"""共享契约层基线校验（所有者：root）。

把 `AGENTS.md §5.4` 提到的"16 项校验"固化成常驻测试，防止契约漂移。
任何子 agent 的改动若让本文件失败，先怀疑自己的实现——`pulse/shared/` 是冻结层。

覆盖范围：枚举白名单、平台必填项、时区约束、素材授权、合规硬拦截、
幂等约束、ID 前缀、错误分级分区、序列化。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pulse.shared import (
    CONTRACT_VERSION,
    Caption,
    ComplianceInfo,
    ContentType,
    ContractError,
    LicenseStatus,
    MediaItem,
    MediaKind,
    Platform,
    PublishResult,
    UnifiedPost,
    new_brief_id,
    new_job_id,
    new_schedule_id,
    new_source_id,
    new_unified_post_id,
    new_variant_id,
    new_account_id,
)
from pulse.shared.enums import (
    NON_RETRYABLE_ERRORS,
    RETRYABLE_ERRORS,
    TERMINAL_JOB_STATUSES,
    ErrorClass,
    PublishJobStatus,
)

IST = timezone(timedelta(hours=5, minutes=30))


def _linkedin_post(**overrides) -> UnifiedPost:
    """构造一个默认合法的 LinkedIn 贴文，便于按需覆盖单个字段。"""
    base = {
        "unified_post_id": "up_01J8XYZ",
        "platform": Platform.LINKEDIN,
        "account_id": "acct_01",
        "source_id": "src_01",
        "variant_id": "var_01",
        "caption": Caption(text="Casting plus rough machining for machine tool builders."),
        "options": {
            "author_urn": "urn:li:organization:1234",
            "linkedin_visibility": "PUBLIC",
        },
    }
    base.update(overrides)
    return UnifiedPost(**base)  # type: ignore[arg-type]


def _video_item() -> MediaItem:
    return MediaItem(
        kind=MediaKind.VIDEO,
        url="s3://pulse-media/demo.mp4",
        mime="video/mp4",
        license_status=LicenseStatus.OWNED,
        duration_s=42.0,
    )


def _image_item() -> MediaItem:
    return MediaItem(
        kind=MediaKind.IMAGE,
        url="s3://pulse-media/demo.jpg",
        mime="image/jpeg",
        license_status=LicenseStatus.OWNED,
        width=1200,
        height=1500,
    )


def test_01_contract_version_is_frozen() -> None:
    assert CONTRACT_VERSION == "1.1"


def test_02_platform_whitelist_includes_vk_excludes_tiktok() -> None:
    """VK 于 2026-09-11 法务评审通过转正，进入枚举；TikTok 仍暂不投入。"""
    values = {p.value for p in Platform}
    assert values == {"linkedin", "youtube", "reddit", "facebook", "instagram", "vk"}
    assert "vk" in values
    assert "tiktok" not in values


def test_02b_vk_requires_community_owner_id() -> None:
    """VK 走社区墙发布：owner_id 必填、且必须是负数的社区 ID。"""
    post = _linkedin_post(platform=Platform.VK, options={})
    assert any("owner_id" in e for e in post.validate())

    as_person = _linkedin_post(platform=Platform.VK, options={"owner_id": 1234})
    assert any("社区 ID" in e for e in as_person.validate()), "正数会发到个人墙，必须拦"

    not_a_number = _linkedin_post(platform=Platform.VK, options={"owner_id": "abc"})
    assert any("必须是整数" in e for e in not_a_number.validate())

    ok = _linkedin_post(
        platform=Platform.VK, options={"owner_id": -1234567, "from_group": True}
    )
    assert ok.validate() == []


def test_03_valid_linkedin_post_passes() -> None:
    assert _linkedin_post().validate() == []


def test_04_naive_scheduled_at_is_rejected() -> None:
    """缺少时区偏移会导致多时区排期漂移，必须拦截。"""
    post = _linkedin_post(scheduled_at=datetime(2026, 9, 11, 3, 0))
    assert any("时区" in e for e in post.validate())


def test_05_aware_scheduled_at_is_accepted() -> None:
    post = _linkedin_post(scheduled_at=datetime(2026, 9, 11, 3, 0, tzinfo=IST))
    assert post.validate() == []


def test_06_linkedin_requires_author_urn_and_visibility() -> None:
    post = _linkedin_post(options={})
    errors = post.validate()
    assert any("author_urn" in e for e in errors)
    assert any("linkedin_visibility" in e for e in errors)


def test_07_author_urn_must_be_urn_li() -> None:
    post = _linkedin_post(
        options={"author_urn": "1234", "linkedin_visibility": "PUBLIC"}
    )
    assert any("urn:li:" in e for e in post.validate())


def test_08_youtube_requires_title_video_and_options() -> None:
    post = _linkedin_post(
        platform=Platform.YOUTUBE,
        title=None,
        content_type=ContentType.TEXT,
        options={},
    )
    errors = post.validate()
    assert any("title" in e for e in errors)
    assert any("视频素材" in e for e in errors)
    assert any("privacy_status" in e for e in errors)
    assert any("category_id" in e for e in errors)
    assert any("made_for_kids" in e for e in errors)


def test_09_pending_license_status_blocks_publish() -> None:
    """授权未完成的素材不得发布（版权审计要求）。"""
    item = MediaItem(
        kind=MediaKind.IMAGE,
        url="s3://pulse-media/x.jpg",
        mime="image/jpeg",
        license_status=LicenseStatus.PENDING,
        width=800,
        height=800,
    )
    assert any("pending" in e for e in item.validate())


def test_10_unknown_license_status_is_rejected() -> None:
    item = MediaItem(
        kind=MediaKind.IMAGE,
        url="s3://pulse-media/x.jpg",
        mime="image/jpeg",
        license_status="borrowed",
        width=800,
        height=800,
    )
    assert any("license_status" in e for e in item.validate())


def test_11_image_requires_dimensions() -> None:
    item = MediaItem(
        kind=MediaKind.IMAGE,
        url="s3://pulse-media/x.jpg",
        mime="image/jpeg",
        license_status=LicenseStatus.OWNED,
    )
    assert any("width" in e for e in item.validate())


def test_12_hashtag_format_is_enforced() -> None:
    post_missing_hash = _linkedin_post(hashtags=("casting",))
    assert any("必须以 # 开头" in e for e in post_missing_hash.validate())

    post_with_space = _linkedin_post(hashtags=("#rough machining",))
    assert any("不能含空格" in e for e in post_with_space.validate())


def test_13_content_type_must_match_media() -> None:
    post = _linkedin_post(content_type=ContentType.IMAGE, media=(_image_item(),))
    assert post.validate() == []

    mismatched = _linkedin_post(content_type=ContentType.IMAGE, media=())
    assert any("未提供图片素材" in e for e in mismatched.validate())


def test_14_compliance_block_prevents_publish() -> None:
    post = _linkedin_post(compliance=ComplianceInfo(blocked=True, findings_ref=(7,)))
    errors = post.validate()
    assert any("compliance.blocked=True" in e for e in errors)

    # 拦截但没记录原因 → 无法追溯，同样非法
    untraceable = _linkedin_post(compliance=ComplianceInfo(blocked=True))
    assert any("findings_ref" in e for e in untraceable.validate())


def test_15_publishing_without_platform_post_id_is_contract_error() -> None:
    """受理 ≠ 发布成功：要进入轮询收敛就必须带 platform_post_id。"""
    with pytest.raises(ContractError):
        PublishResult(ok=True, status="publishing")

    # 显式携带则合法，且 issued 状态仍是中间态而非终态
    result = PublishResult(ok=True, status="publishing", platform_post_id="urn:li:share:1")
    assert result.status != PublishJobStatus.PUBLISHED.value


def test_16_error_classes_partition_without_overlap() -> None:
    assert RETRYABLE_ERRORS.isdisjoint(NON_RETRYABLE_ERRORS)
    assert ErrorClass.POLICY_REJECTED in NON_RETRYABLE_ERRORS
    assert ErrorClass.RATE_LIMITED in RETRYABLE_ERRORS
    # media_processing 是第三类：既不是"重试"，也不是"终态"，
    # 而是转 pending_finalize 后轮询收敛（契约 §3.4）。不得被塞进任一重试集合。
    assert ErrorClass.MEDIA_PROCESSING not in RETRYABLE_ERRORS
    assert ErrorClass.MEDIA_PROCESSING not in NON_RETRYABLE_ERRORS
    classified = RETRYABLE_ERRORS | NON_RETRYABLE_ERRORS | {ErrorClass.MEDIA_PROCESSING}
    assert classified == set(ErrorClass)


def test_17_terminal_statuses_are_consistent() -> None:
    assert PublishJobStatus.PENDING_FINALIZE not in TERMINAL_JOB_STATUSES
    assert PublishJobStatus.PUBLISHED in TERMINAL_JOB_STATUSES
    assert PublishJobStatus.REJECTED in TERMINAL_JOB_STATUSES


def test_18_id_prefixes_follow_the_contract() -> None:
    assert new_account_id().startswith("acct_")
    assert new_brief_id().startswith("b_")
    assert new_source_id().startswith("src_")
    assert new_variant_id().startswith("var_")
    assert new_schedule_id().startswith("sched_")
    assert new_job_id().startswith("job_")
    assert new_unified_post_id().startswith("up_")


def test_19_ids_are_unique_and_time_ordered() -> None:
    ids = [new_variant_id() for _ in range(50)]
    assert len(set(ids)) == 50
    # 时间有序 = ULID 的 10 字符时间前缀单调不减。
    # 同一毫秒内生成的多条 ID 只有随机后缀不同，整串排序不保证有序，故只断言前缀。
    prefixes = [i[len("var_") : len("var_") + 10] for i in ids]
    assert prefixes == sorted(prefixes)


def test_20_serialization_produces_plain_json_types() -> None:
    post = _linkedin_post(content_type=ContentType.IMAGE, media=(_image_item(),))
    payload = post.to_dict()
    assert payload["platform"] == "linkedin"
    assert payload["content_type"] == "image"
    assert payload["media"][0]["license_status"] == "owned"
    assert payload["media"][0]["kind"] == "image"
    assert isinstance(payload["caption"], dict)
    assert isinstance(payload["hashtags"], list)
