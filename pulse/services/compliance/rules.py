"""规则引擎与内置规则（所有者：A4，任务包 W2-A4-1/2/3/4）。

引擎本身很薄：把 `VariantView` 依次喂给规则，收集结构化 `Finding`。
规则集是**数据驱动的**（词表来自 `data/*.json`），逻辑只负责匹配与判定。

严重度口径（契约 §3.5）：

* `block`：硬拦截，默认不可绕过（豁免需管理员权限）；
* `warn`：可人工豁免，但**必须留痕**（写入 `waived_by`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from pulse.services.compliance.lexicon import (
    SensitiveLexicon,
    content_policies,
    sensitive_lexicon,
)
from pulse.services.compliance.models import Finding, VariantView, position
from pulse.shared.enums import ComplianceSeverity, LicenseStatus

#: 规则函数签名
RuleFn = Callable[[VariantView], Iterable[Finding]]


@dataclass(frozen=True, slots=True)
class Rule:
    """一条规则：ID + 默认严重度 + 说明 + 实现。"""

    id: str
    severity: ComplianceSeverity
    description: str
    check: RuleFn


#: 数字/规格类表述：出现就必须人工确认"能不能对外披露"（不编造，但也不能悄悄发）
_SPEC_PATTERN = re.compile(
    r"(\b\d+(?:\.\d+)?\s*(?:mm|cm|m|kg|t|ton|tons|tonnes|kg/mm2|mpa|hrc|hb)\b)"
    r"|(\b(?:ht200|ht250|qt400|qt450|qt500|qt600|gg25|ggg40|en-gjl-\d+|en-gjs-\d+)\b)"
    r"|(\d+(?:\.\d+)?\s*(?:吨|毫米|公斤|兆帕))",
    re.IGNORECASE,
)

#: emoji 粗略识别（与内容和媒体域保持同一思路：只用于计数，不做语义判断）
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]"
)


# --------------------------------------------------------------------------
# W2-A4-2 版权 / 素材授权
# --------------------------------------------------------------------------


def rule_media_license(view: VariantView) -> Iterable[Finding]:
    """素材授权：`pending` 直接拦；缺失也拦（无法追溯来源）。"""
    findings: list[Finding] = []
    for index, item in enumerate(view.media):
        status = str(item.license_status or "").strip().lower()
        if status == LicenseStatus.PENDING.value:
            findings.append(
                Finding(
                    rule="copyright.license_pending",
                    severity=ComplianceSeverity.BLOCK,
                    message=(
                        f"素材 {index + 1} 的授权状态为 pending：外部素材须先完成授权登记才能入库发布"
                    ),
                    position={"field": f"media[{index}].license_status", "value": status},
                )
            )
        elif status not in (LicenseStatus.OWNED.value, LicenseStatus.LICENSED.value):
            findings.append(
                Finding(
                    rule="copyright.license_missing",
                    severity=ComplianceSeverity.BLOCK,
                    message=(
                        f"素材 {index + 1} 的授权状态不明（{status or '空'}）："
                        "产品图必须是自有实拍或已授权素材"
                    ),
                    position={"field": f"media[{index}].license_status", "value": status},
                )
            )
    return findings


# --------------------------------------------------------------------------
# W2-A4-3 敏感词（B2B 工业向最小集，词表在 data/sensitive_words.json）
# --------------------------------------------------------------------------


def _scan_terms(
    text: str,
    *,
    field_name: str,
    terms: Sequence[Any],
    rule_id: str,
    severity: ComplianceSeverity,
) -> Iterable[Finding]:
    lowered = text.lower()
    for item in terms:
        term = str(getattr(item, "term", "") or "")
        if not term or term.lower() not in lowered:
            continue
        reason = str(getattr(item, "reason", "") or "")
        yield Finding(
            rule=rule_id,
            severity=severity,
            message=f"命中敏感词「{term}」" + (f"：{reason}" if reason else ""),
            position=position(field_name, text, term),
        )


def rule_sensitive_words(view: VariantView, lexicon: SensitiveLexicon | None = None) -> Iterable[Finding]:
    """敏感词：`block` 级拦，`warn` 级提示。"""
    table = lexicon or sensitive_lexicon()
    for field_name, text in (("text", view.text), ("title", view.title or "")):
        if not text:
            continue
        yield from _scan_terms(
            text,
            field_name=field_name,
            terms=table.block,
            rule_id="sensitive.blocked_term",
            severity=ComplianceSeverity.BLOCK,
        )
        yield from _scan_terms(
            text,
            field_name=field_name,
            terms=table.warn,
            rule_id="sensitive.review_term",
            severity=ComplianceSeverity.WARN,
        )


# --------------------------------------------------------------------------
# W2-A4-4 平台政策与质量规则
# --------------------------------------------------------------------------


def rule_banned_phrases(view: VariantView) -> Iterable[Finding]:
    """禁用词：承诺性表述拦（法律风险），无法证实的形容词只提示。"""
    policies = content_policies()
    for field_name, text in (("text", view.text), ("title", view.title or "")):
        if not text:
            continue
        lowered = text.lower()
        for phrase in policies.banned_block:
            if phrase.lower() in lowered:
                yield Finding(
                    rule="claims.guarantee",
                    severity=ComplianceSeverity.BLOCK,
                    message=f"出现承诺性表述「{phrase}」：不得对客户做零封号/保证类承诺",
                    position=position(field_name, text, phrase),
                )
        for phrase in policies.banned_warn:
            if phrase.lower() in lowered:
                yield Finding(
                    rule="claims.unverifiable_adjective",
                    severity=ComplianceSeverity.WARN,
                    message=f"出现无法证实的形容词「{phrase}」：请改为可核验的事实表述",
                    position=position(field_name, text, phrase),
                )


def rule_unverified_specs(view: VariantView) -> Iterable[Finding]:
    """硬参数披露：出现材质/尺寸/重量等数字时必须人工确认可公开。"""
    for match in _SPEC_PATTERN.finditer(view.text or ""):
        token = match.group(0)
        yield Finding(
            rule="claims.unverified_spec",
            severity=ComplianceSeverity.WARN,
            message=(
                f"出现规格/参数类表述「{token}」：确属可公开的真实数据才可保留，"
                "否则改为不涉及数字的表述"
            ),
            position=position("text", view.text, token, index=match.start()),
        )


def rule_platform_quality(view: VariantView) -> Iterable[Finding]:
    """字数 / 标签数 / emoji 上限：按平台口径提示（口径在 platform_policies.json）。"""
    policy = content_policies().platform(view.platform)
    if policy is None:
        return
    length = len(view.text or "")
    if policy.min_chars is not None and length < policy.min_chars:
        yield Finding(
            rule="quality.length_below_min",
            severity=ComplianceSeverity.WARN,
            message=f"正文 {length} 字符，低于 {view.platform} 的下限 {policy.min_chars}",
            position={"field": "text", "offset": 0, "length": length, "excerpt": ""},
        )
    if policy.max_chars is not None and length > policy.max_chars:
        yield Finding(
            rule="quality.length_above_max",
            severity=ComplianceSeverity.WARN,
            message=f"正文 {length} 字符，超过 {view.platform} 的上限 {policy.max_chars}",
            position={"field": "text", "offset": policy.max_chars, "length": length, "excerpt": ""},
        )
    tag_count = len(view.hashtags)
    if tag_count < policy.hashtag_min:
        yield Finding(
            rule="quality.hashtag_too_few",
            severity=ComplianceSeverity.WARN,
            message=f"标签 {tag_count} 个，少于 {view.platform} 建议的 {policy.hashtag_min} 个",
            position={"field": "hashtags", "offset": tag_count, "length": 0, "excerpt": ""},
        )
    if tag_count > policy.hashtag_max:
        yield Finding(
            rule="quality.hashtag_too_many",
            severity=ComplianceSeverity.WARN,
            message=f"标签 {tag_count} 个，超过 {view.platform} 上限 {policy.hashtag_max} 个",
            position={"field": "hashtags", "offset": policy.hashtag_max, "length": tag_count, "excerpt": ""},
        )
    emoji_count = len(_EMOJI_PATTERN.findall(view.text or ""))
    if emoji_count > policy.emoji_max:
        yield Finding(
            rule="quality.emoji_too_many",
            severity=ComplianceSeverity.WARN,
            message=f"emoji {emoji_count} 个，超过 {view.platform} 上限 {policy.emoji_max} 个",
            position={"field": "text", "offset": 0, "length": 0, "excerpt": ""},
        )


def rule_hashtag_shape(view: VariantView) -> Iterable[Finding]:
    """标签规范性：必须含 `#`、不含空格（契约 §2 的硬要求）。"""
    for tag in view.hashtags:
        if not str(tag).startswith("#"):
            yield Finding(
                rule="quality.hashtag_missing_hash",
                severity=ComplianceSeverity.WARN,
                message=f"标签「{tag}」缺少 # 前缀",
                position={"field": "hashtags", "value": str(tag)},
            )
        if " " in str(tag):
            yield Finding(
                rule="quality.hashtag_has_space",
                severity=ComplianceSeverity.WARN,
                message=f"标签「{tag}」含空格",
                position={"field": "hashtags", "value": str(tag)},
            )


def rule_machine_flavor(view: VariantView) -> Iterable[Finding]:
    """反机器味：模板腔开场白、连续感叹号积堆。"""
    policies = content_policies()
    text = view.text or ""
    lowered = text.lower()
    for opener in policies.flavor_openers:
        if opener.lower() in lowered:
            yield Finding(
                rule="quality.machine_flavor_opener",
                severity=ComplianceSeverity.WARN,
                message=f"开场白「{opener}」是典型模板腔：B2B 内容第一行应当是对方的痛点或利益",
                position=position("text", text, opener),
            )
    bangs = len(re.findall(r"!{2,}|！{2,}", text))
    if bangs > policies.max_repeated_exclamation:
        yield Finding(
            rule="quality.machine_flavor_bangs",
            severity=ComplianceSeverity.WARN,
            message=f"连续感叹号出现 {bangs} 次，超过上限 {policies.max_repeated_exclamation} 次",
            position={"field": "text", "count": bangs},
        )


# --------------------------------------------------------------------------
# 规则注册表
# --------------------------------------------------------------------------

#: 内置规则集。**每个 rule 函数只登记一次**——同一个函数登记两次会让 finding 成对出现
#: （每次调用都各自产出，ID 不同、内容一样），豁免时会出现"豁免了一条还剩一条"。
#: 一条规则函数可以产出多个 rule id（例如版权规则同时管 pending 与状态不明），
#: 因此 `Rule.id` 只是该函数的代表 id 与说明锚点，finding 自带真实 rule id。
#: `first_time_account` 这类场景标记由服务层判读，不在这里写死。
RULES: tuple[Rule, ...] = (
    Rule(
        id="copyright.media_license",
        severity=ComplianceSeverity.BLOCK,
        description="素材授权：pending 或状态不明一律硬拦截",
        check=rule_media_license,
    ),
    Rule(
        id="sensitive.terms",
        severity=ComplianceSeverity.BLOCK,
        description="敏感词：block 级拦截 / warn 级提示（词表在 data/sensitive_words.json）",
        check=rule_sensitive_words,
    ),
    Rule(
        id="claims.banned_phrases",
        severity=ComplianceSeverity.BLOCK,
        description="禁用词：承诺性表述拦截 / 无法证实的形容词提示",
        check=rule_banned_phrases,
    ),
    Rule(
        id="claims.unverified_spec",
        severity=ComplianceSeverity.WARN,
        description="硬参数披露需人工确认",
        check=rule_unverified_specs,
    ),
    Rule(
        id="quality.platform",
        severity=ComplianceSeverity.WARN,
        description="字数 / 标签数 / emoji 与平台口径不符",
        check=rule_platform_quality,
    ),
    Rule(
        id="quality.hashtag_shape",
        severity=ComplianceSeverity.WARN,
        description="标签缺少 # 或含空格",
        check=rule_hashtag_shape,
    ),
    Rule(
        id="quality.machine_flavor",
        severity=ComplianceSeverity.WARN,
        description="模板腔/机器味提示",
        check=rule_machine_flavor,
    ),
)


@dataclass(frozen=True, slots=True)
class RuleEngine:
    """把规则集跑一遍的薄引擎。"""

    rules: tuple[Rule, ...] = RULES

    def evaluate(self, view: VariantView) -> tuple[Finding, ...]:
        """返回全部 finding，**按严重度排序**（block 在前，便于审批台优先展示）。"""
        collected: list[Finding] = []
        for rule in self.rules:
            collected.extend(rule.check(view))
        order = {ComplianceSeverity.BLOCK: 0, ComplianceSeverity.WARN: 1}
        return tuple(sorted(collected, key=lambda item: (order.get(item.severity, 9), item.rule)))

    def rule_ids(self) -> tuple[str, ...]:
        return tuple(rule.id for rule in self.rules)
