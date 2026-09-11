"""媒体资产库配置（所有者：root）。

所有阈值都是业务决策，不允许在各调用点硬编码。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 当前品牌（素材池按品牌隔离）
BRAND_NAME = "沧州菲美得"

#: 同一张图进入生成内容 / 发布后，多少天内不得再次被召回
COOLDOWN_DAYS = 15

#: 可召回容量低于该值时发布红色预警（112 张 ≈ 一周用量）
CAPACITY_RED_THRESHOLD = 112

#: 新图加权窗口（入库后多少天内享受新鲜度加权）
FRESHNESS_WINDOW_DAYS = 7

#: 新图权重倍数（老图权重为 1.0）
FRESHNESS_BOOST = 2.0

#: 单次召回的默认返回条数
DEFAULT_TOP_K = 3

#: 入库文件白名单与大小上限
ALLOWED_IMAGE_SUFFIXES: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})
MAX_IMAGE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class RecallConfig:
    """召回策略参数（品牌级素材池）。"""

    brand: str = BRAND_NAME
    cooldown_days: int = COOLDOWN_DAYS
    capacity_red_threshold: int = CAPACITY_RED_THRESHOLD
    freshness_window_days: int = FRESHNESS_WINDOW_DAYS
    freshness_boost: float = FRESHNESS_BOOST
    top_k: int = DEFAULT_TOP_K

    def __post_init__(self) -> None:
        if not self.brand:
            raise ValueError("brand 不能为空：素材池必须绑定品牌")
        if self.cooldown_days <= 0:
            raise ValueError("cooldown_days 必须为正整数")
        if self.capacity_red_threshold <= 0:
            raise ValueError("capacity_red_threshold 必须为正整数")
        if self.freshness_window_days < 0:
            raise ValueError("freshness_window_days 不能为负")
        if self.freshness_boost < 1.0:
            raise ValueError("freshness_boost 不能小于 1.0")
        if self.top_k <= 0:
            raise ValueError("top_k 必须为正整数")
