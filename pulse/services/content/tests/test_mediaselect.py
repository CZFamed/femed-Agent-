"""W1-A1-5：素材选用器（复用媒体域召回，缺口显式暴露）。"""

from __future__ import annotations

import pytest

from pulse.services.content.errors import MediaSelectionError
from pulse.services.content.mediaselect import NEED_REAL_DATA, has_slot_profile, select_media
from pulse.services.content.tests.conftest import make_asset


def test_linkedin_selection_fills_all_four_slots(asset_pool, policy):
    selection = select_media("linkedin", asset_pool, policy)

    assert selection.platform == "linkedin"
    assert selection.aspect == "4:5"
    assert len(selection.slots) == 4
    assert all(slot.picks for slot in selection.slots)
    assert selection.gaps == ()
    assert selection.needs_reshoot is False
    assert len(selection.picked_asset_ids) == 4


def test_selection_is_strict_when_keywords_match(asset_pool, policy):
    selection = select_media("linkedin", asset_pool, policy)

    assert all(slot.match_level == "strict" for slot in selection.slots)


def test_gap_is_marked_with_placeholder_when_library_is_thin(ledger, policy):
    """只有一张铸件图时，检测位次必然缺口——必须标 TODO(need-real-data)。"""
    thin_pool = [
        make_asset("IMG_1000.jpg", process="铸件", sub_process="壳体", summary="阀门壳体毛坯")
    ]

    selection = select_media("linkedin", thin_pool, policy)

    assert selection.needs_reshoot is True
    assert selection.gaps
    assert all(gap.startswith(NEED_REAL_DATA) for gap in selection.gaps)
    assert "补拍" in selection.gaps[0]


def test_relaxed_match_is_surfaced_as_gap(asset_pool, policy):
    """素材不相干时只能放宽到全库，这属于"画面不对"，要提示补拍。"""
    unrelated = [
        make_asset(
            "IMG_2000.jpg",
            process="人员",
            sub_process="",
            summary="员工在办公室查看图纸",
            keywords=("人员",),
        )
    ]

    selection = select_media("linkedin", unrelated, policy)

    relaxed = [slot for slot in selection.slots if slot.is_relaxed]
    assert relaxed, "不相干素材应当落到放宽档"
    assert relaxed[0].gap
    assert "放宽" in relaxed[0].gap


def test_cooling_assets_are_not_returned(asset_pool, policy):
    first = select_media("linkedin", asset_pool, policy)
    picked_id = first.slots[0].picks[0].asset_id
    policy.ledger.mark_used(picked_id, brand=policy.config.brand, content_id="c1")

    second = select_media("linkedin", asset_pool, policy)

    assert picked_id not in second.picked_asset_ids


def test_empty_library_raises(ledger, policy):
    with pytest.raises(MediaSelectionError) as exc:
        select_media("linkedin", [], policy)

    assert "素材库为空" in str(exc.value)


def test_platform_without_slot_profile_raises(asset_pool, policy):
    """位次口径目前只覆盖 linkedin / facebook / tiktok / vk。

    YouTube 是 P0-A 却还没有位次口径——这是跨域缺口，A1 不猜画面顺序，
    直接报错并把缺口交给上层（ContentService 会降级为"人工配图"并标注 TODO）。
    """
    with pytest.raises(MediaSelectionError) as exc:
        select_media("youtube", asset_pool, policy)

    assert "没有位次口径" in str(exc.value)


def test_has_slot_profile_reports_coverage(asset_pool):
    assert has_slot_profile("linkedin") is True
    assert has_slot_profile("vk") is True
    assert has_slot_profile("youtube") is False


def test_media_brief_lists_only_visible_facts(asset_pool, policy):
    selection = select_media("linkedin", asset_pool, policy)

    brief = selection.media_brief()

    assert "文件 IMG_0001.jpg" in brief
    assert "品类 加工件/壳体" in brief
    assert "位次1" in brief


def test_payload_is_json_friendly(asset_pool, policy):
    payload = select_media("linkedin", asset_pool, policy).as_payload()

    assert payload["platform"] == "linkedin"
    assert payload["needs_reshoot"] is False
    assert isinstance(payload["slots"], list)
    assert isinstance(payload["slots"][0]["picks"], list)
