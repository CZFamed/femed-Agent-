"""合规与治理域（A4）：结构化判定、豁免留痕、制裁与出口管制筛查。

入口与出口（契约 §9）：

* 入口：`variant_id`（消费 `pulse.compliance.check`，载荷只有 ID）
* 出口：`compliance_findings` 结构化发现项 + 是否 `block`
* 桩：词库与清单先用最小集 + 本地静态数据（真实名单标注 `TODO(need-real-data)`）

设计口径（派工单 §4）：**生成者不能判定自己合规**。本域只吃 A1 产物的公开字段
（`VariantView`），不 import 内容域内部实现。
"""

from pulse.services.compliance.errors import (
    ComplianceError,
    FindingNotFoundError,
    RuleConfigError,
    WaiverNotAllowedError,
)
from pulse.services.compliance.lexicon import (
    ContentPolicies,
    PlatformPolicy,
    SensitiveLexicon,
    SensitiveTerm,
    clear_caches,
    content_policies,
    load_json,
    sensitive_lexicon,
)
from pulse.services.compliance.models import (
    Finding,
    MediaView,
    VariantView,
    covered_fields,
    position,
)
from pulse.services.compliance.rules import (
    RULES,
    Rule,
    RuleEngine,
    rule_banned_phrases,
    rule_hashtag_shape,
    rule_machine_flavor,
    rule_media_license,
    rule_platform_quality,
    rule_sensitive_words,
    rule_unverified_specs,
)
from pulse.services.compliance.sanctions import (
    DISCLAIMER,
    SanctionsList,
    ScreeningRecord,
    load_lists,
    normalize_subject,
    screen,
    screen_many,
)
from pulse.services.compliance.service import (
    INDIVIDUAL_REVIEW_TRIGGERS,
    ComplianceOutcome,
    ComplianceService,
)

__all__ = [
    "DISCLAIMER",
    "INDIVIDUAL_REVIEW_TRIGGERS",
    "RULES",
    "ComplianceError",
    "ComplianceOutcome",
    "ComplianceService",
    "ContentPolicies",
    "Finding",
    "FindingNotFoundError",
    "MediaView",
    "PlatformPolicy",
    "Rule",
    "RuleConfigError",
    "RuleEngine",
    "SanctionsList",
    "ScreeningRecord",
    "SensitiveLexicon",
    "SensitiveTerm",
    "VariantView",
    "WaiverNotAllowedError",
    "clear_caches",
    "content_policies",
    "covered_fields",
    "load_json",
    "load_lists",
    "normalize_subject",
    "position",
    "rule_banned_phrases",
    "rule_hashtag_shape",
    "rule_machine_flavor",
    "rule_media_license",
    "rule_platform_quality",
    "rule_sensitive_words",
    "rule_unverified_specs",
    "screen",
    "screen_many",
    "sensitive_lexicon",
]
