"""契约枚举（冻结，v1.0）。

与 contracts/INTERFACES.md §3 一一对应。**禁止新增/重命名成员**，
需要变更请走契约变更流程（消息 root）。
"""

from enum import StrEnum


class Platform(StrEnum):
    """平台白名单。

    优先级（见 AGENTS.md §3.4）：
      P0-A: LINKEDIN, YOUTUBE
      P1:   REDDIT, FACEBOOK
      P2:   INSTAGRAM
      冻结: VK（合规红线，不实现）
      暂不投入: TIKTOK（仅保留半自动导出能力）
    """

    LINKEDIN = "linkedin"
    YOUTUBE = "youtube"
    REDDIT = "reddit"
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"


class ContentType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"


class MediaKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class LicenseStatus(StrEnum):
    """素材授权状态。只允许自有或已授权 —— 见 AGENTS.md §3.5。"""

    OWNED = "owned"
    LICENSED = "licensed"
    PENDING = "pending"


class VariantStatus(StrEnum):
    """draft → pending_review → approved | rejected | needs_revision"""

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"


class ScheduleStatus(StrEnum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublishJobStatus(StrEnum):
    """含异步回执终态。

    **关键约束**：平台返回"已受理"只能进 PENDING_FINALIZE，
    绝不能直接置 PUBLISHED（契约 §3.3）。
    """

    QUEUED = "queued"
    DISPATCHING = "dispatching"
    PUBLISHING = "publishing"
    PENDING_FINALIZE = "pending_finalize"
    PUBLISHED = "published"
    FAILED = "failed"
    RETRYING = "retrying"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


#: 终态集合：用于判断任务是否还需要继续收敛
TERMINAL_JOB_STATUSES: frozenset[PublishJobStatus] = frozenset(
    {
        PublishJobStatus.PUBLISHED,
        PublishJobStatus.FAILED,
        PublishJobStatus.REJECTED,
        PublishJobStatus.CANCELLED,
    }
)


class ErrorClass(StrEnum):
    """错误分级 → 决定是否重试（契约 §3.4）。"""

    RATE_LIMITED = "rate_limited"      # 429 / quota，指数退避后重试
    TRANSIENT = "transient"            # 5xx / 网络，有限次重试
    AUTH_EXPIRED = "auth_expired"      # 401 / token 失效，刷新后重试
    MEDIA_PROCESSING = "media_processing"  # 平台异步处理中，转 pending_finalize
    POLICY_REJECTED = "policy_rejected"    # 政策拒绝，**不重试**
    VALIDATION_ERROR = "validation_error"  # payload 不合法，**不重试**


#: 可自动重试的错误类别
RETRYABLE_ERRORS: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.RATE_LIMITED,
        ErrorClass.TRANSIENT,
        ErrorClass.AUTH_EXPIRED,
    }
)

#: 不可重试（终态或需人工）
NON_RETRYABLE_ERRORS: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.POLICY_REJECTED,
        ErrorClass.VALIDATION_ERROR,
    }
)


class ComplianceSeverity(StrEnum):
    """合规发现项级别。

    BLOCK: 硬拦截，默认不可绕过，豁免需管理员权限。
    WARN:  可人工豁免，**必须留痕**。
    """

    BLOCK = "block"
    WARN = "warn"


class ScreeningResult(StrEnum):
    """制裁与出口管制筛查结果（本项目新增，原需求文档缺失）。"""

    CLEAR = "clear"
    HIT = "hit"
    REVIEW_REQUIRED = "review_required"
