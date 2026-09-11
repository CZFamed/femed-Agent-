"""分平台素材配置（所有者：root）。

依据：《菲美得_四平台推荐风格与方式报告_v1.md》(2026-09-11 第 2 版) §3 逐平台详解。

报告的核心结论是"四个平台不是四种排版，是四种说服顺序"——同一个卖点在不同平台
需要的**证据形态**不同，所以每个平台该配哪些画面、几张、什么顺序都不一样。
本模块把报告里那些"拍什么"的清单变成可执行的数据：每个平台一组**位次**
（``ShotSlot``），召回时按位次逐个挑图；某个位次挑不到就明确告诉用户"这格要补拍"。

两点说明：

1. **为什么按顶层品类匹配，而不是子类**：库里描述头部的 ``sub_process`` 与目录名
   并不总是一致（目录是 ``生产流程/发泡``，头部写的是 ``发泡（白模成型）``；
   ``人员`` 目录下还写着 ``会议与培训`` 等子类）。所以位次只锁定顶层品类，
   细分靠关键词——目录怎么调整都不会把配置打散。
2. **这里不是平台优先级的事实来源**。优先级仍以 ``pulse/contracts/INTERFACES.md``
   与 ``pulse/shared/enums.py`` 为准；本模块只描述"素材准备"口径，
   与发布侧契约的关系见 ``PlatformProfile.contract_note``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pulse.services.media.catalog import MediaAsset

#: 报告文件位置（仓库外，见 AGENTS.md §5.0）
REPORT_REFERENCE = "菲美得产品图片/菲美得_四平台推荐风格与方式报告_v1.md"

#: 位次候选的"同档"阈值：只保留得分不低于最高分 95% 的素材。
#: 位次是"这一格要什么画面"，必须挑得准；档内再随机，保证不老是同一张。
SLOT_TIER_RATIO = 0.95


@dataclass(frozen=True)
class ShotSlot:
    """一个拍摄位次：这个平台的第 N 张图需要什么画面。"""

    order: int
    role: str
    processes: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    media_kind: str = ""  # 空=不限；"video" 优先视频；"image" 优先图片
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.order}. {self.role}"


@dataclass(frozen=True)
class PlatformProfile:
    """一个平台的素材口径。"""

    key: str
    name: str
    priority: str
    status: str
    aspect: str
    shot_count: str
    form: str
    caption_length: str
    slots: tuple[ShotSlot, ...] = field(default=())
    contract_note: str = ""

    @property
    def slot_count(self) -> int:
        return len(self.slots)

    def as_payload(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "priority": self.priority,
            "status": self.status,
            "aspect": self.aspect,
            "shot_count": self.shot_count,
            "form": self.form,
            "caption_length": self.caption_length,
            "slot_count": self.slot_count,
            "contract_note": self.contract_note,
        }


#: 四个平台的素材口径（顺序即优先级顺序）
PLATFORM_PROFILES: tuple[PlatformProfile, ...] = (
    PlatformProfile(
        key="linkedin",
        name="LinkedIn",
        priority="P0-A",
        status="正式执行",
        aspect="4:5",
        shot_count="3–5 张",
        form="多图帖（3–5 张）+ 技术短文",
        caption_length="600–1,200 字符",
        slots=(
            ShotSlot(
                order=1,
                role="能力证明：精加工成品与数控机床同框（最关键的一张）",
                processes=("加工件",),
                keywords=("加工面", "导轨面", "机床床身", "机加工", "加工中心", "镗床", "成品"),
                media_kind="image",
                note="报告 §3.1：直接证明铸件与粗加工同厂",
            ),
            ShotSlot(
                order=2,
                role="工艺能力：大型数控镗铣床正在加工厚壁箱体",
                processes=("加工件", "生产流程"),
                keywords=("加工中心", "镗床", "数控", "箱体", "齿轮箱体", "机加工", "导轨"),
                media_kind="image",
                note="报告 §3.1：大型数控镗铣床加工厚壁箱体",
            ),
            ShotSlot(
                order=3,
                role="质量能力：三维扫描 / 三坐标检测",
                processes=("铸件", "人员"),
                keywords=("扫描", "三维扫描", "尺寸检测", "点云", "偏差", "检测", "逆向"),
                media_kind="image",
                note="报告 §3.1：三维扫描与三坐标是质量能力的核心证据",
            ),
            ShotSlot(
                order=4,
                role="交付能力：上托 / 涂装 / 待发成品",
                processes=("铸件", "生产流程", "厂区_场景"),
                keywords=("托盘", "包装", "待发", "涂装", "喷漆", "标识", "堆放"),
                media_kind="image",
                note="报告 §3.1：缠膜上托、待发成品，证明交付能力",
            ),
        ),
    ),
    PlatformProfile(
        key="facebook",
        name="Facebook",
        priority="P1",
        status="正式执行",
        aspect="4:5（视频 9:16）",
        shot_count="3–5 张 或 1 段原生视频",
        form="原生上传短视频（首选）/ 图集（备用）",
        caption_length="150–400 字符",
        slots=(
            ShotSlot(
                order=1,
                role="浇铸现场：浇铸区全景 / 浇注后冷却（原生视频首选）",
                processes=("厂区_场景", "生产流程"),
                keywords=("浇铸", "浇注", "冷却", "车间全景", "消失模", "发泡"),
                media_kind="video",
                note="报告 §3.2：直接上传原始片段，不剪不配乐",
            ),
            ShotSlot(
                order=2,
                role="工艺过程：白模 / 发泡成型",
                processes=("生产流程",),
                keywords=("白模", "泡沫模样", "发泡", "模具箱", "模样", "EPS"),
            ),
            ShotSlot(
                order=3,
                role="加工作业：数控机床正在加工",
                processes=("加工件", "生产流程"),
                keywords=("机加工", "加工中心", "镗床", "数控", "加工面"),
            ),
            ShotSlot(
                order=4,
                role="成品与车间现场",
                processes=("铸件", "厂区_场景"),
                keywords=("灰铁铸件", "车间", "批量", "铸件", "堆放", "涂装"),
            ),
        ),
    ),
    PlatformProfile(
        key="tiktok",
        name="TikTok",
        priority="P2",
        status="暂不投入（不建发布链路）",
        aspect="9:16 竖屏",
        shot_count="5–6 张",
        form="图片轮播（Photo Mode）",
        caption_length="≤150 字符",
        contract_note="契约里 TikTok 为暂不投入：此处只备素材，不建发布链路。",
        slots=(
            ShotSlot(
                order=1,
                role="浇铸",
                processes=("厂区_场景", "生产流程"),
                keywords=("浇铸", "浇注", "冷却"),
            ),
            ShotSlot(
                order=2,
                role="白模",
                processes=("生产流程",),
                keywords=("白模", "泡沫模样", "模样", "EPS", "模具箱"),
            ),
            ShotSlot(
                order=3,
                role="喷丸 / 清理后的铸件",
                processes=("铸件",),
                keywords=("喷砂", "表面喷砂", "毛坯", "灰铁铸件", "铸件"),
            ),
            ShotSlot(
                order=4,
                role="加工",
                processes=("加工件", "生产流程"),
                keywords=("加工面", "导轨面", "机加工", "加工中心", "镗床"),
            ),
            ShotSlot(
                order=5,
                role="检测",
                processes=("铸件", "人员"),
                keywords=("扫描", "检测", "尺寸", "点云", "偏差"),
            ),
            ShotSlot(
                order=6,
                role="待发",
                processes=("铸件", "生产流程"),
                keywords=("涂装", "喷漆", "托盘", "标识", "包装"),
            ),
        ),
    ),
    PlatformProfile(
        key="vk",
        name="VK",
        priority="P1",
        status="报告口径为正式执行（法务已通过）",
        aspect="4:5 或 1:1",
        shot_count="3–5 张相册图",
        form="图文长帖 + 相册（复用 LinkedIn 同一批直拍图）",
        caption_length="500–1,500 字符（俄语）",
        contract_note=(
            "⚠ 契约冲突：报告 §8.1 称 VK 法务评审已通过、转正式运营，"
            "但 AGENTS.md §3.4 与 pulse/shared/enums.py 仍把 VK 列为冻结、不在 Platform 枚举内。"
            "本模块只备素材，未改契约；如需转正须单独做契约变更。"
        ),
        slots=(
            ShotSlot(
                order=1,
                role="企业形象：厂区 / 厂房外景",
                processes=("厂区_场景",),
                keywords=("厂区外景", "车间", "钢结构", "绿色地坪", "消失模", "厂房"),
                media_kind="image",
            ),
            ShotSlot(
                order=2,
                role="生产工艺：白模 / 发泡 / 涂装",
                processes=("生产流程",),
                keywords=("白模", "发泡", "涂装", "消失模涂料", "烘干"),
                media_kind="image",
            ),
            ShotSlot(
                order=3,
                role="产品：阀体 / 箱体 / 配重 / 管件",
                processes=("铸件",),
                keywords=("灰铁铸件", "阀体", "齿轮箱体", "配重块", "三通", "管件", "床身"),
                media_kind="image",
            ),
            ShotSlot(
                order=4,
                role="设备与检测",
                processes=("加工件", "铸件"),
                keywords=("加工中心", "镗床", "扫描", "检测", "尺寸"),
                media_kind="image",
            ),
        ),
    ),
)

#: 平台 key → 配置
PROFILES_BY_KEY: dict[str, PlatformProfile] = {item.key: item for item in PLATFORM_PROFILES}


def platform_profile(key: str | None) -> PlatformProfile | None:
    """按 key 取平台配置；未知或空返回 None（调用方退回通用召回）。"""
    return PROFILES_BY_KEY.get((key or "").strip().lower())


def platform_choices() -> list[dict[str, object]]:
    """给控制台下拉框用的平台清单。"""
    return [item.as_payload() for item in PLATFORM_PROFILES]


def _text_of(asset: MediaAsset) -> str:
    """正文类文本：文件名 + 摘要 + 品类。"""
    return " ".join([asset.file_name, asset.summary, asset.category, asset.process])


def slot_score(slot: ShotSlot, asset: MediaAsset, *, extra_terms: tuple[str, ...] = ()) -> float:
    """算一条素材与某个位次的匹配度；返回 0 表示这个位次不该用它。

    返回 0 是有意义的：调用方据此判断"这一格真的没有素材、需要补拍"，
    而不是随便塞一张凑数。
    """
    if slot.processes and asset.process not in slot.processes:
        return 0.0
    terms = slot.keywords + tuple(extra_terms)
    if not terms:
        return 0.5
    # 命中的位置决定强度：落在关键词字段上说明这条素材**本身就是**这个题材；
    # 只是出现在摘要段落里，可能只是顺带提了一句，权重低得多。
    keyword_text = " ".join(asset.keywords)
    text = _text_of(asset)
    keyword_hits = [term for term in terms if term and term in keyword_text]
    text_hits = [term for term in terms if term and term not in keyword_hits and term in text]
    if not keyword_hits and not text_hits:
        return 0.0
    # 命中 3 个关键词即视为强匹配
    strength = len(keyword_hits) + 0.4 * len(text_hits)
    score = 0.5 + 0.5 * min(1.0, strength / 3.0)
    return round(min(score, 1.0), 4)


def prefer_media_kind(
    slot: ShotSlot, candidates: list[tuple[float, MediaAsset]]
) -> list[tuple[float, MediaAsset]]:
    """位次指定了形态时，优先只留那个形态——留不下才退回全部。

    报告的"首选形态"是硬要求（Facebook 首选原生视频、LinkedIn/VK 是图集），
    靠权重加成压不住随机性，所以这里做硬筛选；一个都没有时才退回，
    避免整格空掉。
    """
    if not slot.media_kind:
        return candidates
    want_video = slot.media_kind == "video"
    preferred = [item for item in candidates if item[1].is_video is want_video]
    return preferred or candidates


def top_tier(
    candidates: list[tuple[float, MediaAsset]], *, ratio: float = SLOT_TIER_RATIO
) -> list[tuple[float, MediaAsset]]:
    """只保留得分最高那一档，避免"位次要求明确"的格子被随机性挑歪。"""
    if not candidates:
        return candidates
    best = max(score for score, _asset in candidates)
    if best <= 0:
        return candidates
    return [item for item in candidates if item[0] >= best * ratio]
