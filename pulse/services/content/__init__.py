"""内容生产域（A1）：brief → 文案 → 素材选用 → variant 派生。

入口与出口（契约 §9）：

* 入口：``contents`` / ``briefs``（本域 ``ContentService.submit_brief``）
* 出口：``variants`` + ``media_assets`` 写入（``DerivedVariant`` / ``ContentStore``）
* 桩：生成链路默认走 ``StubCopywriter``（固定文案，不调真实模型）

本域只依赖 ``pulse/shared``（冻结契约类型）与 ``pulse/services/media``（素材召回），
不反向依赖调度、发布、接口层。
"""

from pulse.services.content.brief import (
    DEFAULT_TOKEN_BUDGET,
    Brief,
    parse_brief,
)
from pulse.services.content.errors import (
    BriefValidationError,
    ContentError,
    ContentNotFoundError,
    MediaSelectionError,
    TokenBudgetExceeded,
    UnknownHashtagError,
    UnknownPlatformError,
    UnknownTaskError,
)
from pulse.services.content.generator import (
    TASK_GENERATE_VARIANT,
    ContentService,
    CopyDraft,
    CopyRequest,
    Copywriter,
    StubCopywriter,
)
from pulse.services.content.hashtags import (
    ALLOWED_TAGS,
    CATEGORY_TAGS,
    REGION_TAGS,
    compose_hashtags,
    validate_hashtags,
)
from pulse.services.content.mediaselect import (
    NEED_REAL_DATA,
    MediaSelection,
    SlotSelection,
    has_slot_profile,
    select_media,
)
from pulse.services.content.prompts import (
    PROMPT_TEMPLATES,
    PromptTemplate,
    missing_options,
    prompt_template,
    render_prompt,
    supported_platforms,
    template_as_dict,
)
from pulse.services.content.router import (
    TASK_LONG_FORM,
    TASK_REWRITE,
    TASK_SELF_REVIEW,
    ModelRoute,
    ModelRouter,
    TokenBudget,
)
from pulse.services.content.store import (
    ContentRecord,
    ContentStore,
    MediaAssetRecord,
    VariantRecord,
)
from pulse.services.content.variants import (
    AccountBinding,
    DerivedVariant,
    VariantDraft,
    build_unified_post,
    derive_variants,
    media_items_from_picks,
)

__all__ = [
    "ALLOWED_TAGS",
    "AccountBinding",
    "Brief",
    "BriefValidationError",
    "CATEGORY_TAGS",
    "ContentError",
    "ContentNotFoundError",
    "ContentRecord",
    "ContentService",
    "ContentStore",
    "CopyDraft",
    "CopyRequest",
    "Copywriter",
    "DEFAULT_TOKEN_BUDGET",
    "DerivedVariant",
    "MediaAssetRecord",
    "MediaSelection",
    "MediaSelectionError",
    "ModelRoute",
    "ModelRouter",
    "NEED_REAL_DATA",
    "PROMPT_TEMPLATES",
    "PromptTemplate",
    "REGION_TAGS",
    "SlotSelection",
    "StubCopywriter",
    "TASK_GENERATE_VARIANT",
    "TASK_LONG_FORM",
    "TASK_REWRITE",
    "TASK_SELF_REVIEW",
    "TokenBudget",
    "TokenBudgetExceeded",
    "UnknownHashtagError",
    "UnknownPlatformError",
    "UnknownTaskError",
    "VariantDraft",
    "VariantRecord",
    "build_unified_post",
    "compose_hashtags",
    "derive_variants",
    "has_slot_profile",
    "media_items_from_picks",
    "missing_options",
    "parse_brief",
    "prompt_template",
    "render_prompt",
    "select_media",
    "supported_platforms",
    "template_as_dict",
    "validate_hashtags",
]
