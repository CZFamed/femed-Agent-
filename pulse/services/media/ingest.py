"""素材入库（所有者：root）。

一次入库做四件事：

1. 校验文件类型与体积，按内容哈希查重；
2. 把实拍图片落到素材目录（默认 ``菲美得产品图片/<process>/<sub_process>/``）；
3. 在 ``RAG知识库/图片描述/<process>/<sub_process>/`` 写一份与既有格式一致的描述 MD；
4. 重建该品类的 ``00_汇总索引.md``，并写入登记表（登记表决定它算"新图"）。

工业件铁律（AGENTS.md §3.6）：产品图只能用实拍素材，本模块不接受生成图。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from pulse.services.media.catalog import (
    SUMMARY_INDEX_NAME,
    MediaAsset,
    asset_id_for,
    is_summary_index_name,
    parse_front_matter,
)
from pulse.services.media.config import (
    ALLOWED_IMAGE_SUFFIXES,
    ALLOWED_VIDEO_SUFFIXES,
    BRAND_NAME,
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
)
from pulse.services.media.registry import MediaRegistry


class UnsupportedMediaError(ValueError):
    """文件类型或体积不符合入库要求。"""


class DuplicateMediaError(ValueError):
    """同一张图（内容哈希一致）已经入库。"""

    def __init__(self, asset_id: str, description_path: str) -> None:
        super().__init__(f"素材已存在：{asset_id}")
        self.asset_id = asset_id
        self.description_path = description_path


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _normalize_name(name: str) -> str:
    """只保留文件名部分，避免路径穿越。"""
    candidate = Path(name.replace("\\", "/")).name.strip()
    if not candidate or candidate in {".", ".."}:
        raise UnsupportedMediaError("文件名不合法")
    return candidate


class MediaIngestor:
    """把实拍图片与实拍视频加入 RAG 素材库。"""

    def __init__(
        self,
        *,
        rag_root: str | Path,
        media_root: str | Path,
        registry: MediaRegistry,
        brand: str = BRAND_NAME,
        max_bytes: int = MAX_IMAGE_BYTES,
    ) -> None:
        self.rag_root = Path(rag_root)
        self.media_root = Path(media_root)
        self.registry = registry
        self.brand = brand
        self.max_bytes = max_bytes

    # ---------- 对外入口 ----------
    def add_media(
        self,
        *,
        file_name: str,
        process: str,
        sub_process: str,
        data: bytes | None = None,
        source_path: str | Path | None = None,
        keywords: tuple[str, ...] = (),
        summary: str = "",
        details: str = "",
        added_at: datetime | None = None,
    ) -> MediaAsset:
        """入库一份实拍素材（图片或视频）并返回素材记录。"""
        safe_name = _normalize_name(file_name)
        suffix = Path(safe_name).suffix.lower()
        is_video = suffix in ALLOWED_VIDEO_SUFFIXES
        if not is_video and suffix not in ALLOWED_IMAGE_SUFFIXES:
            raise UnsupportedMediaError(
                f"不支持的素材格式：{suffix or '未知'}"
                f"（图片支持 {'/'.join(sorted(ALLOWED_IMAGE_SUFFIXES))}，"
                f"视频支持 {'/'.join(sorted(ALLOWED_VIDEO_SUFFIXES))}）"
            )
        # sub_process 可以为空：库里"人员"这类品类本来就没有子目录
        if not process:
            raise UnsupportedMediaError("必须指定 process（品类）")

        payload = self._read_payload(data, source_path)
        content_hash = hashlib.sha256(payload).hexdigest()
        existing = self.registry.find_by_hash(content_hash)
        if existing:
            raise DuplicateMediaError(str(existing.get("asset_id")), str(existing.get("source_path")))

        stamp = added_at or datetime.now(timezone.utc)
        media_dir = self.media_root / process / sub_process
        rag_dir = self.rag_root / process / sub_process
        media_dir.mkdir(parents=True, exist_ok=True)
        rag_dir.mkdir(parents=True, exist_ok=True)

        target_image = media_dir / safe_name
        target_image.write_bytes(payload)
        # 视频描述的命名沿用库里既有约定：<文件名带扩展>.md（如 1.mp4.md）
        description_path = rag_dir / (
            f"{safe_name}.md" if is_video else f"{Path(safe_name).stem}.md"
        )
        # 同名描述已存在时拒绝：既有素材（老图）不在登记表里，光靠内容哈希查不出来，
        # 直接写下去会把原来那份描述覆盖掉——这是不可逆的数据损失。
        if description_path.exists():
            raise DuplicateMediaError(
                f"同名描述已存在，拒绝覆盖：{description_path.name}", str(description_path)
            )
        description_path.write_text(
            self._render_description(
                file_name=safe_name,
                image_path=target_image,
                process=process,
                sub_process=sub_process,
                keywords=keywords,
                summary=summary,
                details=details,
                content_type="视频描述" if is_video else "图片描述",
                added_at=stamp,
            ),
            encoding="utf-8",
        )
        self.rebuild_index(rag_dir, process=process, sub_process=sub_process)

        relative = description_path.relative_to(self.rag_root).as_posix()
        asset_id = asset_id_for(relative)
        self.registry.add(
            asset_id,
            added_at=stamp,
            file_name=safe_name,
            process=process,
            sub_process=sub_process,
            source_path=str(target_image),
            brand=self.brand,
            content_hash=content_hash,
        )
        return MediaAsset(
            asset_id=asset_id,
            file_name=safe_name,
            process=process,
            sub_process=sub_process,
            source_path=str(target_image),
            description_path=str(description_path),
            summary=summary or safe_name,
            keywords=tuple(keywords),
            added_at=stamp,
            is_legacy=False,
            brand=self.brand,
        )

    def add_image(self, **kwargs: Any) -> MediaAsset:
        """兼容旧调用名：内部就是 ``add_media``（图片与视频同一入口）。"""
        return self.add_media(**kwargs)

    # ---------- 内部实现 ----------
    def _read_payload(self, data: bytes | None, source_path: str | Path | None) -> bytes:
        if data is not None:
            payload = data
        elif source_path is not None:
            payload = Path(source_path).read_bytes()
        else:
            raise UnsupportedMediaError("必须提供 data 或 source_path")
        if not payload:
            raise UnsupportedMediaError("空文件")
        if len(payload) > self.max_bytes:
            raise UnsupportedMediaError(f"文件超过上限 {self.max_bytes} 字节")
        return payload

    def _render_description(
        self,
        *,
        file_name: str,
        image_path: Path,
        process: str,
        sub_process: str,
        keywords: tuple[str, ...],
        summary: str,
        details: str,
        added_at: datetime,
        content_type: str = "图片描述",
    ) -> str:
        keyword_text = ", ".join(f'"{item}"' for item in keywords)
        heading = summary or f"{file_name} 实拍素材"
        body = details or "本图为新入库实拍素材，尚未补充细节说明。"
        return (
            "---\n"
            f'source_file: "{file_name}"\n'
            f'source_path: "{image_path.as_posix()}"\n'
            f'process: "{process}"\n'
            f'sub_process: "{sub_process}"\n'
            f'content_type: "{content_type}"\n'
            f"keywords: [{keyword_text}]\n"
            f'added_at: "{_iso(added_at)}"\n'
            f'brand: "{self.brand}"\n'
            "---\n\n"
            f"# {file_name} 图片描述\n\n"
            f"## {heading}\n\n"
            f"{body}\n"
        )

    def rebuild_index(
        self, rag_dir: Path, *, process: str | None = None, sub_process: str | None = None
    ) -> Path:
        """按目录内描述文件重建 ``00_汇总索引.md``。

        品类显式传入：没有子类的品类（如 ``人员``）用目录名反推会推错。
        """
        rows: list[tuple[str, str, str]] = []
        images = 0
        videos = 0
        for path in sorted(rag_dir.glob("*.md")):
            if is_summary_index_name(path.name):
                continue
            header, body = parse_front_matter(path.read_text(encoding="utf-8"))
            source_file = str(header.get("source_file") or path.stem)
            if source_file.lower().endswith((".mp4", ".mov", ".avi")):
                videos += 1
            else:
                images += 1
            rows.append((source_file, path.name, _first_line(body)))
        process = process if process is not None else rag_dir.parent.name
        sub_process = sub_process if sub_process is not None else rag_dir.name
        category = f"{process}/{sub_process}" if sub_process else process
        media_dir = self.media_root.joinpath(process, sub_process) if sub_process else (
            self.media_root / process
        )
        lines = [
            "---",
            f'source_folder: "{media_dir.as_posix()}"',
            f'process: "{process}"',
            f'sub_process: "{sub_process}"',
            'content_type: "汇总索引"',
            f"total_files: {len(rows)}",
            "---",
            "",
            f"# {category} 图片描述汇总索引",
            "",
            f"本目录存放“{category}”下全部 {len(rows)} 个媒体文件的 RAG 文字描述"
            f"（图片 {images} 张、视频 {videos} 个）。视频文件的描述以 `<文件名>.mp4.md` 命名。"
            "内容概览如下：",
            "",
            "| 序号 | 源文件 | 描述文件 | 内容概要 |",
            "|------|--------|----------|----------|",
        ]
        for index, (source_file, description, summary) in enumerate(rows, start=1):
            lines.append(f"| {index} | {source_file} | {description} | {summary} |")
        lines.append("")
        target = rag_dir / SUMMARY_INDEX_NAME
        target.write_text("\n".join(lines), encoding="utf-8")
        return target

    def copy_from_directory(self, source_dir: str | Path) -> list[MediaAsset]:
        """批量导入一个目录下的实拍图（跳过不支持格式）。"""
        created: list[MediaAsset] = []
        root = Path(source_dir)
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
                continue
            try:
                created.append(
                    self.add_image(
                        file_name=path.name,
                        process=path.parent.parent.name or root.name,
                        sub_process=path.parent.name,
                        source_path=path,
                    )
                )
            except (DuplicateMediaError, UnsupportedMediaError):
                continue
        return created


def _first_line(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            return stripped[3:].strip().replace("|", "/")[:120]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped.replace("|", "/")[:120]
    return ""
