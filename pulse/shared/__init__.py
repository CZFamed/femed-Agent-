"""跨域共用类型（所有者：root）。

**本目录只读。** 业务域一律从这里导入类型，不得自行定义同名字段或枚举，
否则会造成接口漂移（见 docs/多Agent协同开发方案.md §7）。
"""

from pulse.shared.enums import (
    ComplianceSeverity,
    ContentType,
    ErrorClass,
    LicenseStatus,
    MediaKind,
    Platform,
    PublishJobStatus,
    ScheduleStatus,
    ScreeningResult,
    VariantStatus,
)
from pulse.shared.ids import (
    new_account_id,
    new_brief_id,
    new_job_id,
    new_schedule_id,
    new_source_id,
    new_unified_post_id,
    new_variant_id,
)
from pulse.shared.models import (
    CONTRACT_VERSION,
    Caption,
    ComplianceInfo,
    ContractError,
    MediaItem,
    PublishResult,
    SemiAutoBundle,
    UnifiedPost,
)

__all__ = [
    "CONTRACT_VERSION",
    # enums
    "ComplianceSeverity",
    "ContentType",
    "ErrorClass",
    "LicenseStatus",
    "MediaKind",
    "Platform",
    "PublishJobStatus",
    "ScheduleStatus",
    "ScreeningResult",
    "VariantStatus",
    # models
    "Caption",
    "ComplianceInfo",
    "ContractError",
    "MediaItem",
    "PublishResult",
    "SemiAutoBundle",
    "UnifiedPost",
    # ids
    "new_account_id",
    "new_brief_id",
    "new_job_id",
    "new_schedule_id",
    "new_source_id",
    "new_unified_post_id",
    "new_variant_id",
]
