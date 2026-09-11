"""召回结果导出（所有者：root）。

把"这一帖要用的素材"落成一个人能直接拿去用的文件夹：

1. 按**位次顺序**复制素材原图（``01_…``、``02_…``），视频也一并带上；
2. 写一份 ``说明.txt``：每个位次要什么画面、实际用的是哪个文件；
3. 有短文时写一份 ``文案.txt``：正文 + 标签 + 第一条评论 + 英语母版，复制即用。

两条硬约束：

- **零修饰**：只做字节级复制（``shutil.copy2``），不裁、不压、不调色——
  报告 §0.1 明确取消全部后期加工，导出环节也不例外。
- **不覆盖已有目录**：重名时自动加序号，绝不静默盖掉上一次的导出。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

#: 导出文件名里不允许出现的字符
UNSAFE_CHARS = '<>:"/\\|?*'


@dataclass(frozen=True)
class ExportEntry:
    """一个位次要导出的一条素材。"""

    order: int
    role: str
    file_name: str
    category: str = ""
    summary: str = ""
    note: str = ""
    source: Path | None = None


@dataclass(frozen=True)
class ExportResult:
    directory: Path
    copied: tuple[str, ...] = field(default=())
    missing: tuple[str, ...] = field(default=())

    @property
    def file_count(self) -> int:
        return len(self.copied)

    def as_payload(self) -> dict[str, object]:
        return {
            "directory": str(self.directory),
            "copied": list(self.copied),
            "missing": list(self.missing),
            "file_count": self.file_count,
        }


def safe_name(text: str, *, limit: int = 40) -> str:
    """把品类/角色名压成可做文件名的形式。"""
    cleaned = "".join("_" if char in UNSAFE_CHARS else char for char in str(text or ""))
    cleaned = cleaned.replace("/", "_").strip().strip(".")
    return cleaned[:limit] or "未命名"


def make_export_dir(root: str | Path, *, label: str, now: datetime | None = None) -> Path:
    """在 root 下建一个不重名的导出目录。"""
    base = Path(root)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    prefix = f"{safe_name(label, limit=24)}_{stamp}"
    candidate = base / prefix
    index = 2
    while candidate.exists():
        candidate = base / f"{prefix}_{index}"
        index += 1
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def export_entries(
    *,
    root: str | Path,
    label: str,
    entries: list[ExportEntry],
    caption: dict[str, object] | None = None,
    spec_summary: str = "",
    now: datetime | None = None,
) -> ExportResult:
    """把位次素材复制成文件夹并写说明；返回落盘结果。"""
    directory = make_export_dir(root, label=label, now=now)
    copied: list[str] = []
    missing: list[str] = []
    lines: list[str] = [
        f"{label} 素材包",
        f"导出时间：{(now or datetime.now()).strftime('%Y-%m-%d %H:%M')}",
    ]
    if spec_summary:
        lines.append(spec_summary)
    lines += [
        "",
        "说明：以下均为实拍原图，未做任何裁剪、调色或加字（零修饰口径）。",
        "文件名前的序号就是发帖时的排列顺序。",
        "",
    ]

    # 同一条素材可能被两个位次选中：只复制一份，在说明里注明覆盖了哪些位次
    seen: dict[str, str] = {}
    for entry in entries:
        if entry.source is None or not Path(entry.source).is_file():
            missing.append(entry.file_name)
            lines.append(f"{entry.order}. {entry.role}")
            lines.append(f"   ⚠ 找不到素材文件：{entry.file_name}（跳过）")
            lines.append("")
            continue

        key = str(Path(entry.source).resolve())
        if key in seen:
            lines.append(f"{entry.order}. {entry.role}")
            lines.append(f"   → 与 {seen[key]} 同图（{entry.file_name}）")
            lines.append("")
            continue

        number = len(copied) + 1
        suffix = Path(entry.file_name).suffix or Path(entry.source).suffix
        target_name = (
            f"{number:02d}_{safe_name(Path(entry.file_name).stem)}"
            f"_{safe_name(entry.category)}{suffix}"
        )
        try:
            shutil.copy2(entry.source, directory / target_name)
        except OSError as exc:
            missing.append(entry.file_name)
            lines.append(f"{entry.order}. {entry.role}")
            lines.append(f"   ⚠ 复制失败：{exc}")
            lines.append("")
            continue
        copied.append(target_name)
        seen[key] = target_name
        lines.append(f"{entry.order}. {entry.role}")
        lines.append(f"   → {target_name}")
        if entry.summary:
            lines.append(f"      画面：{entry.summary}")
        if entry.note:
            lines.append(f"      口径：{entry.note}")
        lines.append("")

    if missing:
        lines += ["", f"⚠ 有 {len(missing)} 条素材没有导出成功，请检查素材文件是否还在原位置。"]

    (directory / "说明.txt").write_text("\n".join(lines), encoding="utf-8-sig")

    if caption:
        (directory / "文案.txt").write_text(_caption_text(caption), encoding="utf-8-sig")

    return ExportResult(directory=directory, copied=tuple(copied), missing=tuple(missing))


def _caption_text(caption: dict[str, object]) -> str:
    """把短文整理成可直接复制的文本。"""
    lines: list[str] = []
    platform = caption.get("platform_name") or caption.get("platform") or ""
    lines.append(f"{platform} 短文（按报告模式生成，可直接复制）")
    lines.append("")
    lines.append(str(caption.get("text") or ""))
    hashtags = caption.get("hashtags") or []
    if hashtags:
        lines += ["", " ".join(str(tag) for tag in hashtags)]
    if caption.get("first_comment"):
        lines += [
            "",
            "—— 以下不要发在正文里 ——",
            "第一条评论（链接放这里）：",
            str(caption["first_comment"]),
        ]
    if caption.get("text_en"):
        lines += ["", "—— 英语母版（供复用与审校，不外发）——", str(caption["text_en"])]
    checks = caption.get("checks") or []
    if checks:
        lines += ["", "—— 发布前自查 ——"]
        for item in checks:
            mark = "✓" if item.get("ok") else "！"
            lines.append(f"{mark} {item.get('name')}：{item.get('detail')}")
    warnings = caption.get("warnings") or []
    if warnings:
        lines += ["", "⚠ " + "；".join(str(item) for item in warnings)]
    return "\n".join(lines)
