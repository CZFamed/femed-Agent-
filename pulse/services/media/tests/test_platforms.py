"""分平台素材配置测试：位次匹配、形态偏好、最高分档。"""

from __future__ import annotations

from pulse.services.media.catalog import MediaAsset
from pulse.services.media.platforms import (
    MATCH_LEVELS,
    PLATFORM_PROFILES,
    PlatformProfile,
    ShotSlot,
    level_label,
    platform_choices,
    platform_profile,
    prefer_media_kind,
    slot_pools,
    slot_score,
    top_tier,
    weakest_level,
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


# ---------- 位次补足阶梯（2026-09-20：解决"一堆图却一张也召回不来"） ----------


def test_slot_pools_go_from_strict_to_whole_library() -> None:
    slot = ShotSlot(
        order=1, role="x", processes=("铸件",), keywords=("阀体", "灰铁")
    )
    assets = [
        make_asset("hit.jpg", process="铸件", keywords=("阀体", "灰铁")),
        make_asset("weak.jpg", process="铸件", keywords=("机加工",), summary="只有一件工件"),
        make_asset("other.jpg", process="加工件", keywords=("阀体",)),
    ]
    pools = dict(slot_pools(slot, assets))
    assert list(pools) == [key for key, _label in MATCH_LEVELS], "档位顺序即放宽顺序"
    assert [a.file_name for _s, a in pools["strict"]] == ["hit.jpg"]
    assert [a.file_name for _s, a in pools["same_category"]] == ["hit.jpg"]
    # 同品类但不限关键词 → 该品类的都进来
    assert {a.file_name for _s, a in pools["category_any_keyword"]} == {"hit.jpg", "weak.jpg"}
    # 全库 → 连别品类的也进来（否则窄位次永远凑不齐）
    assert {a.file_name for _s, a in pools["whole_library"]} == {
        "hit.jpg",
        "weak.jpg",
        "other.jpg",
    }


def test_slot_pools_respect_media_kind_when_possible() -> None:
    slot = ShotSlot(order=1, role="x", media_kind="video")
    assets = [make_asset("v.mp4"), make_asset("p.jpg")]
    pools = dict(slot_pools(slot, assets))
    assert [a.file_name for _s, a in pools["whole_library"]] == ["v.mp4"]


def test_weakest_level_returns_loosest_used() -> None:
    assert weakest_level(["strict"]) == "strict"
    assert weakest_level(["strict", "same_category"]) == "same_category"
    assert weakest_level(["whole_library", "strict"]) == "whole_library"
    assert weakest_level([]) == ""
    assert weakest_level(["不存在的档位"]) == ""


def test_level_label_is_chinese() -> None:
    assert level_label("strict") == "精准匹配"
    assert "放宽" in level_label("same_category")
    assert level_label("") == ""


def test_whole_library_pool_covers_assets_outside_slot_categories() -> None:
    """窄位次（比如"三维扫描"只有两张）也必须能靠全库档补齐数量。"""
    slot = ShotSlot(order=1, role="检测", processes=("铸件",), keywords=("三维扫描",))
    assets = [make_asset("scan.jpg", process="铸件", keywords=("三维扫描",))]
    assets += [
        make_asset(f"other{i}.jpg", process="厂区_场景", keywords=("厂房",)) for i in range(8)
    ]
    pools = dict(slot_pools(slot, assets))
    assert len(pools["strict"]) == 1, "精准匹配只有一张"
    assert len(pools["whole_library"]) == 9, "全库档能补齐剩下的"


# ---------- 2026-09-14 素材库品类调整后的位次口径 ----------


def test_scan_slot_follows_scan_to_production_flow() -> None:
    """三维扫描 2026-09-14 从「铸件/扫描」挪到「生产流程/扫描」，检测位次要跟得上。

    否则素材一搬家，LinkedIn 的质量位次就再也挑不到扫描证据——这是"目录调整
    把配置打散"的真实风险，所以在这里钉死。
    """
    scan_asset = make_asset(
        process="生产流程",
        sub_process="扫描",
        keywords=("三维扫描", "点云比对", "偏差色谱"),
    )
    for key in ("linkedin", "tiktok"):
        profile = platform_profile(key)
        slots = {slot.order: slot for slot in profile.slots}
        inspection = slots[3] if key == "linkedin" else slots[5]
        assert slot_score(inspection, scan_asset) > 0, f"{key} 的检测位次应能召回扫描素材"


def test_pouring_and_yellow_pattern_slots_match_new_sub_processes() -> None:
    """新拆出的两个工序目录（浇筑 / 黄模）要能被对应位次召回。"""
    pouring = make_asset(
        process="生产流程", sub_process="浇筑", keywords=("浇注", "砂箱", "浇包")
    )
    yellow = make_asset(
        process="生产流程", sub_process="黄模", keywords=("浸涂", "浆料", "涂料")
    )
    facebook = {slot.order: slot for slot in platform_profile("facebook").slots}
    assert slot_score(facebook[1], pouring) > 0, "浇铸位次要认「浇筑」工序"
    assert slot_score(facebook[2], yellow) > 0, "工艺位次要认「黄模」工序"
