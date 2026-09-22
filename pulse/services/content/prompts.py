"""平台 Prompt 模板（所有者：A1，任务包 W1-A1-3）。

模板**外置为数据**（``PROMPT_TEMPLATES``），不在函数里拼硬编码字符串——
理由是可回归：换模型、换提示词时必须能做生成质量回归（需求 §3.4 测试要求），
模板一旦散落在代码里就没人敢改。

约束来源：

* 文案结构、长度、标签数、emoji 上限：《菲美得_四平台推荐风格与方式报告_v1》§2.2/§3，
  与 ``pulse/services/media/captions.py`` 的 ``CAPTION_SPECS`` 同源；
  本域另有一条测试专门比对两者，防止各写一套后漂移。
* YouTube 的字数与配额：平台硬限制（title ≤100 字符、description ≤5000 字符、
  tags 总计 ≤500 字符），以及"上传 1 次约 1600 单位、日配额约 10000"的配额口径
  （见派工单 §4 与需求拆解 §4 的平台接入约束表）。

平台优先级：**P0-A = LinkedIn + YouTube**，这两个模板是 M1 的必须项；
P1（Facebook / VK / Reddit）与 P2（Instagram）一并定义，便于后续直接启用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from pulse.services.content.brief import Brief
from pulse.services.content.errors import UnknownPlatformError
from pulse.shared.enums import Platform

#: YouTube 平台硬限制（外部事实，不是业务参数）
YOUTUBE_TITLE_MAX = 100
YOUTUBE_DESCRIPTION_MAX = 5000
YOUTUBE_TAGS_MAX_CHARS = 500


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """一个平台的内容模板规格。"""

    platform: str
    name: str
    priority: str
    #: 内容形态（用于选择 content_type 与素材形态）
    content_type: str
    #: 段落结构，渲染时逐条列出，模型按顺序写
    structure: tuple[str, ...]
    min_chars: int | None = None
    max_chars: int | None = None
    hashtag_min: int = 0
    hashtag_max: int = 5
    emoji_max: int = 0
    link_rule: str = "正文不放链接"
    #: 平台特有的额外要求（会被逐条写进 prompt）
    rules: tuple[str, ...] = field(default=())
    #: 平台必填的 options（便于生成方一次性把缺项报全）
    required_options: tuple[str, ...] = field(default=())


PROMPT_TEMPLATES: dict[str, PromptTemplate] = {
    "linkedin": PromptTemplate(
        platform="linkedin",
        name="LinkedIn",
        priority="P0-A",
        content_type="image",
        structure=("钩子行（对方的痛点或利益）", "痛点展开", "能力证据（分点：铸造/加工/检测/包装）", "产品范围", "CTA（提问收尾）", "标签"),
        min_chars=600,
        max_chars=1200,
        hashtag_min=3,
        hashtag_max=5,
        emoji_max=2,
        link_rule="正文不放链接，链接放第一条评论",
        rules=(
            "第一行必须是对方的利益或痛点，绝不能是自我介绍",
            "每段 1–2 行，段间空行，不要写成一整块",
            "工程语言、参数导向、克制；不使用无法证实的形容词",
            "结尾用提问收尾，带起评论",
        ),
        required_options=("author_urn", "linkedin_visibility"),
    ),
    "youtube": PromptTemplate(
        platform="youtube",
        name="YouTube",
        priority="P0-A",
        content_type="video",
        # 注意：这里的段落结构是**本项目自己定的**，不是报告原文——
        # 《四平台推荐风格与方式报告》只定义了 LinkedIn / Facebook / TikTok / VK 四个平台，
        # YouTube 虽然在 AGENTS.md 里是 P0-A，却没有对应的风格依据。
        # 外部硬限制（title ≤100、description ≤5000、tags ≤500）是平台事实，可放心用；
        # 结构部分等业务方确认后再固化，当前视为可评审的默认方案。
        structure=("标题（说清做什么件、什么能力）", "描述第一段（3 行内说明是什么画面、哪家厂）", "能力要点（分点）", "工艺与检测说明", "联系与合规声明", "标签"),
        min_chars=300,
        max_chars=YOUTUBE_DESCRIPTION_MAX,
        hashtag_min=3,
        hashtag_max=5,
        emoji_max=0,
        link_rule="描述正文可以放链接（YouTube 不降低外链权重）",
        rules=(
            f"title 不超过 {YOUTUBE_TITLE_MAX} 字符，且必须提供",
            "视频必须是工厂实拍，不允许用图片拼成「伪视频」",
            f"tags 合计不超过 {YOUTUBE_TAGS_MAX_CHARS} 字符",
            "不上传含客户品牌标识的画面（脱敏见 AGENTS.md 铁律）",
        ),
        required_options=("privacy_status", "category_id", "made_for_kids"),
    ),
    "facebook": PromptTemplate(
        platform="facebook",
        name="Facebook",
        priority="P1",
        content_type="image",
        structure=("场景描述", "一句结论", "提问收尾", "标签"),
        min_chars=150,
        max_chars=400,
        hashtag_min=1,
        hashtag_max=2,
        emoji_max=4,
        link_rule="正文不放链接，链接放评论",
        rules=("比 LinkedIn 更口语、更有画面感，但仍克制", "前 2 行决定是否被展开"),
        required_options=("page_id",),
    ),
    "vk": PromptTemplate(
        platform="vk",
        name="VK",
        priority="P1",
        content_type="image",
        structure=("企业介绍", "工艺", "产品", "设备", "合作方式"),
        min_chars=500,
        max_chars=1500,
        hashtag_min=2,
        hashtag_max=5,
        emoji_max=2,
        link_rule="外链可以直接放正文",
        rules=(
            "对外文案用俄语，同时保留英语母版供复用与审校",
            "语气比 LinkedIn 更正式、更完整",
        ),
        required_options=("owner_id",),
    ),
    "reddit": PromptTemplate(
        platform="reddit",
        name="Reddit",
        priority="P1",
        content_type="image",
        structure=("标题（事实，不做标题党）", "背景与数据", "我方能力（克制，不硬推销）", "讨论式提问"),
        min_chars=200,
        max_chars=2000,
        hashtag_min=0,
        hashtag_max=0,
        emoji_max=0,
        link_rule="外链仅在被问到或版规允许时放",
        rules=("子版块必须先由人工确认（subreddit 必填）", "不做自我推广刷屏，遵守 1:10 惯例"),
        required_options=("subreddit",),
    ),
    "instagram": PromptTemplate(
        platform="instagram",
        name="Instagram",
        priority="P2",
        content_type="image",
        structure=("一句画面说明", "能力要点", "标签"),
        min_chars=80,
        max_chars=400,
        hashtag_min=3,
        hashtag_max=8,
        emoji_max=3,
        link_rule="正文不放链接（链接只能在主页简介）",
        rules=("仅作品牌存在感，不指望直接获客",),
    ),
}


def prompt_template(platform: str | Platform) -> PromptTemplate:
    """取平台模板；平台不在白名单时抛 ``UnknownPlatformError``。"""
    key = platform.value if isinstance(platform, Platform) else str(platform).strip().lower()
    template = PROMPT_TEMPLATES.get(key)
    if template is None:
        raise UnknownPlatformError(
            f"没有 {platform!r} 的 Prompt 模板（可用：{'、'.join(sorted(PROMPT_TEMPLATES))}）",
            details={"available": sorted(PROMPT_TEMPLATES)},
        )
    return template


def supported_platforms() -> tuple[str, ...]:
    """模板覆盖的平台（按优先级排序：P0-A 优先，其次 P1、P2）。"""
    order = {"P0-A": 0, "P1": 1, "P2": 2}
    return tuple(
        item.platform
        for item in sorted(PROMPT_TEMPLATES.values(), key=lambda t: (order.get(t.priority, 9), t.platform))
    )


def render_prompt(
    platform: str | Platform,
    brief: Brief,
    *,
    media_brief: str = "",
    evidence_gaps: tuple[str, ...] = (),
) -> str:
    """把模板 + brief + 素材清单渲染成最终提示词。

    ``evidence_gaps`` 会作为**硬约束**写进提示词：缺口处只能留
    ``TODO(need-real-data)``，不允许模型自己补数字（AGENTS.md 铁律 7）。
    """
    template = prompt_template(platform)
    lines: list[str] = [
        f"你是沧州菲美得（工业铸件与粗加工一体化，河北沧州）的海外社媒文案，现在写 {template.name} 的内容。",
        "",
        "【本次 brief】",
        f"主题：{brief.topic}",
        f"目标读者：{brief.target_audience}",
    ]
    if brief.cta:
        lines.append(f"期望行动：{brief.cta}")
    if brief.extra_terms:
        lines.append("必须覆盖的要点：" + "、".join(brief.extra_terms))
    lines += [
        "",
        "【结构（严格按顺序）】",
        *(f"{index}. {item}" for index, item in enumerate(template.structure, start=1)),
    ]
    if template.min_chars is not None and template.max_chars is not None:
        lines.append(f"【长度】正文 {template.min_chars}–{template.max_chars} 字符")
    elif template.max_chars is not None:
        lines.append(f"【长度】正文不超过 {template.max_chars} 字符")
    lines += [
        f"【标签】{template.hashtag_min}–{template.hashtag_max} 个，已含 #，不含空格",
        f"【emoji】最多 {template.emoji_max} 个",
        f"【外链】{template.link_rule}",
        "",
        "【平台要求】",
        *(f"- {rule}" for rule in template.rules),
        "",
        "【只能使用给定素材里可见的事实】",
        "严禁编造材质牌号、公差、单重、月产能、检测结果、认证、客户名称、出口国家。",
        "严禁使用 world-class / leading / No.1 / best price 这类无法证实的形容词，",
        "严禁任何承诺性表述（零封号 / 保证通过 / 保证交期）。",
    ]
    if media_brief:
        lines += ["", "【本次选用的素材（唯一可用事实来源）】", media_brief]
    if evidence_gaps:
        lines += [
            "",
            "【证据缺口——不得编造】",
            *(f"- {gap}" for gap in evidence_gaps),
            "缺口处只能写 TODO(need-real-data) 占位，不得用文字描述替代真实证据。",
        ]
    lines += ["", "只输出 JSON，字段：text（正文）、text_zh（中文对照）、hashtags（数组）"]
    if template.content_type == "video":
        lines.append("、title（标题）")
    lines.append("。")
    return "\n".join(lines)


def template_as_dict(platform: str | Platform) -> dict[str, Any]:
    """模板的纯数据视图（供 A5 控制台展示，不暴露任何实现细节）。"""
    template = prompt_template(platform)
    return {
        "platform": template.platform,
        "name": template.name,
        "priority": template.priority,
        "content_type": template.content_type,
        "structure": list(template.structure),
        "min_chars": template.min_chars,
        "max_chars": template.max_chars,
        "hashtag_min": template.hashtag_min,
        "hashtag_max": template.hashtag_max,
        "emoji_max": template.emoji_max,
        "link_rule": template.link_rule,
        "rules": list(template.rules),
        "required_options": list(template.required_options),
    }


def missing_options(platform: str | Platform, options: Mapping[str, Any]) -> list[str]:
    """返回该平台缺失的必填 options（供生成前一次性报全）。"""
    template = prompt_template(platform)
    return [key for key in template.required_options if key not in (options or {})]
