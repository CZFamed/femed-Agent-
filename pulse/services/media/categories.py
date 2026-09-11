"""素材品类目录（所有者：root）。

既有 RAG 库的目录形态就是品类的事实标准::

    RAG知识库/图片描述/<process>/<sub_process>/<文件名>.md
    RAG知识库/图片描述/<process>/<文件名>.md          # 无子类的品类，如"人员"

本模块做两件事：

1. **只读扫描**出可选品类（``load_categories``），供控制台下拉框与提示词使用；
2. **关键词兜底匹配**（``match_category``）——视觉模型没给出合法品类时，
   用识别出的关键词去猜一个；猜不出就老实返回空，交给人工确认。

不在这里硬编码品类清单：目录变了，下拉框自动跟着变。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pulse.services.media.catalog import (
    SUMMARY_CONTENT_TYPE,
    is_summary_index_name,
)

#: 目录扫描时跳过的名字前缀（隐藏目录、缓存目录）
SKIP_PREFIXES = (".", "_")


@dataclass(frozen=True)
class Category:
    """一个可入库的品类。``sub_process`` 为空表示该品类没有子类（如"人员"）。"""

    process: str
    sub_process: str = ""
    count: int = 0

    @property
    def label(self) -> str:
        return f"{self.process}/{self.sub_process}" if self.sub_process else self.process


@dataclass(frozen=True)
class CategoryCatalog:
    """品类目录快照。"""

    entries: tuple[Category, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.entries)

    @property
    def processes(self) -> tuple[str, ...]:
        """品类列表，按库大小降序（下拉框里常用的排前面）。"""
        totals: dict[str, int] = {}
        for entry in self.entries:
            totals[entry.process] = totals.get(entry.process, 0) + entry.count
        return tuple(sorted(totals, key=lambda name: (-totals[name], name)))

    def sub_processes(self, process: str) -> tuple[Category, ...]:
        return tuple(entry for entry in self.entries if entry.process == process)

    def has(self, process: str, sub_process: str) -> bool:
        return self.find(process, sub_process) is not None

    def find(self, process: str, sub_process: str) -> Category | None:
        target = (sub_process or "").strip()
        for entry in self.entries:
            if entry.process == (process or "").strip() and entry.sub_process == target:
                return entry
        return None

    def prompt_text(self) -> str:
        """给视觉模型看的可选品类清单（按库大小排序，便于模型优先选主品类）。"""
        ordered = sorted(self.entries, key=lambda item: (-item.count, item.label))
        return "；".join(f"{entry.label}（{entry.count}）" for entry in ordered)

    def as_payload(self) -> list[dict[str, object]]:
        """给控制台下拉框用的结构。"""
        ordered = sorted(self.entries, key=lambda item: (-item.count, item.label))
        return [
            {
                "process": entry.process,
                "sub_process": entry.sub_process,
                "label": entry.label,
                "count": entry.count,
            }
            for entry in ordered
        ]


def load_categories(rag_root: str | Path) -> CategoryCatalog:
    """扫描描述库，得到可选品类及各自已有描述条数。"""
    root = Path(rag_root)
    if not root.is_dir():
        return CategoryCatalog()
    entries: list[Category] = []
    for process_dir in sorted(root.iterdir()):
        if not process_dir.is_dir() or process_dir.name.startswith(SKIP_PREFIXES):
            continue
        sub_dirs = sorted(
            item
            for item in process_dir.iterdir()
            if item.is_dir() and not item.name.startswith(SKIP_PREFIXES)
        )
        if sub_dirs:
            for sub_dir in sub_dirs:
                entries.append(
                    Category(
                        process=process_dir.name,
                        sub_process=sub_dir.name,
                        count=_count_descriptions(sub_dir),
                    )
                )
        else:
            entries.append(
                Category(
                    process=process_dir.name,
                    sub_process="",
                    count=_count_descriptions(process_dir),
                )
            )
    return CategoryCatalog(entries=tuple(entries))


def _count_descriptions(directory: Path) -> int:
    """数这个目录里真正的素材描述条数，索引文件不算。"""
    count = 0
    for path in directory.glob("*.md"):
        if is_summary_index_name(path.name):
            continue
        # 文件名不合约定、但头部标了汇总索引的，也要排除
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:300]
        except OSError:
            head = ""
        if f"content_type: \"{SUMMARY_CONTENT_TYPE}\"" in head:
            continue
        count += 1
    return count


def _bigrams(text: str) -> set[str]:
    """中文没有空格，用二元组做粗糙的"正文里是否提到过"判断。"""
    cleaned = text.replace("_", "").replace("/", "").strip()
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[index : index + 2] for index in range(len(cleaned) - 1)}


def _mentioned(name: str, haystack: str) -> bool:
    """名字本身或其二元组出现在正文里，就认为提到了。"""
    if not name:
        return False
    if name in haystack:
        return True
    return bool(_bigrams(name) & _bigrams(haystack))


def match_category(
    text: str,
    keywords: tuple[str, ...] | list[str] = (),
    catalog: CategoryCatalog | None = None,
) -> tuple[str, str, str]:
    """按识别出的关键词兜底猜品类，返回 ``(process, sub_process, 来源)``。

    来源为 ``"keyword"`` 或 ``"none"``。猜不出时返回空字符串——宁可留空让
    人工确认，也不塞一个错的品类进去。
    """
    catalog = catalog or CategoryCatalog()
    if not catalog:
        return "", "", "none"
    haystack = " ".join([text or "", *[str(item) for item in keywords]])

    best: Category | None = None
    best_score = 0
    for entry in catalog.entries:
        score = 0
        if _mentioned(entry.process, haystack):
            score += 2
        if entry.sub_process and _mentioned(entry.sub_process, haystack):
            score += 3
        if score == 0:
            continue
        # 同分时选库更大的品类，避免"阀体"这类同名子类抢错父类
        if best is None or (score, entry.count) > (best_score, best.count):
            best, best_score = entry, score
    if best is None or best_score < 2:
        return "", "", "none"
    return best.process, best.sub_process, "keyword"


def resolve_category(
    process: str,
    sub_process: str,
    *,
    text: str = "",
    keywords: tuple[str, ...] | list[str] = (),
    catalog: CategoryCatalog | None = None,
) -> tuple[str, str, str]:
    """定品类：模型给的合法就用，否则按关键词兜底，再不行留空交给人工。

    返回 ``(process, sub_process, 来源)``，来源为 ``vision`` / ``keyword`` / ``none``。
    """
    candidate_process = (process or "").strip()
    candidate_sub = (sub_process or "").strip()
    if not catalog:
        # 没有品类目录（例如旧库）时不拦，原样采信模型的判断
        return (candidate_process, candidate_sub, "vision") if candidate_process else ("", "", "none")
    if catalog.has(candidate_process, candidate_sub):
        return candidate_process, candidate_sub, "vision"
    guessed_process, guessed_sub, how = match_category(text, keywords, catalog)
    if how == "keyword":
        return guessed_process, guessed_sub, "keyword"
    return "", "", "none"
