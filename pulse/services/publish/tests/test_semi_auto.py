"""半自动导出（P0 能力，派工单 §3 W1-A2-9 / §4 第 6 条）。"""

from __future__ import annotations

from pulse.services.publish.semi_auto import DEEP_LINKS, SemiAutoBundleExporter
from pulse.shared.enums import Platform


def test_bundle_has_all_four_fields(make_post) -> None:
    bundle = SemiAutoBundleExporter().export(make_post())

    assert bundle.text
    assert bundle.media_paths
    assert bundle.deep_link
    assert bundle.checklist


def test_text_is_copy_ready_and_zh_stays_internal(make_post) -> None:
    """文案包只含外发文案；`caption.text_zh` 仅供审校，**不**外发。"""

    post = make_post()
    bundle = SemiAutoBundleExporter().export(post)

    assert post.caption.text in bundle.text
    assert "#casting" in bundle.text and "#machining" in bundle.text
    assert post.caption.text_zh not in bundle.text


def test_media_urls_are_resolved(make_post) -> None:
    exporter = SemiAutoBundleExporter(
        media_url_resolver=lambda url: url.replace("s3://pulse-media/", "/mnt/media/")
    )
    bundle = exporter.export(make_post())
    assert bundle.media_paths == ("/mnt/media/images/valve-body-01.jpg",)


def test_deep_link_targets_official_entries(make_post) -> None:
    exporter = SemiAutoBundleExporter()
    assert exporter.export(make_post()).deep_link == DEEP_LINKS["linkedin"]
    assert exporter.export(make_post(Platform.YOUTUBE)).deep_link == DEEP_LINKS["youtube"]


def test_checklist_covers_license_and_platform_specifics(make_post) -> None:
    exporter = SemiAutoBundleExporter()

    linkedin = exporter.export(make_post())
    joined = " ".join(linkedin.checklist)
    assert "owned" in joined and "licensed" in joined
    assert "urn:li:organization:1234567" in joined

    youtube = exporter.export(make_post(Platform.YOUTUBE))
    assert any("processed" in item for item in youtube.checklist)

    reddit = exporter.export(make_post(Platform.REDDIT))
    assert any("foundry" in item for item in reddit.checklist)


def test_text_only_post_says_no_media_needed(make_post) -> None:
    bundle = SemiAutoBundleExporter().export(make_post(Platform.REDDIT))
    assert bundle.media_paths == ()
    assert any("纯文本" in item for item in bundle.checklist)


def test_platform_field_matches_post(make_post) -> None:
    assert SemiAutoBundleExporter().export(make_post()).platform == "linkedin"
