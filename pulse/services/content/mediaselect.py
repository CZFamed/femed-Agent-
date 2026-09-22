"""素材选用器（所有者：A1，任务包 W1-A1-5）。

复用媒体资产域（pulse/services/media/）的能力，而不是重造一套召回：

* platforms.slot_pools 给出每个平台每个"位次"的候选阶梯（精准 → 放宽 → 全库）；
* policy.RecallPolicy 负责冷却、新图加权与加权随机采样；
* 本模块只多做两件 A1 特有的事：
  1. 把"这一格是精准挑出来的、还是放宽来的"翻译成证据缺口，
     缺口一律标注 TODO(need-real-data)（AGENTS.md 铁律 7：不伪造、缺失要显式）；
  2. 把选中的素材压成喂给模型的"只含可见事实"清单。

一处要点：冷却不可为凑数而破例——媒体域 2026-09-20 的修正已经明确
"冷却始终不动"，所以本模块逐级放宽的只是题材匹配度，不碰冷却。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from pulse.services.content.errors import MediaSelectionError
from pulse.services.media.catalog import MediaAsset
from pulse.services.media.platforms import (
    RELAXED_LEVELS,
    level_label,
    platform_profile,
    slot_pools,
)
from pulse.services.media.policy import MediaCandidate, RecallPick, RecallPolicy

#: 缺口占位符：凡是拿不到真实证据的地方都用它，不允许用文字编造（铁律 7）
NEED_REAL_DATA = "TODO(need-real-data)"


@dataclass(frozen=True, slots=True)
class SlotSelection:
    """单个位次的选用结果。"""

    order: int
    role: str
    picks: tuple[RecallPick, ...] = ()
    #: 命中档位：strict / same_category / category_any_keyword / whole_library
    match_level: str = ""
    gap: str = ""

    @property
    def level_label(self) -> str:
        return level_label(self.match_level)

    @property
    def is_relaxed(self) -> bool:
        return self.match_level in RELAXED_LEVELS

    @property
    def is_gap(self) -> bool:
        """该位次是否构成"证据缺口"（没挑到，或只能放宽匹配）。"""
        return bool(self.gap)

    def as_payload(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "role": self.role,
            "match_level": self.match_level,
            "match_level_label": self.level_label,
            "is_gap": self.is_gap,
            "gap": self.gap,
            "picks": [
                {
                    "asset_id": pick.asset_id,
                    "file_name": pick.file_name,
                    "category": pick.category,
                    "summary": pick.summary,
                    "similarity": pick.similarity,
                    "weight": pick.weight,
                    "is_new": pick.is_new,
                }
                for pick in self.picks
            ],
        }


@dataclass(frozen=True, slots=True)
class MediaSelection:
    """一次平台级素材选用结果。"""

    platform: str
    aspect: str = ""
    slots: tuple[SlotSelection, ...] = field(default=())

    @property
    def gaps(self) -> tuple[str, ...]:
        """全部证据缺口（给 prompt 与页面用）。"""
        return tuple(slot.gap for slot in self.slots if slot.gap)

    @property
    def needs_reshoot(self) -> bool:
        """是否需要补拍——只要有一位次是缺口就为真。"""
        return any(slot.is_gap for slot in self.slots)

    @property
    def picked_asset_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for slot in self.slots:
            for pick in slot.picks:
                if pick.asset_id not in seen:
                    seen.append(pick.asset_id)
        return tuple(seen)

    def media_brief(self) -> str:
        """压成提示词里的素材清单：只含文件名、品类与画面描述（都是可见事实）。"""
        rows: list[str] = []
        index = 0
        for slot in self.slots:
            for pick in slot.picks:
                index += 1
                rows.append(
                    f"{index}. 位次{slot.order}（{slot.role}）｜文件 {pick.file_name}"
                    f"｜品类 {pick.category}｜画面 {pick.summary or '（无描述）'}"
                )
        return "\n".join(rows) if rows else "（未选到任何素材）"

    def as_payload(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "aspect": self.aspect,
            "needs_reshoot": self.needs_reshoot,
            "gaps": list(self.gaps),
            "slots": [slot.as_payload() for slot in self.slots],
        }


def _gap_message(slot_role: str, order: int, level: str) -> str:
    if level in RELAXED_LEVELS:
        return (
            f"{NEED_REAL_DATA}：位次{order}（{slot_role}）只匹配到「{level_label(level)}」档，"
            "画面与位次要求不完全对应，建议补拍后再用"
        )
    return f"{NEED_REAL_DATA}：位次{order}（{slot_role}）没有任何可用素材，需要补拍"


def has_slot_profile(platform: str) -> bool:
    """该平台是否已有位次口径（没有就只能跳过自动选素材）。"""
    return platform_profile(platform) is not None


def select_media(
    platform: str,
    assets: Sequence[MediaAsset],
    policy: RecallPolicy,
    *,
    top_k: int = 1,
    extra_terms: Iterable[str] = (),
    rng: random.Random | None = None,
) -> MediaSelection:
    """按平台位次逐格选素材，并把"缺口"显式标出来。

    Args:
        platform: 平台 key（linkedin / youtube / …）。
        assets: 素材清单（通常来自 media.catalog.load_catalog）。
        policy: 召回策略（冷却 + 新图加权 + 加权随机）。
        top_k: 每个位次取几张。
        extra_terms: 本次 brief 的额外关键词（会加大对应素材的得分）。
        rng: 仅供测试注入固定随机源。

    Raises:
        MediaSelectionError: 素材库为空，或该平台没有素材口径。
    """
    profile = platform_profile(platform)
    if profile is None:
        raise MediaSelectionError(
            f"平台 {platform!r} 没有位次口径（素材口径目前只覆盖 linkedin / facebook / tiktok / vk），"
            "无法按位次选素材",
            details={"platform": platform},
        )
    if not assets:
        raise MediaSelectionError(
            "素材库为空，无法选素材（先确认 RAG知识库/图片描述/ 已挂载）",
            details={"platform": platform},
        )
    if rng is not None:
        policy.rng = rng

    terms = tuple(str(item) for item in extra_terms)
    selections: list[SlotSelection] = []
    for slot in profile.slots:
        picks: tuple[RecallPick, ...] = ()
        matched_level = ""
        for level, scored in slot_pools(slot, list(assets), extra_terms=terms):
            if not scored:
                continue
            candidates = [MediaCandidate(asset=asset, similarity=score) for score, asset in scored]
            found = policy.recall(candidates, top_k=top_k)
            if found:
                picks = tuple(found)
                matched_level = level
                break
        if not picks:
            gap = _gap_message(slot.role, slot.order, "")
        elif matched_level in RELAXED_LEVELS:
            gap = _gap_message(slot.role, slot.order, matched_level)
        else:
            gap = ""
        selections.append(
            SlotSelection(
                order=slot.order,
                role=slot.role,
                picks=picks,
                match_level=matched_level,
                gap=gap,
            )
        )

    return MediaSelection(platform=profile.key, aspect=profile.aspect, slots=tuple(selections))
