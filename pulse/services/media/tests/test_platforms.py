"""分平台素材配置测试：位次匹配、形态偏好、最高分档。"""

from __future__ import annotations

from pulse.services.media.catalog import MediaAsset
from pulse.services.media.platforms import (
    PLATFORM_PROFILES,
    PlatformProfile,
    ShotSlot,
    platform_choices,
    platform_profile,
    prefer_media_kind,
    slot_score,
    top_tier,
)


def make_asset(
    file_name: str = "a.jpg",
    *,
    process: str = "加工件",
    sub_process: str = "机床件",
    keywords: tuple[str, ...] = (),
    summary: str = "",
) -> MediaAsset:
    return MediaAsset(
        asset_id="img_test",
        file_name=file_name,
        process=process,
        sub_process=sub_process,
        source_path="",
        description_path="",
        summary=summary,
        keywords=keywords,
        added_at=None,
        is_legacy=True,
    )


def test_platform_profile_lookup() -> None:
    assert platform_profile("linkedin").name == "LinkedIn"
    assert platform_profile("  VK  ").key == "vk", "大小写与空格都要容忍"
    assert platform_profile("weibo") is None
    assert platform_profile("") is None
    assert platform_profile(None) is None


def test_platform_choices_order_and_fields() -> None:
    choices = platform_choices()
    assert [item["key"] for item in choices] == ["linkedin", "facebook", "tiktok", "vk"]
    assert choices[0]["priority"] == "P0-A", "LinkedIn 排第一"
    for item in choices:
        assert {"name", "priority", "status", "aspect", "shot_count", "slot_count"} <= set(item)


def test_every_profile_has_ordered_unique_slots() -> None:
    for profile in PLATFORM_PROFILES:
        orders = [slot.order for slot in profile.slots]
        assert orders == list(range(1, len(orders) + 1)), f"{profile.key} 位次编号应连续"
        assert all(slot.role for slot in profile.slots), f"{profile.key} 位次要有说明"
        assert profile.slot_count >= 3


def test_slots_carry_report_provenance() -> None:
    linkedin = platform_profile("linkedin")
    assert all(slot.note for slot in linkedin.slots), "LinkedIn 位次要标注报告出处"
    facebook_video = platform_profile("facebook").slots[0]
    assert facebook_video.media_kind == "video", "Facebook 首选形态是原生视频"


def test_vk_profile_records_promotion_and_obligations() -> None:
    """VK 已于 2026-09-11 转正式运营；配置里要写明转正事实与随之而来的留痕义务。"""
    vk = platform_profile("vk")
    assert "转正式运营" in vk.contract_note
    assert "俄语" in vk.contract_note
    assert "制裁名单筛查" in vk.contract_note
    assert platform_profile("tiktok").contract_note, "TikTok 也要写明只备素材"


def test_slot_score_rejects_wrong_process() -> None:
    slot = ShotSlot(order=1, role="x", processes=("铸件",), keywords=("阀体",))
    assert slot_score(slot, make_asset(process="加工件", keywords=("阀体",))) == 0.0


def test_slot_score_requires_a_keyword_hit() -> None:
    slot = ShotSlot(order=1, role="x", processes=("加工件",), keywords=("镗铣", "箱体"))
    assert slot_score(slot, make_asset(summary="一件普通工件", keywords=("机床",))) == 0.0


def test_slot_score_prefers_keyword_field_over_summary() -> None:
    """同一个词出现在关键词字段里，比只出现在摘要段落里更能说明题材。"""
    slot = ShotSlot(order=1, role="x", processes=("铸件",), keywords=("三维扫描", "点云", "偏差"))
    tagged = make_asset(
        process="铸件", keywords=("三维扫描", "点云", "偏差色谱"), summary="扫描检测"
    )
    mentioned = make_asset(process="铸件", keywords=("阀体",), summary="文中提到三维扫描与点云")
    assert slot_score(slot, tagged) > slot_score(slot, mentioned) > 0


def test_prefer_media_kind_filters_but_falls_back() -> None:
    video_slot = ShotSlot(order=1, role="x", media_kind="video")
    video = make_asset("v.mp4")
    photo = make_asset("p.jpg")
    assert prefer_media_kind(video_slot, [(1.0, video), (1.0, photo)]) == [(1.0, video)]

    # 一个视频都没有时不能把整格清空，退回全部
    assert prefer_media_kind(video_slot, [(1.0, photo)]) == [(1.0, photo)]
    # 没指定形态就原样返回
    assert len(prefer_media_kind(ShotSlot(order=1, role="x"), [(1.0, photo)])) == 1


def test_top_tier_keeps_only_best_scores() -> None:
    a = make_asset("a.jpg")
    b = make_asset("b.jpg")
    c = make_asset("c.jpg")
    assert top_tier([(1.0, a), (0.98, b), (0.4, c)]) == [(1.0, a), (0.98, b)]
    assert top_tier([]) == []


def test_profile_payload_is_serialisable() -> None:
    """as_payload 要能直接喂给前端下拉框。"""
    payload = PlatformProfile(
        key="k",
        name="K",
        priority="P1",
        status="正式执行",
        aspect="4:5",
        shot_count="3–5 张",
        form="图集",
        caption_length="100 字符",
        slots=(ShotSlot(order=1, role="首位"),),
    ).as_payload()
    assert payload["slot_count"] == 1
    assert payload["key"] == "k"
