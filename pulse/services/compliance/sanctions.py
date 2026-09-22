"""制裁与出口管制筛查（所有者：A4，任务包 W2-A4-5）。

这是本项目的红线功能：尽调报告显示美国正重点监控"中国到俄罗斯"的 CNC/机床供应链，
铸件与机床功能件出口方与之交易可能触发次级制裁（见派工单 §4）。

三条口径必须写死在这里，避免以后被误读：

1. 筛查结论是"尽调记录"，不是"法律豁免"：clear 只代表在所列清单中未命中且已留痕，
   不得输出"合规/合法/无风险"这类结论性表述。
2. 清单是本地静态数据（data/sanctions_lists.json），当前条目为空且更新日期标注
   TODO(need-real-data)，机制先立起来，真实名单由人工按季度导入（AGENTS.md §3.4 的留痕义务）。
3. 除清单命中之外，还有一层项目级风险提示（review_hints）：出现俄罗斯、土耳其等关键词时
   置 review_required，必须人工复核；这层不是法律清单，别混为一谈。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from pulse.services.compliance.errors import RuleConfigError
from pulse.services.compliance.lexicon import SANCTIONS_LISTS_FILE, load_json
from pulse.shared.enums import ScreeningResult

#: 筛查结论的免责口径（任何对外输出都必须带上，避免被当成法律意见）
DISCLAIMER = (
    "本结论仅表示在所列清单中未命中且已留痕，是尽调记录，不构成法律意见或合规豁免。"
)

#: 企业名归一化时剥离的后缀（跨国写法差异很大，先统一再比对）
_LEGAL_SUFFIXES = (
    "co., ltd.", "co.,ltd.", "co. ltd.", "co ltd", "company limited", "limited",
    "gmbh", "s.a.", "sa", "inc.", "inc", "llc", "l.l.c.", "pte ltd", "pte. ltd.",
    "pte", "corp.", "corp", "corporation", "a.s.", "as", "ag", "bv", "b.v.",
    "有限公司", "股份公司", "公司", "集团",
)

_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")


def normalize_subject(raw: str) -> str:
    """把主体名归一化：小写、去标点、压空白、剥常见公司后缀。"""
    text = str(raw or "").strip().lower()
    text = _PUNCT.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    changed = True
    while changed and text:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            marker = suffix.lower().rstrip(".")
            if text.endswith(marker) and len(text) > len(marker):
                text = text[: -len(marker)].strip(" ,.-")
                changed = True
    return text.strip()


@dataclass(frozen=True, slots=True)
class SanctionsList:
    name: str
    source: str = ""
    updated: str = "TODO(need-real-data)"
    entries: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScreeningRecord:
    """筛查记录（契约 §6 sanctions_screenings 的一行）。"""

    subject: str
    result: ScreeningResult
    lists_checked: tuple[str, ...]
    evidence: Mapping[str, Any] = field(default_factory=dict)
    checked_by: str | None = None
    checked_at: datetime | None = None

    @property
    def disclaimer(self) -> str:
        return DISCLAIMER

    def as_payload(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "result": self.result.value,
            "lists_checked": list(self.lists_checked),
            "evidence": dict(self.evidence),
            "checked_by": self.checked_by,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "disclaimer": self.disclaimer,
        }


def load_lists() -> tuple[tuple[SanctionsList, ...], tuple[str, ...]]:
    """读取本地清单与风险提示词。清单条目可以为空（见模块 docstring 第 2 条）。"""
    raw = load_json(SANCTIONS_LISTS_FILE)
    items = raw.get("lists") or []
    if not isinstance(items, list):
        raise RuleConfigError(f"{SANCTIONS_LISTS_FILE} 的 lists 必须是数组")
    lists: list[SanctionsList] = []
    for item in items:
        if not isinstance(item, Mapping) or not item.get("name"):
            raise RuleConfigError(f"{SANCTIONS_LISTS_FILE} 的清单条目缺少 name：{item!r}")
        entries = item.get("entries") or []
        if not isinstance(entries, list):
            raise RuleConfigError(f"{SANCTIONS_LISTS_FILE} 的 entries 必须是数组：{item!r}")
        lists.append(
            SanctionsList(
                name=str(item["name"]),
                source=str(item.get("source") or ""),
                updated=str(item.get("updated") or ""),
                entries=tuple(str(entry) for entry in entries),
            )
        )
    hints_raw = (raw.get("review_hints") or {}).get("keywords") or []
    return tuple(lists), tuple(str(item) for item in hints_raw)


def _match_entry(subject_norm: str, entry: str) -> str | None:
    """命中判定：归一化后完全相等，或一方完整包含另一方（取较短者做子串检查）。"""
    entry_norm = normalize_subject(entry)
    if not entry_norm or not subject_norm:
        return None
    if subject_norm == entry_norm:
        return entry
    shorter, longer = sorted((subject_norm, entry_norm), key=len)
    if len(shorter) >= 4 and shorter in longer:
        return entry
    return None


def screen(
    subject: str,
    *,
    lists: Sequence[SanctionsList] | None = None,
    review_hints: Sequence[str] | None = None,
    checked_by: str | None = None,
    now: datetime | None = None,
) -> ScreeningRecord:
    """对主体名做一次筛查，返回可留痕的记录。

    Args:
        subject: 客户名 / 主体名（必填，空值抛错——没主体就没有筛查对象）。
        lists: 清单；默认读本地数据文件，测试可注入。
        review_hints: 项目级风险关键词；默认读本地数据文件。
        checked_by: 执行人（留痕用）。
        now: 时间源（测试注入固定时钟）。

    Returns:
        `ScreeningRecord`；`lists_checked` 记录实际查了哪些清单（含更新日期可在 evidence 里核）。
    """
    name = str(subject or "").strip()
    if not name:
        raise RuleConfigError("筛查主体不能为空：没有主体名称的筛查没有意义")

    if lists is None or review_hints is None:
        default_lists, default_hints = load_lists()
        lists = default_lists if lists is None else lists
        review_hints = default_hints if review_hints is None else review_hints

    moment = now or datetime.now(timezone.utc)
    subject_norm = normalize_subject(name)

    hits: list[dict[str, str]] = []
    for item in lists:
        for entry in item.entries:
            matched = _match_entry(subject_norm, entry)
            if matched:
                hits.append({"list": item.name, "entry": matched})

    if hits:
        return ScreeningRecord(
            subject=name,
            result=ScreeningResult.HIT,
            lists_checked=tuple(item.name for item in lists),
            evidence={
                "subject_normalized": subject_norm,
                "hits": hits,
                "lists_updated": {item.name: item.updated for item in lists},
                "next_step": "命中即停止接触该主体，并交法务复核（本项目红线）",
            },
            checked_by=checked_by,
            checked_at=moment,
        )

    lowered = name.lower()
    hinted = [hint for hint in review_hints if hint.lower() in lowered]
    if hinted:
        return ScreeningRecord(
            subject=name,
            result=ScreeningResult.REVIEW_REQUIRED,
            lists_checked=tuple(item.name for item in lists),
            evidence={
                "subject_normalized": subject_norm,
                "review_hints": hinted,
                "lists_updated": {item.name: item.updated for item in lists},
                "next_step": "命中项目级风险提示（非法律清单），须人工复核后再决定是否接触",
            },
            checked_by=checked_by,
            checked_at=moment,
        )

    return ScreeningRecord(
        subject=name,
        result=ScreeningResult.CLEAR,
        lists_checked=tuple(item.name for item in lists),
        evidence={
            "subject_normalized": subject_norm,
            "lists_updated": {item.name: item.updated for item in lists},
            "note": "清单为空或未命中；清单条目需按季度由人工导入真实数据（TODO(need-real-data)）",
        },
        checked_by=checked_by,
        checked_at=moment,
    )


def screen_many(
    subjects: Sequence[str], **kwargs: Any
) -> tuple[ScreeningRecord, ...]:
    """批量筛查（每个主体各留一条记录，不做合并）。"""
    return tuple(screen(item, **kwargs) for item in subjects)
