"""素材目录：扫描 RAG 图片描述库，生成可召回的素材清单（所有者：root）。

目录形态（既有资产，见 AGENTS.md §5.0）::

    RAG知识库/图片描述/<process>/<sub_process>/<文件名>.md
    RAG知识库/图片描述/<process>/<sub_process>/00_汇总索引.md

每个描述文件带 YAML 风格头部（source_file / process / sub_process / keywords）。
本模块只做只读解析，不写任何文件；入库写入见 ``ingest.py``。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pulse.services.media.config import BRAND_NAME
from pulse.services.media.registry import MediaRegistry

SUMMARY_INDEX_NAME = "00_汇总索引.md"

#: 汇总索引文件的 content_type 标记
SUMMARY_CONTENT_TYPE = "汇总索引"

#: 视频描述文件的源文件后缀（这类条目不计入"图片库容量"）
VIDEO_SUFFIXES: tuple[str, ...] = (".mp4", ".mov", ".avi", ".mkv", ".webm")


def is_summary_index_name(name: str) -> bool:
    """按文件名判断是否为汇总索引。

    约定是所有索引都以 ``00_`` 开头。库里实际不止 ``00_汇总索引.md`` 一种
    （例如 ``00_发泡工段汇总索引.md``），只比对固定文件名会把索引当成一张图片。
    """
    return str(name).startswith("00_")


def is_summary_index_header(header: dict[str, Any]) -> bool:
    """按描述头部判断是否为汇总索引（比文件名更可靠）。"""
    return str(header.get("content_type") or "").strip() == SUMMARY_CONTENT_TYPE


def asset_id_for(relative_path: str | Path) -> str:
    """由描述文件相对路径派生稳定素材 ID（前缀 `img_`）。"""
    digest = hashlib.sha1(str(relative_path).replace("\\", "/").encode("utf-8")).hexdigest()
    return f"img_{digest[:12]}"


def _split_items(raw: str) -> list[str]:
    items: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for char in raw:
        if quote:
            if char == quote:
                quote = None
            else:
                buf.append(char)
            continue
        if char in "\"'":
            quote = char
        elif char == ",":
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(char)
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return [item for item in items if item]


def _parse_value(raw: str) -> Any:
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        return _split_items(value[1:-1])
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value.isdigit():
        return int(value)
    return value


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """解析 `---` 包裹的头部；只支持标量与一维列表，避免引入 yaml 依赖。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            header: dict[str, Any] = {}
            for line in lines[1:index]:
                if ":" not in line:
                    continue
                key, _, raw = line.partition(":")
                header[key.strip()] = _parse_value(raw)
            return header, "\n".join(lines[index + 1 :])
    return {}, text


def _extract_summary(body: str) -> str:
    """取第一个二级标题或第一段正文作为摘要。"""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            return stripped[3:].strip()[:160]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:160]
    return ""


@dataclass(frozen=True)
class MediaAsset:
    """一张可召回的实拍素材。"""

    asset_id: str
    file_name: str
    process: str
    sub_process: str
    source_path: str
    description_path: str
    summary: str
    keywords: tuple[str, ...]
    added_at: datetime | None
    is_legacy: bool
    brand: str = BRAND_NAME

    @property
    def category(self) -> str:
        return f"{self.process}/{self.sub_process}" if self.sub_process else self.process

    @property
    def is_video(self) -> bool:
        return self.file_name.lower().endswith(VIDEO_SUFFIXES)

    @property
    def kind(self) -> str:
        return "video" if self.is_video else "image"


def load_catalog(
    rag_root: str | Path,
    registry: MediaRegistry | None = None,
    *,
    brand: str = BRAND_NAME,
) -> list[MediaAsset]:
    """扫描描述库，返回素材清单（按品类 / 文件名排序）。"""
    root = Path(rag_root)
    if not root.is_dir():
        return []
    assets: list[MediaAsset] = []
    for path in sorted(root.rglob("*.md")):
        if is_summary_index_name(path.name):
            continue
        relative = path.relative_to(root)
        asset_id = asset_id_for(relative.as_posix())
        header, body = parse_front_matter(path.read_text(encoding="utf-8"))
        if is_summary_index_header(header):
            continue
        added_at = registry.registered_at(asset_id) if registry else None
        keywords = header.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        folders = relative.parts[:-1]
        process = str(header.get("process") or (folders[0] if folders else ""))
        sub_process = str(header.get("sub_process") or (folders[1] if len(folders) > 1 else ""))
        assets.append(
            MediaAsset(
                asset_id=asset_id,
                file_name=str(header.get("source_file") or path.stem),
                process=process,
                sub_process=sub_process,
                source_path=str(header.get("source_path") or ""),
                description_path=str(path),
                summary=_extract_summary(body),
                keywords=tuple(str(item) for item in keywords),
                added_at=added_at,
                is_legacy=added_at is None,
                brand=brand,
            )
        )
    return assets
