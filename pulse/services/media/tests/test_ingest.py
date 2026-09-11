"""入库测试：描述与索引生成、登记表、查重、路径安全。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pulse.services.media.catalog import load_catalog
from pulse.services.media.ingest import DuplicateMediaError, MediaIngestor, UnsupportedMediaError
from pulse.services.media.registry import MediaRegistry

NOW = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
JPEG = b"\xff\xd8\xff\xe0" + b"pulse-test-image" * 4


def make_ingestor(tmp_path, *, max_bytes: int | None = None) -> MediaIngestor:
    rag_root = tmp_path / "RAG知识库" / "图片描述"
    media_root = tmp_path / "菲美得产品图片"
    registry = MediaRegistry(rag_root / "_媒体登记表.json")
    kwargs = {"max_bytes": max_bytes} if max_bytes is not None else {}
    return MediaIngestor(
        rag_root=rag_root, media_root=media_root, registry=registry, **kwargs
    )


def test_add_image_writes_description_index_and_registry(tmp_path) -> None:
    ingestor = make_ingestor(tmp_path)
    asset = ingestor.add_image(
        file_name="IMG_NEW.jpg",
        process="加工件",
        sub_process="机床件",
        data=JPEG,
        keywords=("机床床身", "灰口铸铁"),
        summary="新入库的机床床身实拍",
        details="表面已喷防锈底漆。",
        added_at=NOW,
    )
    assert asset.is_legacy is False
    image_path = tmp_path / "菲美得产品图片" / "加工件" / "机床件" / "IMG_NEW.jpg"
    assert image_path.read_bytes() == JPEG
    description = asset.description_path
    text = __import__("pathlib").Path(description).read_text(encoding="utf-8")
    assert 'keywords: ["机床床身", "灰口铸铁"]' in text
    assert 'brand: "沧州菲美得"' in text
    index = tmp_path / "RAG知识库" / "图片描述" / "加工件" / "机床件" / "00_汇总索引.md"
    assert "| 1 | IMG_NEW.jpg |" in index.read_text(encoding="utf-8")
    catalog = load_catalog(tmp_path / "RAG知识库" / "图片描述", ingestor.registry)
    assert len(catalog) == 1
    assert catalog[0].asset_id == asset.asset_id
    assert catalog[0].is_legacy is False


def test_duplicate_content_is_rejected(tmp_path) -> None:
    ingestor = make_ingestor(tmp_path)
    ingestor.add_image(
        file_name="IMG_A.jpg", process="铸件", sub_process="阀体", data=JPEG, added_at=NOW
    )
    with pytest.raises(DuplicateMediaError):
        ingestor.add_image(
            file_name="IMG_B.jpg", process="铸件", sub_process="阀体", data=JPEG, added_at=NOW
        )


def test_unsupported_suffix_is_rejected(tmp_path) -> None:
    ingestor = make_ingestor(tmp_path)
    with pytest.raises(UnsupportedMediaError):
        ingestor.add_image(
            file_name="notes.txt", process="铸件", sub_process="阀体", data=b"hello"
        )


def test_file_name_is_sanitized(tmp_path) -> None:
    ingestor = make_ingestor(tmp_path)
    asset = ingestor.add_image(
        file_name="../../evil.jpg",
        process="铸件",
        sub_process="阀体",
        data=JPEG,
        added_at=NOW,
    )
    assert asset.file_name == "evil.jpg"
    assert (tmp_path / "菲美得产品图片" / "铸件" / "阀体" / "evil.jpg").is_file()


def test_empty_and_oversized_payload_rejected(tmp_path) -> None:
    ingestor = make_ingestor(tmp_path)
    with pytest.raises(UnsupportedMediaError):
        ingestor.add_image(file_name="a.jpg", process="铸件", sub_process="阀体", data=b"")
    small = make_ingestor(tmp_path / "small", max_bytes=8)
    with pytest.raises(UnsupportedMediaError):
        small.add_image(
            file_name="b.jpg", process="铸件", sub_process="阀体", data=b"x" * 64
        )


def test_rebuild_index_counts_images(tmp_path) -> None:
    ingestor = make_ingestor(tmp_path)
    ingestor.add_image(
        file_name="IMG_1.jpg", process="厂区_场景", sub_process="厂房", data=JPEG, added_at=NOW
    )
    ingestor.add_image(
        file_name="IMG_2.jpg", process="厂区_场景", sub_process="厂房", data=JPEG + b"2",
        added_at=NOW,
    )
    index = tmp_path / "RAG知识库" / "图片描述" / "厂区_场景" / "厂房" / "00_汇总索引.md"
    text = index.read_text(encoding="utf-8")
    assert "全部 2 个媒体文件" in text
    assert "图片 2 张、视频 0 个" in text
