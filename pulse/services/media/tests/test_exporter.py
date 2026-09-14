"""导出测试：按位次复制、不覆盖、零修饰、说明与文案落盘。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pulse.services.media.exporter import (
    ExportEntry,
    export_entries,
    make_export_dir,
    safe_name,
)

NOW = datetime(2026, 9, 11, 15, 30)


def _source(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / "src" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_safe_name_strips_unsafe_characters() -> None:
    assert "/" not in safe_name("加工件/机床件")
    assert ":" not in safe_name('a:b*c?d"e')
    assert safe_name("") == "未命名"
    assert len(safe_name("很长的名字" * 30)) <= 40


def test_make_export_dir_never_overwrites(tmp_path) -> None:
    first = make_export_dir(tmp_path, label="LinkedIn", now=NOW)
    second = make_export_dir(tmp_path, label="LinkedIn", now=NOW)
    third = make_export_dir(tmp_path, label="LinkedIn", now=NOW)
    assert first != second != third
    assert first.name == "LinkedIn_20260911-1530"
    assert second.name.endswith("_2")
    assert third.name.endswith("_3")
    assert all(path.is_dir() for path in (first, second, third))


def test_export_copies_originals_byte_for_byte(tmp_path) -> None:
    """零修饰：导出的必须是原文件字节，不是压缩或改过的图。"""
    payload = b"\xff\xd8\xff\xe0" + b"real-photo-bytes" * 100
    source = _source(tmp_path, "IMG_1.jpg", payload)
    result = export_entries(
        root=tmp_path / "out",
        label="LinkedIn",
        entries=[ExportEntry(order=1, role="能力证明", file_name="IMG_1.jpg", source=source)],
        now=NOW,
    )
    exported = result.directory / result.copied[0]
    assert exported.read_bytes() == payload
    assert result.file_count == 1
    assert result.missing == ()


def test_export_names_files_in_slot_order(tmp_path) -> None:
    entries = [
        ExportEntry(
            order=index,
            role=f"位次{index}",
            file_name=f"IMG_{index}.jpg",
            category="铸件/阀体",
            source=_source(tmp_path, f"IMG_{index}.jpg", bytes([index]) * 10),
        )
        for index in (1, 2, 3)
    ]
    result = export_entries(root=tmp_path / "out", label="VK", entries=entries, now=NOW)
    assert result.copied[0].startswith("01_")
    assert result.copied[1].startswith("02_")
    assert result.copied[2].startswith("03_")
    assert all("铸件_阀体" in name for name in result.copied), "品类要进文件名"


def test_export_deduplicates_same_asset(tmp_path) -> None:
    """同一条素材被两个位次选中时只复制一份，说明里注明。"""
    source = _source(tmp_path, "IMG_SAME.jpg", b"same")
    entries = [
        ExportEntry(order=1, role="位次一", file_name="IMG_SAME.jpg", source=source),
        ExportEntry(order=2, role="位次二", file_name="IMG_SAME.jpg", source=source),
    ]
    result = export_entries(root=tmp_path / "out", label="LinkedIn", entries=entries, now=NOW)
    assert result.file_count == 1
    notes = (result.directory / "说明.txt").read_text(encoding="utf-8-sig")
    assert "同图" in notes


def test_export_reports_missing_source(tmp_path) -> None:
    entries = [
        ExportEntry(order=1, role="位次一", file_name="GONE.jpg", source=None),
        ExportEntry(
            order=2,
            role="位次二",
            file_name="IMG_OK.jpg",
            source=_source(tmp_path, "IMG_OK.jpg", b"ok"),
        ),
    ]
    result = export_entries(root=tmp_path / "out", label="Facebook", entries=entries, now=NOW)
    assert result.missing == ("GONE.jpg",)
    assert result.file_count == 1
    notes = (result.directory / "说明.txt").read_text(encoding="utf-8-sig")
    assert "找不到素材文件" in notes
    assert "没有导出成功" in notes


def test_export_writes_notes_with_roles_and_zero_retouch_notice(tmp_path) -> None:
    entries = [
        ExportEntry(
            order=1,
            role="能力证明：精加工成品与数控机床同框",
            file_name="IMG_1.jpg",
            category="加工件/机床件",
            summary="大型机床床身导轨面加工",
            note="报告 §3.1：直接证明铸件与粗加工同厂",
            source=_source(tmp_path, "IMG_1.jpg", b"x"),
        )
    ]
    result = export_entries(
        root=tmp_path / "out",
        label="LinkedIn",
        entries=entries,
        spec_summary="平台：LinkedIn（P0-A）｜画幅 4:5",
        now=NOW,
    )
    notes = (result.directory / "说明.txt").read_text(encoding="utf-8-sig")
    assert "LinkedIn 素材包" in notes
    assert "零修饰" in notes
    assert "能力证明：精加工成品与数控机床同框" in notes
    assert "大型机床床身导轨面加工" in notes
    assert "平台：LinkedIn（P0-A）" in notes


def test_export_writes_caption_file(tmp_path) -> None:
    entries = [
        ExportEntry(
            order=1,
            role="位次一",
            file_name="IMG_1.jpg",
            source=_source(tmp_path, "IMG_1.jpg", b"x"),
        )
    ]
    caption = {
        "platform_name": "LinkedIn",
        "text": "Most machine tool builders don't own a foundry.",
        "hashtags": ["#casting", "#foundry"],
        "first_comment": "Capability sheet here → [link]",
        "checks": [{"name": "长度", "ok": True, "detail": "700 字符"}],
        "warnings": [],
    }
    result = export_entries(
        root=tmp_path / "out",
        label="LinkedIn",
        entries=entries,
        caption=caption,
        now=NOW,
    )
    text = (result.directory / "文案.txt").read_text(encoding="utf-8-sig")
    assert "Most machine tool builders" in text
    assert "#casting #foundry" in text
    assert "第一条评论" in text
    assert "发布前自查" in text
    assert "✓ 长度" in text


def test_export_without_caption_skips_caption_file(tmp_path) -> None:
    entries = [
        ExportEntry(
            order=1,
            role="位次",
            file_name="IMG_1.jpg",
            source=_source(tmp_path, "IMG_1.jpg", b"x"),
        )
    ]
    result = export_entries(root=tmp_path / "out", label="TikTok", entries=entries, now=NOW)
    assert not (result.directory / "文案.txt").exists()
    assert (result.directory / "说明.txt").exists()


def test_export_handles_video_files(tmp_path) -> None:
    entries = [
        ExportEntry(
            order=1,
            role="浇铸现场视频",
            file_name="pour.mp4",
            category="厂区_场景/厂房",
            source=_source(tmp_path, "pour.mp4", b"\x00\x00\x00\x18ftypmp42"),
        )
    ]
    result = export_entries(root=tmp_path / "out", label="Facebook", entries=entries, now=NOW)
    assert result.copied[0].endswith(".mp4")


def test_export_converts_psd_to_png(tmp_path) -> None:
    """PSD 发不到社媒：导出时除了原文件，还要落一份同画面的 PNG。"""
    from pulse.services.media.tests.test_psd import build_psd, parse_png, rgb_planes

    payload = build_psd(rgb_planes(4, 3), width=4, height=3)
    entries = [
        ExportEntry(
            order=1,
            role="产品图（设计稿）",
            file_name="海报.psd",
            category="生产流程/黄模",
            source=_source(tmp_path, "海报.psd", payload),
        )
    ]
    result = export_entries(root=tmp_path / "out", label="VK", entries=entries, now=NOW)
    names = list(result.copied)
    assert any(name.endswith(".psd") for name in names), "原 PSD 要一起给"
    png = [name for name in names if name.endswith(".png")]
    assert len(png) == 1, "同时要有一份可直接发布的 PNG"
    info = parse_png((result.directory / png[0]).read_bytes())
    assert (info["width"], info["height"]) == (4, 3)
    assert (result.directory / names[0]).read_bytes() == payload, "原文件仍是字节级复制"
    notes = (result.directory / "说明.txt").read_text(encoding="utf-8-sig")
    assert "PSD" in notes and "未做任何修饰" in notes


def test_export_psd_that_cannot_decode_still_ships_original(tmp_path) -> None:
    """解不出来时如实说明，但不能因此丢掉原文件。"""
    entries = [
        ExportEntry(
            order=1,
            role="位次",
            file_name="坏文件.psd",
            category="铸件/壳体",
            source=_source(tmp_path, "坏文件.psd", b"8BPS-not-really"),
        )
    ]
    result = export_entries(root=tmp_path / "out", label="VK", entries=entries, now=NOW)
    assert result.file_count == 1
    assert result.copied[0].endswith(".psd")
    notes = (result.directory / "说明.txt").read_text(encoding="utf-8-sig")
    assert "转换 PNG 失败" in notes


def test_export_payload_is_serialisable(tmp_path) -> None:
    entries = [
        ExportEntry(
            order=1,
            role="位次",
            file_name="IMG_1.jpg",
            source=_source(tmp_path, "IMG_1.jpg", b"x"),
        )
    ]
    payload = export_entries(
        root=tmp_path / "out", label="VK", entries=entries, now=NOW
    ).as_payload()
    assert isinstance(payload["directory"], str)
    assert payload["file_count"] == 1
    assert isinstance(payload["copied"], list)
