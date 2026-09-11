"""素材目录测试：头部解析、老图判定、汇总索引排除。"""

from __future__ import annotations

from datetime import datetime, timezone

from pulse.services.media.catalog import asset_id_for, load_catalog, parse_front_matter
from pulse.services.media.registry import MediaRegistry

SAMPLE = """---
source_file: "IMG_0001.jpg"
source_path: "D:/菲美得/菲美得产品图片/加工件/机床件/IMG_0001.jpg"
process: "加工件"
sub_process: "机床件"
content_type: "图片描述"
keywords: ["机床床身", "灰色底漆"]
---

# IMG_0001.jpg 图片描述

## 机床床身铸件批量堆放

画面中央可见多件机床床身铸件。
"""


def _write_sample(root) -> None:
    folder = root / "加工件" / "机床件"
    folder.mkdir(parents=True)
    (folder / "IMG_0001.md").write_text(SAMPLE, encoding="utf-8")
    (folder / "00_汇总索引.md").write_text("# 索引\n", encoding="utf-8")


def test_parse_front_matter_reads_lists_and_quotes() -> None:
    header, body = parse_front_matter(SAMPLE)
    assert header["process"] == "加工件"
    assert header["keywords"] == ["机床床身", "灰色底漆"]
    assert body.strip().startswith("# IMG_0001.jpg")


def test_missing_front_matter_is_tolerated() -> None:
    header, body = parse_front_matter("# 只有正文\n")
    assert header == {}
    assert body == "# 只有正文\n"


def test_existing_images_are_legacy_by_default(tmp_path) -> None:
    _write_sample(tmp_path)
    assets = load_catalog(tmp_path)
    assert len(assets) == 1
    asset = assets[0]
    assert asset.is_legacy is True
    assert asset.added_at is None
    assert asset.file_name == "IMG_0001.jpg"
    assert asset.category == "加工件/机床件"
    assert asset.summary == "机床床身铸件批量堆放"


def test_registered_image_becomes_new(tmp_path) -> None:
    _write_sample(tmp_path)
    registry = MediaRegistry(tmp_path / "_媒体登记表.json")
    asset_id = asset_id_for("加工件/机床件/IMG_0001.md")
    stamp = datetime(2026, 9, 11, tzinfo=timezone.utc)
    registry.add(
        asset_id,
        added_at=stamp,
        file_name="IMG_0001.jpg",
        process="加工件",
        sub_process="机床件",
        source_path="x.jpg",
        brand="沧州菲美得",
        content_hash="abc",
    )
    assets = load_catalog(tmp_path, registry)
    assert assets[0].is_legacy is False
    assert assets[0].added_at == stamp


def test_missing_root_returns_empty(tmp_path) -> None:
    assert load_catalog(tmp_path / "not-exists") == []
