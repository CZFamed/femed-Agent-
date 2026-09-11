"""媒体资产库域（素材入库 + 召回策略）。

所有者：root（2026-09-11 新增，见 AGENTS.md §2）。

业务决策（2026-09-11 确认）：

1. 冷却触发口径：**实际进入生成内容 / 发布**才算被召回；检索命中只记曝光。
2. 素材池按品牌隔离，当前品牌 = 沧州菲美得。
3. 可召回容量低于 112 张（一周用量）触发红色预警。
4. 既有图库全部视为老图，不论是否被使用过，不享受新图加权。
"""

from pulse.services.media.catalog import MediaAsset, asset_id_for, load_catalog
from pulse.services.media.categories import (
    Category,
    CategoryCatalog,
    load_categories,
    match_category,
    resolve_category,
)
from pulse.services.media.config import (
    BRAND_NAME,
    CAPACITY_RED_THRESHOLD,
    COOLDOWN_DAYS,
    RecallConfig,
)
from pulse.services.media.describe import (
    Description,
    VisionConfig,
    build_describer,
    find_unverified_claims,
    heuristic_describe,
    parse_capture_time,
    probe_png,
    probe_image,
    probe_vision,
    vision_config_from_env,
    vision_model_supports_images,
)
from pulse.services.media.ingest import MediaIngestor, UnsupportedMediaError
from pulse.services.media.ledger import RecallLedger
from pulse.services.media.policy import CapacityReport, RecallPick, RecallPolicy
from pulse.services.media.platforms import (
    PLATFORM_PROFILES,
    PlatformProfile,
    ShotSlot,
    platform_choices,
    platform_profile,
    slot_score,
)
from pulse.services.media.registry import MediaRegistry

__all__ = [
    "BRAND_NAME",
    "CAPACITY_RED_THRESHOLD",
    "COOLDOWN_DAYS",
    "CapacityReport",
    "Category",
    "CategoryCatalog",
    "Description",
    "MediaAsset",
    "MediaIngestor",
    "MediaRegistry",
    "PLATFORM_PROFILES",
    "PlatformProfile",
    "RecallConfig",
    "RecallLedger",
    "RecallPick",
    "RecallPolicy",
    "ShotSlot",
    "UnsupportedMediaError",
    "VisionConfig",
    "asset_id_for",
    "build_describer",
    "find_unverified_claims",
    "heuristic_describe",
    "load_catalog",
    "load_categories",
    "match_category",
    "parse_capture_time",
    "platform_choices",
    "platform_profile",
    "probe_png",
    "probe_image",
    "probe_vision",
    "resolve_category",
    "slot_score",
    "vision_config_from_env",
    "vision_model_supports_images",
]
