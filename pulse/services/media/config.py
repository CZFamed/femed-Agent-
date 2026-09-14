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

#: 入库许可：必须先完成视觉识别（生成 source=vision 的描述）才允许入库。
#: 视觉模型不可用时会让控制台无法入库——这是刻意的取舍：宁可挡住，也不让
#: 未经识别的素材混进 RAG。应急可用 PULSE_MEDIA_REQUIRE_VISION=0 临时关闭。
REQUIRE_VISION_BEFORE_INGEST = True

#: 视觉识别凭据（入库许可令牌）的有效期（秒）
VISION_TICKET_TTL_SECONDS = 3600

#: 单次召回的默认返回条数
DEFAULT_TOP_K = 3

#: 入库文件白名单与大小上限
#:
#: ``.psd`` / ``.psb``（Photoshop 文档）也在白名单里：设计稿同样要能进库、能识别、能召回。
#: 它们不是栅格格式，入库后用 ``pulse.services.media.psd`` 解出合并图再送模型 /
#: 预览 / 导出（见媒体库契约 §2.5.3）。``.psb`` 是 Photoshop 的大文档格式。
ALLOWED_IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".psd", ".psb"}
)
MAX_IMAGE_BYTES = 20 * 1024 * 1024

#: PSD / PSB 的体积上限。设计稿比实拍图大得多（多层 + 无损通道），单独给额度。
MAX_PSD_BYTES = 200 * 1024 * 1024

#: 视频白名单与大小上限（实拍视频，模型走抽帧识别）
ALLOWED_VIDEO_SUFFIXES: frozenset[str] = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm"})
MAX_VIDEO_BYTES = 200 * 1024 * 1024

#: 视频抽帧参数（token 实测见 video.py 模块说明）
VIDEO_FRAME_COUNT = 4
VIDEO_FRAME_WIDTH = 768


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
