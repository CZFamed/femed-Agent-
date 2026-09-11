"""素材召回策略（所有者：root）。

策略同时满足两条业务约束：

1. **时效性**：新入库图片获得加权，优先被召回（既有图全部视为老图，不加权）。
2. **随机性 + 冷却**：同一张图进入内容 / 发布后 15 天内不得再次召回；
   候选之间采用加权随机采样，而不是"永远取相似度最高的同一张"。

采样用 A-Res（Efraimidis–Spirakis）算法：``key = u ** (1 / weight)`` 取前 K，
它是无放回加权随机采样，权重越高被选中概率越大，但低权重候选仍有机会。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from pulse.services.media.catalog import MediaAsset
from pulse.services.media.config import RecallConfig
from pulse.services.media.ledger import RecallLedger


@dataclass(frozen=True)
class MediaCandidate:
    """一条候选素材（相似度由 RAG 向量检索给出）。"""

    asset: MediaAsset
    similarity: float


@dataclass(frozen=True)
class RecallPick:
    """召回结果。"""

    asset_id: str
    file_name: str
    similarity: float
    weight: float
    is_new: bool
    category: str
    summary: str


@dataclass(frozen=True)
class CapacityReport:
    """素材池容量与预警。"""

    brand: str
    total: int
    available: int
    cooling: int
    new: int
    legacy: int
    threshold: int
    alert: str
    videos: int = 0

    @property
    def is_red(self) -> bool:
        return self.alert == "red"


class RecallPolicy:
    """品牌级素材池的召回策略。"""

    def __init__(
        self,
        ledger: RecallLedger,
        config: RecallConfig | None = None,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self.ledger = ledger
        self.config = config or RecallConfig()
        self.rng = rng or random.Random()

    # ---------- 冷却 ----------
    def cooldown_remaining(self, asset: MediaAsset, *, now: datetime | None = None) -> timedelta:
        """返回剩余冷却时长；未在冷却期返回 timedelta(0)。"""
        last_used = self.ledger.last_used_at(asset.asset_id, brand=self.config.brand)
        if last_used is None:
            return timedelta(0)
        reference = now or datetime.now(timezone.utc)
        elapsed = reference - last_used
        cooldown = timedelta(days=self.config.cooldown_days)
        return cooldown - elapsed if elapsed < cooldown else timedelta(0)

    def is_cooling(self, asset: MediaAsset, *, now: datetime | None = None) -> bool:
        return self.cooldown_remaining(asset, now=now) > timedelta(0)

    # ---------- 权重 ----------
    def weight(self, candidate: MediaCandidate, *, now: datetime | None = None) -> float:
        """相似度 × 新图加权；既有图（老图）权重恒为相似度。"""
        asset = candidate.asset
        base = max(float(candidate.similarity), 0.0)
        if base == 0.0:
            return 0.0
        if asset.is_legacy or asset.added_at is None:
            return base
        reference = now or datetime.now(timezone.utc)
        age = reference - asset.added_at
        window = timedelta(days=self.config.freshness_window_days)
        if window <= timedelta(0) or age >= window or age < timedelta(0):
            return base
        return base * self.config.freshness_boost

    # ---------- 召回 ----------
    def recall(
        self,
        candidates: Iterable[MediaCandidate],
        *,
        top_k: int | None = None,
        now: datetime | None = None,
    ) -> list[RecallPick]:
        """过滤冷却中的素材后，做无放回加权随机采样。"""
        limit = top_k or self.config.top_k
        reference = now or datetime.now(timezone.utc)
        scored: list[tuple[float, MediaCandidate, float]] = []
        for candidate in candidates:
            if self.is_cooling(candidate.asset, now=reference):
                continue
            weight = self.weight(candidate, now=reference)
            if weight <= 0.0:
                continue
            # A-Res：权重越高，随机键越大
            key = self.rng.random() ** (1.0 / weight)
            scored.append((key, candidate, weight))
        scored.sort(key=lambda item: item[0], reverse=True)
        picks: list[RecallPick] = []
        for _key, candidate, weight in scored[:limit]:
            asset = candidate.asset
            picks.append(
                RecallPick(
                    asset_id=asset.asset_id,
                    file_name=asset.file_name,
                    similarity=float(candidate.similarity),
                    weight=round(weight, 4),
                    is_new=not asset.is_legacy,
                    category=asset.category,
                    summary=asset.summary,
                )
            )
        return picks

    # ---------- 容量 ----------
    def capacity(self, assets: Sequence[MediaAsset], *, now: datetime | None = None) -> CapacityReport:
        """统计图片可召回容量；低于阈值（112 张）时置红色预警。

        视频描述条目不占用"图片库容量"，单独计入 ``videos``。
        """
        reference = now or datetime.now(timezone.utc)
        cooling = 0
        fresh = 0
        legacy = 0
        videos = 0
        for asset in assets:
            if asset.is_video:
                videos += 1
                continue
            if self.is_cooling(asset, now=reference):
                cooling += 1
            if asset.is_legacy:
                legacy += 1
            else:
                fresh += 1
        total = len(assets) - videos
        available = total - cooling
        alert = "red" if available < self.config.capacity_red_threshold else "ok"
        return CapacityReport(
            brand=self.config.brand,
            total=total,
            available=available,
            cooling=cooling,
            new=fresh,
            legacy=legacy,
            threshold=self.config.capacity_red_threshold,
            alert=alert,
            videos=videos,
        )

    # ---------- 控制台用的轻量相关性 ----------
    @staticmethod
    def keyword_similarity(query: str, asset: MediaAsset) -> float:
        """控制台演示用关键词相似度（正式链路用向量检索）。"""
        terms = [term for term in query.lower().replace(",", " ").split() if term]
        if not terms:
            return 0.5
        haystack = f"{asset.file_name} {asset.summary} {' '.join(asset.keywords)} {asset.category}".lower()
        hits = sum(1 for term in terms if term in haystack)
        return round(min(1.0, 0.3 + 0.7 * hits / len(terms)), 4)
