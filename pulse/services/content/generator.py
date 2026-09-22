"""内容生成编排（所有者：A1）。

把"brief → 选素材 → 渲染提示词 → 写文案 → 派生 variant → 记账"串起来，
并且默认使用固定文案桩（StubCopywriter）：W1 阶段不调真实模型
（派工单 §5 禁止事项 3），真实模型接入时只需替换 Copywriter 实现。

队列契约：本域消费 pulse.content.generate_variant，载荷 {brief_id, platform}
（契约 §5）。载荷里只有 ID，业务对象由本域自己从 ContentStore 取。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol, Sequence

from pulse.services.content.brief import Brief, parse_brief
from pulse.services.content.errors import ContentError, ContentNotFoundError
from pulse.services.content.hashtags import compose_hashtags
from pulse.services.content.mediaselect import (
    NEED_REAL_DATA,
    MediaSelection,
    has_slot_profile,
    select_media,
)
from pulse.services.content.prompts import prompt_template, render_prompt
from pulse.services.content.router import TASK_LONG_FORM, ModelRouter, TokenBudget
from pulse.services.content.store import ContentRecord, ContentStore
from pulse.services.content.variants import (
    AccountBinding,
    DerivedVariant,
    VariantDraft,
    build_unified_post,
    media_items_from_picks,
)
from pulse.services.media.platforms import platform_profile
from pulse.shared.enums import ContentType, Platform
from pulse.shared.models import MediaItem

#: 队列任务名（契约 §5）
TASK_GENERATE_VARIANT = "pulse.content.generate_variant"


@dataclass(frozen=True, slots=True)
class CopyRequest:
    """交给文案生成器的输入（不含任何平台发布参数）。"""

    platform: str
    brief: Brief
    prompt: str
    media_brief: str = ""
    evidence_gaps: tuple[str, ...] = ()
    #: 位次短语：casting / machining / verification / finishing → 素材文件名或占位符
    slot_terms: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CopyDraft:
    """文案生成器的输出。"""

    text: str
    text_zh: str | None = None
    title: str | None = None
    hashtags: tuple[str, ...] = ()
    tokens_used: int = 0
    ai_score: float | None = None


class Copywriter(Protocol):
    """文案生成器接口。真实模型接入时实现本协议即可。"""

    def write(self, request: CopyRequest) -> CopyDraft:  # pragma: no cover - 协议声明
        ...


#: 位次序号 → 文案里引用的能力短语（对齐报告 §3.1 的 LinkedIn 四个位次）
SLOT_TERM_KEYS: Mapping[int, str] = {
    1: "casting",
    2: "machining",
    3: "verification",
    4: "finishing",
}

#: 固定文案桩的分平台段落骨架。只使用 brief 字段与素材文件名；
#: 需要硬参数的位置一律留 TODO(need-real-data) 占位（铁律 7：不伪造数据）。
STUB_BODIES: Mapping[str, str] = {
    "linkedin": (
        "Most machine tool builders don't own a foundry.\n\n"
        "{topic}\n\n"
        "We cover {audience} with casting + pre-machining from one plant:\n"
        "- CASTING: lost-foam and sand casting — {casting}\n"
        "- MACHINING: boring and milling on the same site — {machining}\n"
        "- VERIFICATION: dimensional check before shipment — {verification}\n"
        "- FINISHING: protective coating and export packing — {finishing}\n\n"
        "If your casting supplier stops at the raw part, that pre-machining step is usually "
        "the one that slips your assembly date.\n\n"
        "What does your current supplier quote separately — casting, or casting plus machining?"
    ),
    "youtube": (
        "{topic}\n\n"
        "Shot inside our Botou plant for {audience}. The clip shows the working sequence "
        "rather than a product render: {casting}, then {machining}.\n\n"
        "Capability notes:\n"
        "- Casting and pre-machining under one roof\n"
        "- Dimensional verification before packing — {verification}\n"
        "- Export packing and loading — {finishing}\n\n"
        "Parameters we have not verified are left blank: TODO(need-real-data).\n\n"
        "Contact: czfamed1@outlook.com"
    ),
    "facebook": (
        "{topic}\n\n"
        "Work in progress at the plant — {casting}.\n\n"
        "Casting plus pre-machining from one supplier: does that fit how you buy?"
    ),
    "vk": (
        "{topic}\n\n"
        "Мы производим чугунное литьё и выполняем предварительную механическую "
        "обработку на одном заводе.\n\n"
        "Технология: {casting}\n"
        "Обработка: {machining}\n"
        "Контроль: {verification}\n"
        "Упаковка: {finishing}\n\n"
        "Параметры, которые мы не подтвердили, оставлены пустыми: TODO(need-real-data)."
    ),
    "reddit": (
        "{topic}\n\n"
        "Background: we run a foundry plus a machining shop in Hebei, China, and supply "
        "{audience}.\n\n"
        "What we can actually show: {casting}, {machining}, {verification}.\n\n"
        "Happy to answer process questions; not here to pitch."
    ),
    "instagram": "{topic}\n\nCasting + pre-machining, one plant. {casting}",
}

#: 每个平台默认引用的话题标签类别（按内容支柱）
PLATFORM_HASHTAG_CATEGORIES: Mapping[str, tuple[str, ...]] = {
    "linkedin": ("casting", "machining", "machine_tools", "quality"),
    "youtube": ("casting", "machining", "machine_tools"),
    "facebook": ("casting",),
    "vk": ("casting", "machining"),
    "reddit": (),
    "instagram": ("casting", "machining"),
}

#: 固定文案桩的 token 用量（估算值，用于让预算熔断可测；
#: 真实模型接入后由供应商返回的真实用量替换）
STUB_TOKENS_PER_PLATFORM: Mapping[str, int] = {
    "linkedin": 2_400,
    "youtube": 1_600,
    "facebook": 900,
    "vk": 3_200,
    "reddit": 1_200,
    "instagram": 600,
}


def slot_terms_from_selection(selection: MediaSelection | None) -> dict[str, str]:
    """把选用结果翻成文案里可引用的短语；没有素材的位次留占位符。

    ⚠️ 缺素材时**只能留英文占位符** ``TODO(need-real-data)``：这段文字会被直接拼进
    ``caption.text``，而契约 §2 要求 ``caption.text`` 是英文主文案、中文只进 ``text_zh``
    （中文注记若混进正文，就是要发到英文平台上的东西）。
    缺哪个位次、为什么要补拍，由 ``missing_slot_gaps`` 以缺口形式另行说明。
    """
    terms: dict[str, str] = {}
    for order, key in SLOT_TERM_KEYS.items():
        phrase = None
        if selection is not None:
            for slot in selection.slots:
                if slot.order == order and slot.picks:
                    phrase = slot.picks[0].file_name
                    break
        terms[key] = phrase or NEED_REAL_DATA
    return terms


def missing_slot_gaps(
    selection: MediaSelection | None, expected_orders: set[int]
) -> tuple[str, ...]:
    """列出"位次缺素材"的缺口（中文说明走这里，不进 ``caption.text``）。

    ``expected_orders`` 是该平台确实定义了的位次；没定义的位次不算缺口
    （例如某平台只有 3 格，就不该因为第 4 格没图而报补拍）。
    """
    gaps: list[str] = []
    for order, key in SLOT_TERM_KEYS.items():
        if order not in expected_orders:
            continue
        has_pick = False
        if selection is not None:
            has_pick = any(slot.order == order and slot.picks for slot in selection.slots)
        if not has_pick:
            gaps.append(
                f"{NEED_REAL_DATA}：位次{order}（{key}）缺素材，正文只保留了英文占位符"
            )
    return tuple(gaps)


class StubCopywriter:
    """固定文案桩：可重复、不发网络请求、不编造参数。"""

    def write(self, request: CopyRequest) -> CopyDraft:
        template = prompt_template(request.platform)
        terms = dict(request.slot_terms or {})
        body = STUB_BODIES.get(template.platform, "{topic}").format(
            topic=request.brief.topic,
            audience=request.brief.target_audience,
            casting=terms.get("casting", NEED_REAL_DATA),
            machining=terms.get("machining", NEED_REAL_DATA),
            verification=terms.get("verification", NEED_REAL_DATA),
            finishing=terms.get("finishing", NEED_REAL_DATA),
        )
        hashtags = compose_hashtags(
            categories=PLATFORM_HASHTAG_CATEGORIES.get(template.platform, ()),
            extra=request.brief.extra_terms,
            max_count=template.hashtag_max,
        )
        title = request.brief.topic[:100] if template.content_type == "video" else None
        return CopyDraft(
            text=body,
            text_zh=f"【中文对照】{request.brief.topic}——面向{request.brief.target_audience}",
            title=title,
            hashtags=hashtags,
            tokens_used=STUB_TOKENS_PER_PLATFORM.get(template.platform, 1_000),
            ai_score=0.8,
        )


@dataclass
class ContentService:
    """内容生产域的门面：brief 提交、按平台生成、变体派生。

    构造时可注入素材库与召回策略（``assets`` / ``policy``）以及真实的
    尺寸/时长探针（``media_size_of`` / ``media_duration_of``）。
    不注入时退化为纯文本生成——**不会**用默认尺寸凑数。
    """

    store: ContentStore = field(default_factory=ContentStore)
    copywriter: Copywriter = field(default_factory=StubCopywriter)
    assets: Sequence[Any] = field(default_factory=tuple)
    policy: Any | None = None
    media_size_of: Callable[[str], tuple[int, int] | None] | None = None
    media_duration_of: Callable[[str], float | None] | None = None
    accounts: Mapping[str, AccountBinding] = field(default_factory=dict)
    _briefs: dict[str, Brief] = field(default_factory=dict, init=False, repr=False)
    _routers: dict[str, ModelRouter] = field(default_factory=dict, init=False, repr=False)

    # ---------- brief ----------

    def submit_brief(self, payload: Mapping[str, Any]) -> ContentRecord:
        """解析 brief、建预算账本、落一条 ``contents``（同 brief 幂等）。"""
        brief = parse_brief(payload)
        existing = self.store.content_by_brief(brief.brief_id)
        self._briefs[brief.brief_id] = brief
        self._routers[brief.brief_id] = ModelRouter(budget=TokenBudget(brief.token_budget))
        if existing is not None:
            return existing
        return self.store.create_content(
            brief_id=brief.brief_id,
            title=brief.title,
            body_seed=brief.body_seed,
            brand_guide_id=brief.brand_guide_id,
        )

    def brief(self, brief_id: str) -> Brief:
        found = self._briefs.get(brief_id)
        if found is None:
            raise ContentNotFoundError(f"找不到 brief {brief_id!r}", details={"brief_id": brief_id})
        return found

    def router_for(self, brief_id: str) -> ModelRouter:
        found = self._routers.get(brief_id)
        if found is None:
            raise ContentNotFoundError(
                f"brief {brief_id!r} 还没有预算账本（先 submit_brief）",
                details={"brief_id": brief_id},
            )
        return found

    # ---------- 生成 ----------

    def generate_variant(
        self,
        brief_id: str,
        platform: str | Platform,
        *,
        account: AccountBinding | None = None,
        scheduled_at: datetime | None = None,
        assets: Sequence[Any] | None = None,
        policy: Any | None = None,
        media: Sequence[MediaItem] | None = None,
    ) -> DerivedVariant:
        """生成一个平台的变体：选素材 → 渲染 prompt → 写文案 → 记账 → 派生。

        Raises:
            ContentNotFoundError: brief 未知。
            ContentError: 没有账号绑定、素材缺真实尺寸/时长、契约校验不通过。
            TokenBudgetExceeded: 本次生成的 token 超出该 brief 的预算（熔断）。
        """
        brief = self.brief(brief_id)
        template = prompt_template(platform)
        source = self.store.content_by_brief(brief_id)
        if source is None:
            raise ContentNotFoundError(
                f"brief {brief_id!r} 还没有对应的核心内容（先 submit_brief）",
                details={"brief_id": brief_id},
            )

        manual_media = tuple(media or ())
        asset_pool = [] if manual_media else list(assets if assets is not None else self.assets)
        recall_policy = policy if policy is not None else self.policy
        selection: MediaSelection | None = None
        extra_gaps: tuple[str, ...] = ()
        if asset_pool and recall_policy is not None:
            if has_slot_profile(template.platform):
                selection = select_media(
                    template.platform,
                    asset_pool,
                    recall_policy,
                    extra_terms=brief.extra_terms,
                )
            else:
                # 该平台还没有位次口径（目前只有 linkedin/facebook/tiktok/vk 有），
                # 不猜画面顺序，只把缺口如实标出来交给人工配图
                extra_gaps = (
                    f"{NEED_REAL_DATA}：平台 {template.platform} 尚无画面位次口径，"
                    "本次未自动选素材，需要人工配图或先补齐该平台的位次定义",
                )

        profile = platform_profile(template.platform)
        expected_orders = {slot.order for slot in profile.slots} if profile is not None else set()
        media_brief = selection.media_brief() if selection is not None else ""
        # 缺口说明一律走这里（中文），正文只留英文占位符——契约 §2 的 text/text_zh 分工
        gaps = (
            (selection.gaps if selection is not None else ())
            + missing_slot_gaps(selection, expected_orders)
            + extra_gaps
        )
        request = CopyRequest(
            platform=template.platform,
            brief=brief,
            prompt=render_prompt(
                template.platform, brief, media_brief=media_brief, evidence_gaps=gaps
            ),
            media_brief=media_brief,
            evidence_gaps=gaps,
            slot_terms=slot_terms_from_selection(selection),
        )
        draft = self.copywriter.write(request)

        # 预算熔断点：超预算直接抛 TokenBudgetExceeded，不静默截断
        self.router_for(brief_id).charge(TASK_LONG_FORM, draft.tokens_used)

        media_items = manual_media if manual_media else self._media_items(selection)
        variant_draft = VariantDraft(
            platform=Platform(template.platform),
            text=draft.text,
            text_zh=draft.text_zh,
            title=draft.title,
            hashtags=draft.hashtags,
            media=media_items,
            content_type=ContentType.TEXT if not media_items else None,
            ai_score=draft.ai_score,
        )
        binding = account or self.accounts.get(template.platform)
        if binding is None:
            raise ContentError(
                f"平台 {template.platform} 没有账号绑定，无法派生变体"
                "（AccountBinding 需要账号 ID 与该账号的 options）",
                details={"platform": template.platform, "provided": sorted(self.accounts)},
            )

        record = self.store.create_variant(
            source_id=source.id,
            platform=variant_draft.platform,
            fields={},
            ai_score=draft.ai_score,
        )
        post = build_unified_post(
            draft=variant_draft,
            source_id=source.id,
            account=binding,
            variant_id=record.id,
            scheduled_at=scheduled_at,
        )
        derived = DerivedVariant(
            variant_id=record.id,
            source_id=source.id,
            platform=variant_draft.platform,
            post=post,
        )
        # 把生成结果写回 variants.fields（契约 §6）
        self.store.update_variant_fields(record.id, derived.fields())
        if media_items:
            self.store.attach_media(record.id, media_items)
        return derived

    def _media_items(self, selection: MediaSelection | None) -> tuple[MediaItem, ...]:
        """把选用结果转成 MediaItem；尺寸/时长必须来自真实探针。"""
        if selection is None:
            return ()
        picks = [pick for slot in selection.slots for pick in slot.picks]
        if not picks:
            return ()
        if self.media_size_of is None:
            raise ContentError(
                "选到了素材但没有注入 media_size_of（真实尺寸探针），"
                "无法生成 UnifiedPost——不允许用默认尺寸凑数",
                details={"picked": len(picks)},
            )
        return media_items_from_picks(
            picks,
            size_of=self.media_size_of,
            duration_of=self.media_duration_of,
        )

    # ---------- 队列入口 ----------

    def handle_generate_variant(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """处理 ``pulse.content.generate_variant`` 消息（载荷只有 ID）。"""
        brief_id = str(payload.get("brief_id") or "").strip()
        platform = str(payload.get("platform") or "").strip()
        if not brief_id or not platform:
            raise ContentError(
                f"{TASK_GENERATE_VARIANT} 的载荷必须是 {{brief_id, platform}}，收到 {dict(payload)!r}",
                details={"payload": dict(payload)},
            )
        derived = self.generate_variant(brief_id, platform)
        return {TASK_GENERATE_VARIANT: derived.as_payload()}

    # ---------- 自评（轻模型，只做一轮） ----------

    def self_review(self, brief_id: str, *, tokens_used: int = 400) -> dict[str, Any]:
        """需求 §3.3 的"生成 → 自评 → 重排"轻量反馈：只一轮，不做无限采样。

        自评同样计入预算，超预算即熔断。
        """
        from pulse.services.content.router import TASK_SELF_REVIEW

        route = self.router_for(brief_id).charge(TASK_SELF_REVIEW, tokens_used)
        variants = self.store.list_variants()
        return {
            "brief_id": brief_id,
            "model": route.model,
            "tokens_used": tokens_used,
            "variants_reviewed": len(variants),
            "rounds": 1,  # 明确只做一轮
        }
