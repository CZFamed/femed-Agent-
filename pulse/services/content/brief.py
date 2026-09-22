"""brief 解析与校验（所有者：A1，任务包 W1-A1-2）。

设计要点：

1. **严格但不啰嗦**：一次返回全部问题（``BriefValidationError.errors``），
   而不是发现第一个就退出——运营改一遍就能过。
2. **平台白名单**：brief 里出现 TikTok 会被明确拒绝（本项目暂不投入），
   错误信息里直接说明原因，不要让人以为是拼写问题。
3. **不伪造业务事实**：brief 只承载"这次要写什么"，产能、公差、材质等
   硬参数不在这里编造，缺失时由生成与合规环节标注 ``TODO(need-real-data)``。

契约依据：``INTERFACES.md`` §6 的 ``contents`` 表（``brief_id`` / ``title`` /
``body_seed`` / ``brand_guide_id``）与 §9 的分工（A1 入口是 ``contents``/``briefs``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pulse.services.content.errors import BriefValidationError
from pulse.shared.enums import Platform
from pulse.shared.ids import new_brief_id

#: token 预算默认值（派工单 W1-A1-6：默认 60k，可配置）
DEFAULT_TOKEN_BUDGET = 60_000

#: 单条 brief 默认配图张数
DEFAULT_BUDGET_MEDIA = 3

#: 预算下限：低于这个值的配置几乎必然熔断，属于配置错误
MIN_TOKEN_BUDGET = 1_000

#: 目前只支持英语 brief（英语是客户文案主语言；中英对照在生成阶段产出）
SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"en"})

#: brief 允许出现的字段。出现未知字段一律报错，避免"写了没人读"的静默丢失
ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "brief_id",
        "topic",
        "target_audience",
        "platforms",
        "language",
        "brand_guide_id",
        "budget_media",
        "token_budget",
        "extra_terms",
        "cta",
    }
)

#: 明确不投入、但常被误填的平台（错误信息里要讲清原因，而不是笼统说"非法"）
_NOT_INVESTED: Mapping[str, str] = {
    "tiktok": "TikTok 在本项目暂不投入（受众与询盘路径不匹配），MVP 只保留半自动导出能力",
}


def _platform_of(raw: Any) -> Platform:
    """把字符串/枚举转成 ``Platform``；非法时抛出可读错误。"""
    if isinstance(raw, Platform):
        return raw
    text = str(raw or "").strip().lower()
    if text in _NOT_INVESTED:
        raise ValueError(_NOT_INVESTED[text])
    try:
        return Platform(text)
    except ValueError as exc:  # noqa: PERF203 - 明确需要逐个报错
        allowed = "、".join(item.value for item in Platform)
        raise ValueError(f"平台 {raw!r} 不在白名单内（允许：{allowed}）") from exc


@dataclass(frozen=True, slots=True)
class Brief:
    """一条生成指令。"""

    brief_id: str
    topic: str
    target_audience: str
    platforms: tuple[Platform, ...]
    language: str = "en"
    brand_guide_id: str | None = None
    budget_media: int = DEFAULT_BUDGET_MEDIA
    token_budget: int = DEFAULT_TOKEN_BUDGET
    extra_terms: tuple[str, ...] = ()
    cta: str | None = None

    @property
    def title(self) -> str:
        """契约 §6 ``contents.title`` 的取值来源。"""
        return self.topic

    @property
    def body_seed(self) -> str:
        """契约 §6 ``contents.body_seed`` 的取值来源。"""
        parts = [f"目标读者：{self.target_audience}"]
        if self.cta:
            parts.append(f"期望行动：{self.cta}")
        return "\n".join(parts)

    def as_fields(self) -> dict[str, Any]:
        """喂给 prompt 渲染的结构化字段（不做文案创作）。"""
        return {
            "brief_id": self.brief_id,
            "topic": self.topic,
            "target_audience": self.target_audience,
            "platforms": [item.value for item in self.platforms],
            "language": self.language,
            "cta": self.cta or "",
            "extra_terms": list(self.extra_terms),
        }


def parse_brief(payload: Mapping[str, Any]) -> Brief:
    """把外部传入的 dict 解析成严格校验过的 ``Brief``。

    Raises:
        BriefValidationError: 任一字段不合法（含全部错误，便于一次性修完）。
    """
    if not isinstance(payload, Mapping):
        raise BriefValidationError([f"brief 必须是对象（dict），收到 {type(payload).__name__}"])

    errors: list[str] = []
    unknown = sorted(set(payload) - ALLOWED_KEYS)
    if unknown:
        errors.append(
            "出现未定义字段：" + "、".join(unknown) + "（允许字段：" + "、".join(sorted(ALLOWED_KEYS)) + "）"
        )

    topic = str(payload.get("topic") or "").strip()
    if not topic:
        errors.append("topic 不能为空——它是内容的主题，也是 contents.title 的来源")

    audience = str(payload.get("target_audience") or "").strip()
    if not audience:
        errors.append("target_audience 不能为空——B2B 内容必须明确写给谁看")

    raw_platforms = payload.get("platforms")
    if raw_platforms is None:
        errors.append("platforms 不能为空——至少指定一个平台")
        platforms: list[Platform] = []
    elif isinstance(raw_platforms, (str, bytes)) or not isinstance(raw_platforms, (list, tuple, set)):
        errors.append(f"platforms 必须是数组，收到 {type(raw_platforms).__name__}")
        platforms = []
    else:
        platforms = []
        platform_errors_before = len(errors)
        for item in raw_platforms:
            try:
                platforms.append(_platform_of(item))
            except ValueError as exc:
                errors.append(str(exc))
        # 只有"一个平台都没解析出来、且逐项没报过错"时才补一条空数组错误，
        # 避免与逐项错误重复
        if not platforms and len(errors) == platform_errors_before:
            errors.append("platforms 不能为空——至少指定一个平台")
        # 去重但保持顺序
        deduped: list[Platform] = []
        for item in platforms:
            if item not in deduped:
                deduped.append(item)
        platforms = deduped

    language = str(payload.get("language") or "en").strip().lower()
    if language not in SUPPORTED_LANGUAGES:
        errors.append(
            f"language 目前只支持 {'、'.join(sorted(SUPPORTED_LANGUAGES))}（客户文案以英语为主，"
            "中英对照由生成阶段产出；VK 的俄语由英语母版翻译派生）"
        )

    budget_media_raw = payload.get("budget_media", DEFAULT_BUDGET_MEDIA)
    with_media = True
    try:
        budget_media = int(budget_media_raw)
    except (TypeError, ValueError):
        errors.append(f"budget_media 必须是整数，收到 {budget_media_raw!r}")
        budget_media = DEFAULT_BUDGET_MEDIA
        with_media = False
    if with_media and budget_media < 0:
        errors.append(f"budget_media 不能为负：{budget_media}")

    token_budget_raw = payload.get("token_budget", DEFAULT_TOKEN_BUDGET)
    try:
        token_budget = int(token_budget_raw)
    except (TypeError, ValueError):
        errors.append(f"token_budget 必须是整数，收到 {token_budget_raw!r}")
        token_budget = DEFAULT_TOKEN_BUDGET
    else:
        if token_budget < MIN_TOKEN_BUDGET:
            errors.append(
                f"token_budget 过小：{token_budget}（下限 {MIN_TOKEN_BUDGET}）——"
                "预算不足会直接熔断，几乎不可能生成完整文案"
            )

    extra_terms_raw = payload.get("extra_terms") or ()
    if isinstance(extra_terms_raw, str):
        extra_terms = (extra_terms_raw.strip(),) if extra_terms_raw.strip() else ()
    else:
        try:
            extra_terms = tuple(str(item).strip() for item in extra_terms_raw if str(item).strip())
        except TypeError:
            errors.append(f"extra_terms 必须是字符串数组，收到 {type(extra_terms_raw).__name__}")
            extra_terms = ()

    brand_guide_id_raw = payload.get("brand_guide_id")
    brand_guide_id = str(brand_guide_id_raw).strip() if brand_guide_id_raw else None

    cta_raw = payload.get("cta")
    cta = str(cta_raw).strip() if cta_raw else None

    brief_id_raw = payload.get("brief_id")
    if brief_id_raw:
        brief_id = str(brief_id_raw).strip()
        if not brief_id.startswith("b_"):
            errors.append(f"brief_id 必须以 b_ 开头：{brief_id!r}")
    else:
        brief_id = new_brief_id()

    if errors:
        raise BriefValidationError(errors)

    return Brief(
        brief_id=brief_id,
        topic=topic,
        target_audience=audience,
        platforms=tuple(platforms),
        language=language,
        brand_guide_id=brand_guide_id,
        budget_media=budget_media,
        token_budget=token_budget,
        extra_terms=extra_terms,
        cta=cta,
    )
