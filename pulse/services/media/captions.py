"""按平台生成短文（所有者：root）。

依据：《菲美得_四平台推荐风格与方式报告_v1.md》(2026-09-11 第 2 版)
§2.1 文案结构 / §2.2 平台机制 / §3 逐平台风格规范 / §8.3 禁用语。

报告的核心结论是"四个平台不是四种排版，是四种说服顺序"——同一条素材，
LinkedIn 要按"钩子→痛点→能力证据→产品→CTA→标签"写 600–1,200 字符，
TikTok 只要"钩子→一句结论→标签"且不超过 150 字符。本模块把那些约束
固化成 ``CaptionSpec``，让模型在规格内写，再**逐条校验**并把结果回显给用户。

铁律（AGENTS.md §3.7 不伪造数据）：模型只能使用给定素材描述里的可见事实。
材质牌号、公差、单重、月产能、检测结果、认证、客户名称一律不得编造；
模型若输出了这类表述，会被 ``find_unverified_claims`` 与禁用语表标出。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from pulse.services.media.describe import (
    SYSTEM_PROMPT,
    VisionConfig,
    _loads_json_object,
    check_response_complete,
    find_unverified_claims,
    post_json,
)
from pulse.services.media.platforms import PlatformProfile, platform_profile

#: 报告 §8.3 禁用语：无法证实或构成承诺的表述
BANNED_PHRASES: tuple[str, ...] = (
    "world-class",
    "world class",
    "leading manufacturer",
    "no.1",
    "no. 1",
    "best price",
    "zero risk",
    "零封号",
    "保证通过",
    "保证交期",
    "guaranteed",
)

_URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)

#: 短文比"描述"长得多（LinkedIn 600–1,200 字符、VK 500–1,500 字符且要双语），
#: 且模型会先"思考"吃掉大量输出预算，沿用描述那档 2,000 token 会被截断。
CAPTION_MAX_TOKENS = 8000

#: 写短文不需要深度推理。实测 thinking 段会把 4,000 token 预算整个吃光、
#: 正文一个字都出不来；降到 low 后思考收敛到 1,000–3,000 token 且正常产出。
CAPTION_REASONING_EFFORT = "low"

_CYRILLIC_PATTERN = re.compile(r"[\u0400-\u04FF]")
_EMOJI_PATTERN = re.compile(
    "[" 
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]"
)


@dataclass(frozen=True)
class CaptionSpec:
    """一个平台的短文规格（直接对应报告里的风格规范表）。"""

    key: str
    name: str
    language: str
    min_chars: int
    max_chars: int
    hashtag_min: int
    hashtag_max: int
    emoji_max: int
    fold_chars: int
    structure: tuple[str, ...]
    link_rule: str
    style_rules: tuple[str, ...] = field(default=())
    needs_first_comment: bool = False
    #: 该平台是否允许把链接放进正文（LinkedIn / Facebook 正文带链接会显著降权）
    link_in_body_allowed: bool = False

    @property
    def structure_text(self) -> str:
        return " → ".join(self.structure)

    @property
    def length_text(self) -> str:
        return f"{self.min_chars}–{self.max_chars} 字符"

    def as_payload(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "language": self.language,
            "min_chars": self.min_chars,
            "max_chars": self.max_chars,
            "hashtag_min": self.hashtag_min,
            "hashtag_max": self.hashtag_max,
            "emoji_max": self.emoji_max,
            "fold_chars": self.fold_chars,
            "structure": list(self.structure),
            "structure_text": self.structure_text,
            "length_text": self.length_text,
            "link_rule": self.link_rule,
            "link_in_body_allowed": self.link_in_body_allowed,
        }


#: 四个平台的短文规格（报告 §2.1 + §3）
CAPTION_SPECS: dict[str, CaptionSpec] = {
    "linkedin": CaptionSpec(
        key="linkedin",
        name="LinkedIn",
        language="en",
        min_chars=600,
        max_chars=1200,
        hashtag_min=3,
        hashtag_max=5,
        emoji_max=2,
        fold_chars=210,
        structure=("钩子行", "痛点", "能力证据（分点）", "产品范围", "CTA", "标签"),
        link_rule="正文不放链接，链接放第一条评论",
        needs_first_comment=True,
        style_rules=(
            "第一行必须是对方的利益或痛点，绝不是自我介绍",
            "能力证据要分点写（CASTING / MACHINING / VERIFICATION / FINISHING），采购是扫读的",
            "每段 1–2 行，段间空行，不要写成一整块",
            "工程语言、克制、可信；emoji 最多 2 个且只做功能型分隔",
            "结尾用提问收尾，带起评论",
        ),
    ),
    "facebook": CaptionSpec(
        key="facebook",
        name="Facebook",
        language="en",
        min_chars=150,
        max_chars=400,
        hashtag_min=1,
        hashtag_max=2,
        emoji_max=4,
        fold_chars=120,
        structure=("场景描述", "一句结论", "提问收尾", "标签"),
        link_rule="正文不放链接，链接放评论",
        needs_first_comment=True,
        style_rules=(
            "比 LinkedIn 更口语、更有画面感，但仍克制",
            "场景描述控制在 3 段以内，前 2 行决定是否被展开",
            "可以用 2–4 个 emoji，允许一个画面型（🏭 🔥）",
            "标签只 1–2 个，过多降权",
        ),
    ),
    "tiktok": CaptionSpec(
        key="tiktok",
        name="TikTok",
        language="en",
        min_chars=60,
        max_chars=150,
        hashtag_min=4,
        hashtag_max=5,
        emoji_max=3,
        fold_chars=60,
        structure=("钩子（首行）", "一句结论", "标签"),
        link_rule="正文与评论都不放链接，链接只放主页简介",
        style_rules=(
            "首行即钩子，画面优先于文字",
            "不谈采购、不谈产能，不写「本公司专业生产…」式开场",
            "1–3 个 emoji，服务于情绪",
            "标签 4–5 个品类词",
        ),
    ),
    "vk": CaptionSpec(
        key="vk",
        name="VK",
        language="ru",
        min_chars=500,
        max_chars=1500,
        hashtag_min=2,
        hashtag_max=5,
        emoji_max=2,
        fold_chars=200,
        structure=("企业介绍", "工艺", "产品", "设备", "合作方式"),
        link_rule="外链可以直接放正文（VK 外链降权较轻）",
        link_in_body_allowed=True,
        style_rules=(
            "对外文案用俄语；同时保留一份英语母版供复用与审校",
            "语气比 LinkedIn 更正式、更完整，可用企业介绍式叙述",
            "标签 2–5 个，可含西里尔字母",
            "前 2–3 行决定是否被展开",
        ),
    ),
}


def caption_spec(key: str | None) -> CaptionSpec | None:
    return CAPTION_SPECS.get((key or "").strip().lower())


@dataclass(frozen=True)
class CaptionCheck:
    """一条校验结果，回显到界面上让用户自己判断。"""

    name: str
    ok: bool
    detail: str = ""

    def as_payload(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class CaptionResult:
    platform: str
    language: str
    text: str
    hashtags: tuple[str, ...]
    first_comment: str = ""
    text_en: str = ""
    checks: tuple[CaptionCheck, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def full_text(self) -> str:
        """正文 + 标签，可直接复制发布。"""
        tags = " ".join(self.hashtags)
        return f"{self.text}\n\n{tags}".strip() if tags else self.text

    def as_payload(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "language": self.language,
            "text": self.text,
            "text_en": self.text_en,
            "hashtags": list(self.hashtags),
            "first_comment": self.first_comment,
            "full_text": self.full_text,
            "char_count": len(self.text),
            "checks": [item.as_payload() for item in self.checks],
            "warnings": list(self.warnings),
        }


def count_emoji(text: str) -> int:
    return len(_EMOJI_PATTERN.findall(text or ""))


def validate_caption(
    spec: CaptionSpec, text: str, hashtags: Iterable[str]
) -> tuple[tuple[CaptionCheck, ...], tuple[str, ...]]:
    """按规格逐条校验，返回 ``(校验结果, 警告)``。"""
    tags = [str(tag).strip() for tag in hashtags if str(tag).strip()]
    body = text or ""
    checks: list[CaptionCheck] = []
    warnings: list[str] = []

    length = len(body)
    in_range = spec.min_chars <= length <= spec.max_chars
    checks.append(
        CaptionCheck(
            "长度",
            in_range,
            f"{length} 字符（要求 {spec.length_text}）"
            + ("" if in_range else ("，偏短" if length < spec.min_chars else "，偏长")),
        )
    )

    tag_ok = spec.hashtag_min <= len(tags) <= spec.hashtag_max
    checks.append(
        CaptionCheck(
            "标签数",
            tag_ok,
            f"{len(tags)} 个（要求 {spec.hashtag_min}–{spec.hashtag_max} 个）",
        )
    )

    malformed = [tag for tag in tags if not tag.startswith("#") or " " in tag]
    checks.append(
        CaptionCheck("标签规范", not malformed, "违规：" + "、".join(malformed) if malformed else "均以 # 开头且无空格")
    )

    emoji_count = count_emoji(body)
    checks.append(
        CaptionCheck("emoji", emoji_count <= spec.emoji_max, f"{emoji_count} 个（上限 {spec.emoji_max}）")
    )

    lowered = body.lower()
    banned = [word for word in BANNED_PHRASES if word in lowered]
    if banned:
        warnings.append("命中禁用语：" + "、".join(banned) + "（报告 §8.3）")
    checks.append(CaptionCheck("禁用语", not banned, "、".join(banned) if banned else "未命中"))

    claims = find_unverified_claims(body)
    if claims:
        warnings.append(
            "出现数字/规格类表述（" + "、".join(claims) + "）：确属可公开的真实数据才可保留"
        )
    checks.append(
        CaptionCheck("未核实参数", not claims, "、".join(claims) if claims else "未发现")
    )

    has_url = bool(_URL_PATTERN.search(body))
    if not spec.link_in_body_allowed and has_url:
        checks.append(CaptionCheck("外链位置", False, "正文出现链接，应移到评论或主页简介"))
    elif spec.link_in_body_allowed and has_url:
        checks.append(CaptionCheck("外链位置", True, spec.link_rule))
    else:
        checks.append(CaptionCheck("外链位置", True, "正文无链接；" + spec.link_rule))

    if spec.language == "ru":
        has_ru = bool(_CYRILLIC_PATTERN.search(body))
        checks.append(
            CaptionCheck("俄语", has_ru, "含西里尔字母" if has_ru else "未见西里尔字母，可能不是俄语")
        )

    fold_preview = body[: spec.fold_chars].strip()
    checks.append(
        CaptionCheck("折叠区", bool(fold_preview), f"前 {spec.fold_chars} 字符内是否有钩子：" + fold_preview[:60])
    )
    return tuple(checks), tuple(warnings)


def build_caption_instructions(
    spec: CaptionSpec, profile: PlatformProfile | None = None
) -> str:
    """按报告拼出系统提示词。"""
    lines = [
        "你是沧州菲美得（工业铸件与粗加工一体化，河北沧州）的海外社媒文案。",
        f"现在要写出 **{spec.name}** 平台的帖子短文，严格遵循下面的模式。",
        "",
        "【只能使用给定素材描述里的可见事实】"
        "严禁编造材质牌号、公差、单重、月产能、检测结果、认证、客户名称、出口国家。"
        "看不见或没给的信息就不要写，改用不涉及数字的表述。",
        "严禁使用无法证实的形容（world-class / leading / No.1 / best price 等）"
        "与任何承诺性表述（零封号 / 保证交期 等）。",
        "",
        f"【结构】{spec.structure_text}",
        f"【长度】正文 {spec.length_text}",
        f"【标签】{spec.hashtag_min}–{spec.hashtag_max} 个，已含 #，不含空格",
        f"【emoji】最多 {spec.emoji_max} 个",
        f"【折叠位置】前 {spec.fold_chars} 字符决定读者是否展开，钩子必须落在这里",
        f"【外链】{spec.link_rule}",
    ]
    if spec.language == "ru":
        lines.append("【语言】正文用俄语；同时给出一份英语母版 text_en 供复用与审校")
    else:
        lines.append("【语言】英语")
    if profile is not None:
        lines.append(f"【该平台素材口径】画幅 {profile.aspect}，{profile.shot_count}，{profile.form}")
    for rule in spec.style_rules:
        lines.append(f"- {rule}")
    fields = ["text（正文）", "hashtags（标签数组）"]
    if spec.language == "ru":
        fields.append("text_en（英语母版）")
    if spec.needs_first_comment:
        fields.append("first_comment（第一条评论；链接用 [link] 占位）")
    lines.append("")
    lines.append("只输出 JSON，字段：" + "、".join(fields) + "。")
    return "\n".join(lines)


def _material_brief(materials: Iterable[Any]) -> str:
    """把选中的素材压成"只含可见事实"的素材清单，喂给模型。"""
    rows: list[str] = []
    for index, item in enumerate(materials, start=1):
        category = getattr(item, "category", "") or ""
        summary = getattr(item, "summary", "") or ""
        keywords = " / ".join(getattr(item, "keywords", ()) or ())
        file_name = getattr(item, "file_name", "")
        parts = [f"{index}. 文件 {file_name}", f"品类 {category}", f"画面 {summary}"]
        if keywords:
            parts.append(f"关键词 {keywords}")
        rows.append("｜".join(part for part in parts if part))
    return "\n".join(rows) if rows else "（未提供素材）"


def _payload(
    config: VisionConfig,
    instructions: str,
    prompt: str,
    *,
    max_tokens: int = CAPTION_MAX_TOKENS,
    reasoning_effort: str = CAPTION_REASONING_EFFORT,
) -> dict[str, Any]:
    """纯文本调用：两种 API 形态各拼一份载荷。"""
    if config.api_style == "responses":
        payload: dict[str, Any] = {
            "model": config.model,
            "instructions": instructions,
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
            ],
        }
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
        if max_tokens > 0:
            payload["max_output_tokens"] = max_tokens
        return payload
    payload = {
        "model": config.model,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": prompt},
        ],
    }
    if max_tokens > 0:
        payload["max_tokens"] = max_tokens
    return payload


def _extract_text(body: dict[str, Any], api_style: str) -> str:
    if api_style == "responses":
        chunks: list[str] = []
        for item in body.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    chunks.append(str(part.get("text") or ""))
        if not chunks and isinstance(body.get("output_text"), str):
            chunks.append(body["output_text"])
        return "".join(chunks)
    content = body["choices"][0]["message"]["content"]
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def generate_caption(
    config: VisionConfig,
    *,
    platform: str,
    materials: Iterable[Any],
    client: Any | None = None,
    extra_note: str = "",
) -> CaptionResult:
    """按平台规格生成短文，并返回校验结果。

    ``materials`` 是已召回的素材（``MediaAsset`` 或 ``RecallPick`` 均可，
    只要有 ``file_name`` / ``category`` / ``summary`` / ``keywords``）。
    """
    spec = caption_spec(platform)
    if spec is None:
        raise ValueError(f"不支持的平台：{platform!r}（可选：{', '.join(CAPTION_SPECS)}）")
    if not config.enabled:
        raise ValueError("未配置视觉模型 Key，无法生成短文（见 .env 的 PULSE_VISION_API_KEY）。")

    profile = platform_profile(spec.key)
    instructions = build_caption_instructions(spec, profile)
    prompt_parts = [
        f"请按 {spec.name} 的模式写这一帖的短文。",
        "",
        "本次选用的素材（这些是唯一的可用事实来源）：",
        _material_brief(materials),
    ]
    if extra_note.strip():
        prompt_parts += ["", f"补充要求：{extra_note.strip()}"]
    prompt_parts += ["", "只输出 JSON，不要输出任何解释。"]

    limit = config.caption_max_tokens or CAPTION_MAX_TOKENS
    body = post_json(
        config,
        _payload(config, instructions, "\n".join(prompt_parts), max_tokens=limit),
        client=client,
    )
    check_response_complete(body)
    parsed = _loads_json_object(_extract_text(body, config.api_style))

    text = str(parsed.get("text") or "").strip()
    if not text:
        raise ValueError("模型没有返回正文")
    raw_tags = parsed.get("hashtags") or []
    if isinstance(raw_tags, str):
        raw_tags = raw_tags.replace(",", " ").split()
    hashtags = tuple(
        tag if tag.startswith("#") else f"#{tag}"
        for tag in (str(item).strip() for item in raw_tags)
        if tag
    )
    checks, warnings = validate_caption(spec, text, hashtags)
    return CaptionResult(
        platform=spec.key,
        language=spec.language,
        text=text,
        hashtags=hashtags,
        first_comment=str(parsed.get("first_comment") or "").strip(),
        text_en=str(parsed.get("text_en") or "").strip(),
        checks=checks,
        warnings=warnings,
    )
