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


def test_missing_process_is_rejected_but_sub_process_is_optional(tmp_path) -> None:
    """品类必填；子类可空——库里"人员"这类品类本来就没有子目录。"""
    ingestor = make_ingestor(tmp_path)
    with pytest.raises(UnsupportedMediaError):
        ingestor.add_image(file_name="a.jpg", process="", sub_process="", data=JPEG)

    asset = ingestor.add_image(
        file_name="IMG_STAFF.jpg",
        process="人员",
        sub_process="",
        data=JPEG,
        summary="车间人员合影",
        added_at=NOW,
    )
    assert asset.category == "人员"
    assert (tmp_path / "RAG知识库" / "图片描述" / "人员" / "IMG_STAFF.md").is_file()
    index = (tmp_path / "RAG知识库" / "图片描述" / "人员" / "00_汇总索引.md").read_text(
        encoding="utf-8"
    )
    # 索引里的品类不能被目录名反推成"图片描述/人员"
    assert '# 人员 图片描述汇总索引' in index
    assert 'process: "人员"' in index


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


# ---------- 视频入库 ----------


def test_video_is_accepted_and_named_like_the_existing_library(tmp_path) -> None:
    """库里视频描述一律叫 `<文件名带扩展>.md`（如 1.mp4.md），新入库必须照这个约定。"""
    ingestor = make_ingestor(tmp_path)
    asset = ingestor.add_media(
        file_name="pour.mp4",
        process="生产流程",
        sub_process="发泡",
        data=b"\x00\x00\x00\x18ftypmp42fake-video-bytes",
        summary="浇铸区连续画面",
        keywords=("浇铸", "连续拍摄"),
        added_at=NOW,
    )
    assert asset.is_video is True
    assert asset.kind == "video"
    description = tmp_path / "RAG知识库" / "图片描述" / "生产流程" / "发泡" / "pour.mp4.md"
    assert description.is_file(), "视频描述应当是 <文件名>.mp4.md"
    text = description.read_text(encoding="utf-8")
    assert 'content_type: "视频描述"' in text
    assert (tmp_path / "菲美得产品图片" / "生产流程" / "发泡" / "pour.mp4").is_file()


def test_video_counts_in_index_and_not_in_image_capacity(tmp_path) -> None:
    ingestor = make_ingestor(tmp_path)
    ingestor.add_media(
        file_name="IMG_A.jpg", process="厂区_场景", sub_process="厂房", data=JPEG, added_at=NOW
    )
    ingestor.add_media(
        file_name="clip.mp4",
        process="厂区_场景",
        sub_process="厂房",
        data=b"\x00\x00\x00\x18ftypmp42clip",
        added_at=NOW,
    )
    index = tmp_path / "RAG知识库" / "图片描述" / "厂区_场景" / "厂房" / "00_汇总索引.md"
    text = index.read_text(encoding="utf-8")
    assert "全部 2 个媒体文件" in text
    assert "图片 1 张、视频 1 个" in text


def test_add_image_alias_still_works(tmp_path) -> None:
    """旧调用名保留，避免历史代码与测试一起返工。"""
    ingestor = make_ingestor(tmp_path)
    asset = ingestor.add_image(
        file_name="IMG_ALIAS.jpg",
        process="厂区_场景",
        sub_process="厂房",
        data=JPEG,
        added_at=NOW,
    )
    assert asset.file_name == "IMG_ALIAS.jpg"


def test_unsupported_video_suffix_is_rejected(tmp_path) -> None:
    ingestor = make_ingestor(tmp_path)
    with pytest.raises(UnsupportedMediaError, match="不支持的素材格式"):
        ingestor.add_media(
            file_name="movie.rmvb", process="厂区_场景", sub_process="厂房", data=b"x" * 32
        )


def test_existing_description_is_never_overwritten(tmp_path) -> None:
    """既有素材（老图）不在登记表里，光靠内容哈希查不出来；
    同名描述被覆盖是不可逆的数据损失，必须拒绝。"""
    ingestor = make_ingestor(tmp_path)
    folder = tmp_path / "RAG知识库" / "图片描述" / "铸件" / "阀体"
    folder.mkdir(parents=True, exist_ok=True)
    legacy = folder / "IMG_1706.md"
    legacy.write_text("老图原有的描述，绝不能被覆盖", encoding="utf-8")

    with pytest.raises(DuplicateMediaError, match="拒绝覆盖"):
        ingestor.add_media(
            file_name="IMG_1706.jpg",
            process="铸件",
            sub_process="阀体",
            data=JPEG + b"different-content",
            added_at=NOW,
        )
    assert legacy.read_text(encoding="utf-8") == "老图原有的描述，绝不能被覆盖"
