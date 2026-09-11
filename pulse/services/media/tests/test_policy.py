"""召回策略测试：冷却、新图加权、加权随机、容量红色预警。"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from pulse.services.media.catalog import MediaAsset
from pulse.services.media.config import BRAND_NAME, RecallConfig
from pulse.services.media.ledger import RecallLedger
from pulse.services.media.policy import MediaCandidate, RecallPolicy

NOW = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)


def make_asset(asset_id: str, *, added_at: datetime | None = None, legacy: bool = True) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        file_name=f"{asset_id}.jpg",
        process="加工件",
        sub_process="机床件",
        source_path=f"../菲美得产品图片/加工件/机床件/{asset_id}.jpg",
        description_path=f"RAG知识库/图片描述/加工件/机床件/{asset_id}.md",
        summary="机床床身铸件",
        keywords=("机床床身",),
        added_at=added_at,
        is_legacy=legacy,
    )


def make_policy(tmp_path, *, threshold: int = 112, seed: int = 7) -> RecallPolicy:
    ledger = RecallLedger(tmp_path / "ledger.sqlite3")
    config = RecallConfig(capacity_red_threshold=threshold)
    return RecallPolicy(ledger, config, rng=random.Random(seed))


def test_new_asset_gets_freshness_boost(tmp_path) -> None:
    policy = make_policy(tmp_path)
    legacy = make_asset("img_old", legacy=True)
    fresh = make_asset("img_new", added_at=NOW - timedelta(days=1), legacy=False)
    assert policy.weight(MediaCandidate(legacy, 0.5), now=NOW) == 0.5
    assert policy.weight(MediaCandidate(fresh, 0.5), now=NOW) == 1.0


def test_boost_expires_after_window(tmp_path) -> None:
    policy = make_policy(tmp_path)
    stale_new = make_asset("img_aging", added_at=NOW - timedelta(days=30), legacy=False)
    assert policy.weight(MediaCandidate(stale_new, 0.4), now=NOW) == 0.4


def test_cooldown_blocks_recall_for_15_days(tmp_path) -> None:
    policy = make_policy(tmp_path)
    asset = make_asset("img_a")
    policy.ledger.mark_used(asset.asset_id, brand=BRAND_NAME, used_at=NOW - timedelta(days=14))
    assert policy.is_cooling(asset, now=NOW) is True
    assert policy.recall([MediaCandidate(asset, 1.0)], now=NOW) == []

    policy.ledger.mark_used(
        "img_b", brand=BRAND_NAME, content_id="var_b", used_at=NOW - timedelta(days=16)
    )
    old_use = make_asset("img_b")
    assert policy.is_cooling(old_use, now=NOW) is False
    assert len(policy.recall([MediaCandidate(old_use, 1.0)], now=NOW)) == 1


def test_recall_is_weighted_random_not_top_k(tmp_path) -> None:
    """权重相同时，两张图都应有机会排在首位（保证随机性）。"""
    policy = make_policy(tmp_path)
    first, second = make_asset("img_1"), make_asset("img_2")
    candidates = [MediaCandidate(first, 1.0), MediaCandidate(second, 1.0)]
    leaders = {
        policy.recall(candidates, top_k=1, now=NOW)[0].asset_id for _ in range(50)
    }
    assert leaders == {"img_1", "img_2"}


def test_new_asset_wins_most_of_the_time(tmp_path) -> None:
    policy = make_policy(tmp_path)
    legacy = make_asset("img_old", legacy=True)
    fresh = make_asset("img_new", added_at=NOW - timedelta(days=1), legacy=False)
    candidates = [MediaCandidate(legacy, 0.6), MediaCandidate(fresh, 0.6)]
    wins = sum(
        1
        for _ in range(200)
        if policy.recall(candidates, top_k=1, now=NOW)[0].asset_id == "img_new"
    )
    assert wins > 100


def test_capacity_red_alert_below_threshold(tmp_path) -> None:
    policy = make_policy(tmp_path, threshold=3)
    assets = [make_asset(f"img_{i}") for i in range(3)]
    report = policy.capacity(assets, now=NOW)
    assert report.available == 3
    assert report.alert == "ok"

    policy.ledger.mark_used("img_0", brand=BRAND_NAME, used_at=NOW - timedelta(days=1))
    report = policy.capacity(assets, now=NOW)
    assert report.available == 2
    assert report.cooling == 1
    assert report.alert == "red"
    assert report.is_red is True


def test_capacity_default_threshold_is_112(tmp_path) -> None:
    policy = make_policy(tmp_path)
    assert policy.config.capacity_red_threshold == 112
    assert policy.config.cooldown_days == 15
    assets = [make_asset(f"img_{i}") for i in range(111)]
    assert policy.capacity(assets, now=NOW).alert == "red"
    assets.append(make_asset("img_111"))
    assert policy.capacity(assets, now=NOW).alert == "ok"


def test_capacity_counts_new_and_legacy(tmp_path) -> None:
    policy = make_policy(tmp_path)
    assets = [
        make_asset("img_legacy", legacy=True),
        make_asset("img_new", added_at=NOW, legacy=False),
    ]
    report = policy.capacity(assets, now=NOW)
    assert (report.new, report.legacy, report.total) == (1, 1, 2)


def test_video_assets_do_not_consume_image_capacity(tmp_path) -> None:
    """视频描述不计入图片库容量，单独统计。"""
    policy = make_policy(tmp_path, threshold=2)
    image = make_asset("img_photo")
    video = MediaAsset(
        asset_id="img_video",
        file_name="IMG_0001.mp4",
        process="铸件",
        sub_process="阀体",
        source_path="x.mp4",
        description_path="x.mp4.md",
        summary="浇注过程视频",
        keywords=(),
        added_at=None,
        is_legacy=True,
    )
    assert video.is_video is True
    report = policy.capacity([image, video], now=NOW)
    assert (report.total, report.videos, report.available) == (1, 1, 1)
    assert report.alert == "red"
