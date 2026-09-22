"""W2-A6-5 · 生成质量回归框架（固定 brief 集打分 + 一轮基线）。

派工单的说法是「框架可跑，**不追求真实生成质量**」，所以这里刻意分成两层：

* **硬规则**（必须全过，与模型好坏无关）：标签在白名单内且数量在模板区间、
  没有无法证实的形容词（复用媒体域的 ``BANNED_PHRASES`` 作为单一真源）、
  没有编造的材质/公差/单重等参数数字、外发文案不含中文对照、正文不放链接。
* **打分维度**（0–100，允许不完美，但要能对比）：长度贴合、标签贴合、语气干净。

用法（换模型/改 prompt 后跑一遍，看基线有没有退化）::

    # 1) 跑基线，确认当前分数
    python -m pytest pulse/tests/test_generation_quality_regression.py
    # 2) 有意改动生成逻辑后，重新记录基线（人工确认分数变化合理）
    $env:PULSE_UPDATE_BASELINE=1; python -m pytest pulse/tests/test_generation_quality_regression.py

框架**不调真实模型**：默认跑 ``StubCopywriter``；``ScriptedCopywriter`` 用来模拟
"换了一个模型/提示词之后"的输出，验证这套硬规则真的能拦住坏文案。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pulse.services.content import (
    ALLOWED_TAGS,
    AccountBinding,
    ContentService,
    CopyDraft,
    CopyRequest,
    StubCopywriter,
    prompt_template,
)
from pulse.services.media.captions import BANNED_PHRASES
from pulse.shared.enums import MediaKind, Platform

from pulse.tests.conftest import make_media_item

#: 基线文件（人工确认后才允许更新）
BASELINE_PATH = Path(__file__).resolve().parent / "baselines" / "generation_baseline.json"

#: 更新基线的开关（避免"跑测试顺手把基线改了"这种自欺）
UPDATE_ENV = "PULSE_UPDATE_BASELINE"

#: 固定 brief 集（不引外部数据；产能/公差等硬参数一律不写，留给 TODO 占位）
FIXED_BRIEFS: tuple[dict[str, Any], ...] = (
    {
        "brief_id": "b_a6_reg_0001",
        "topic": "Valve body castings with pre-machining for hydraulic OEMs",
        "target_audience": "hydraulic equipment OEMs in India sourcing castings",
        "platforms": ["linkedin"],
        "cta": "Send a drawing for a casting-plus-machining quote",
        "extra_terms": ["#casting", "#machining"],
    },
    {
        "brief_id": "b_a6_reg_0002",
        "topic": "Machine tool bed castings: from pattern to pre-machined part",
        "target_audience": "machine tool builders in Taiwan and the United States",
        "platforms": ["youtube"],
        "cta": "Ask for a process walkthrough",
    },
    {
        "brief_id": "b_a6_reg_0003",
        "topic": "Pump housing castings and export packing",
        "target_audience": "pump manufacturers reviewing new casting suppliers",
        "platforms": ["facebook"],
    },
    {
        "brief_id": "b_a6_reg_0004",
        "topic": "Iron castings with pre-machining for machine builders",
        "target_audience": "machine builders in Russia and CIS",
        "platforms": ["vk"],
    },
)

#: (brief 序号, 平台, 人工提供的视频素材) —— 只有 YouTube 必须带视频（契约 §2）
FIXED_CASES: tuple[tuple[int, str, bool], ...] = (
    (0, "linkedin", False),
    (1, "youtube", True),
    (2, "facebook", False),
    (3, "vk", False),
)

#: 账号事实（URN / 主页 ID / 社区 ID）必须显式给出，不允许生成器编
PLATFORM_FACTS: dict[str, dict[str, Any]] = {
    "linkedin": {"author_urn": "urn:li:organization:10086"},
    "youtube": {"privacy_status": "public", "category_id": "28", "made_for_kids": False},
    "facebook": {"page_id": "famed-page"},
    "vk": {"owner_id": -123456},
}

#: "编造硬参数"的探测式：数字 + 工业单位（没有真实数据来源时出现即为编造）
_NUMBER_WITH_UNIT = re.compile(
    r"\b\d[\d.,]*\s*(?:mm|cm|m|kg|t|tons?|MPa|HRC|HB|µm|um|%|pcs?|hours?|days?)\b",
    re.IGNORECASE,
)

#: 外发文案里不允许出现中文（中文只用于审校对照，出稿口径见契约 §2）
_CJK = re.compile(r"[\u4e00-\u9fff]")

#: 正文不许放链接的平台（契约 §2 的 link_rule）
_NO_LINK_PLATFORMS = {"linkedin", "facebook", "instagram", "reddit"}
_LINK = re.compile(r"https?://|www\.", re.IGNORECASE)


@dataclass(frozen=True)
class CaseScore:
    """单个 (brief, 平台) 的打分结果。"""

    brief_id: str
    platform: str
    text_len: int
    hashtags: tuple[str, ...]
    title_len: int
    score: float
    hard_failures: tuple[str, ...]

    def as_baseline(self) -> dict[str, Any]:
        """落到基线文件里的形态（浮点保留三位，避免无意义的抖动）。"""
        return {
            "brief_id": self.brief_id,
            "platform": self.platform,
            "text_len": self.text_len,
            "hashtags": list(self.hashtags),
            "title_len": self.title_len,
            "score": round(self.score, 3),
            "hard_failures": list(self.hard_failures),
        }


def score_draft(
    *,
    brief_id: str,
    platform: str,
    text: str,
    text_zh: str | None = None,
    title: str | None = None,
    hashtags: tuple[str, ...] = (),
    has_media: bool = True,
) -> CaseScore:
    """对一段草稿打分（纯函数：不依赖 ContentService，便于单独测框架本身）。"""
    template = prompt_template(platform)
    failures: list[str] = []

    if not text.strip():
        failures.append("正文为空")
    lowered = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase.lower() in lowered:
            failures.append(f"出现无法证实的形容词：{phrase}")
    if _NUMBER_WITH_UNIT.search(text):
        failures.append("出现未经证实的硬参数数字（材质/公差/单重/产能等）")
    if _CJK.search(text):
        failures.append("外发文案里混入中文（中文只用于审校对照）")
    if platform in _NO_LINK_PLATFORMS and _LINK.search(text):
        failures.append(f"{platform} 的正文不允许放链接（契约 §2 link_rule）")

    count = len(hashtags)
    if count < template.hashtag_min or count > template.hashtag_max:
        failures.append(
            f"标签数量 {count} 不在模板区间 {template.hashtag_min}–{template.hashtag_max}"
        )
    for tag in hashtags:
        if not tag.startswith("#") or " " in tag:
            failures.append(f"标签形态不合法：{tag!r}")
        if tag not in ALLOWED_TAGS:
            failures.append(f"标签不在白名单内：{tag!r}")
    if template.content_type == "video" and not title:
        failures.append("视频平台必须给出标题")
    if not has_media and "TODO(need-real-data)" not in text and platform in {"linkedin", "youtube"}:
        failures.append("没有素材时必须在正文里留下 TODO(need-real-data) 占位")

    # ---- 打分维度（允许不完美，但要能对比） ----
    if template.min_chars is None:
        length_fit = 1.0 if len(text) <= (template.max_chars or len(text)) else 0.0
    elif len(text) < template.min_chars:
        length_fit = max(0.0, len(text) / template.min_chars)
    elif template.max_chars is not None and len(text) > template.max_chars:
        length_fit = max(0.0, template.max_chars / len(text))
    else:
        length_fit = 1.0

    upper = template.hashtag_max or 1
    if template.hashtag_min <= count <= upper:
        hashtag_fit = 1.0
    else:
        hashtag_fit = max(0.0, 1.0 - abs(count - template.hashtag_min) / max(upper, 1))

    tone_clean = 1.0 if not any("形容词" in item for item in failures) else 0.0
    score = 100 * (0.5 * length_fit + 0.25 * hashtag_fit + 0.25 * tone_clean)

    return CaseScore(
        brief_id=brief_id,
        platform=platform,
        text_len=len(text),
        hashtags=tuple(hashtags),
        title_len=len(title or ""),
        score=round(score, 3),
        hard_failures=tuple(failures),
    )


def binding_for(platform: str) -> AccountBinding:
    """按平台给出账号绑定（账号事实显式传入，生成器不许编 ID）。"""
    return AccountBinding(
        account_id="acct_a6_regression",
        platform=Platform(platform),
        options=PLATFORM_FACTS[platform],
    )


def run_regression(copywriter: Any | None = None) -> dict[str, Any]:
    """跑完固定 brief 集，返回 ``{"cases": [...], "score_avg": ..., "model": ...}``。"""
    service = ContentService(copywriter=copywriter or StubCopywriter())
    cases: list[CaseScore] = []

    for index, platform, needs_video in FIXED_CASES:
        brief = FIXED_BRIEFS[index]
        service.submit_brief(brief)
        media = (
            (
                make_media_item(
                    kind=MediaKind.VIDEO,
                    url="s3://pulse-media/a6/regression.mp4",
                    width=None,
                    height=None,
                    duration_s=96.0,
                ),
            )
            if needs_video
            else ()
        )
        derived = service.generate_variant(
            brief["brief_id"],
            platform,
            account=binding_for(platform),
            media=media or None,
            scheduled_at=None,
        )
        cases.append(
            score_draft(
                brief_id=brief["brief_id"],
                platform=platform,
                text=derived.post.caption.text,
                text_zh=derived.post.caption.text_zh,
                title=derived.post.title,
                hashtags=derived.post.hashtags,
                has_media=bool(derived.post.media),
            )
        )

    scores = [case.score for case in cases]
    return {
        "model": type(copywriter or StubCopywriter()).__name__,
        "cases": [case.as_baseline() for case in cases],
        "score_avg": round(sum(scores) / len(scores), 3),
        "score_min": min(scores),
    }


class ScriptedCopywriter:
    """按脚本返回固定文案的生成器——用来模拟"换了模型之后"的输出。"""

    def __init__(
        self,
        text: str,
        *,
        hashtags: tuple[str, ...] = ("#casting", "#machining", "#famed"),
        title: str = "Scripted title",
        tokens_used: int = 1_200,
    ) -> None:
        self.text = text
        self.hashtags = hashtags
        self.title = title
        self.tokens_used = tokens_used
        self.requests: list[CopyRequest] = []

    def write(self, request: CopyRequest) -> CopyDraft:
        self.requests.append(request)
        return CopyDraft(
            text=self.text,
            text_zh="【中文对照】脚本化输出",
            title=self.title,
            hashtags=self.hashtags,
            tokens_used=self.tokens_used,
            ai_score=0.5,
        )


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------


def test_fixed_brief_set_passes_all_hard_rules():
    """固定 brief 集跑一轮：硬规则必须全过。

    A1 内容域缺口 #3（桩正文混入中文）已于 2026-09-22 修复：
    ``slot_terms_from_selection`` 现在缺素材时只留英文占位符 ``TODO(need-real-data)``，
    中文说明改由 ``missing_slot_gaps`` 以缺口形式给出。因此这里不再放行任何硬规则违规。
    """
    report = run_regression()

    assert len(report["cases"]) == len(FIXED_CASES)
    broken = {
        (case["brief_id"], case["platform"]): list(case["hard_failures"])
        for case in report["cases"]
    }
    assert not any(broken.values()), f"硬规则未通过：{broken}"
    assert report["score_avg"] > 0


def test_stub_outbound_text_is_chinese_free():
    """出稿口径：``caption.text`` 里不应出现中文（中文只用于审校对照）。

    2026-09-22 修复 F-4：桩正文的缺素材占位符改为纯英文，中文说明走缺口字段。
    """
    report = run_regression()
    offenders = [
        case["platform"]
        for case in report["cases"]
        if any("混入中文" in item for item in case["hard_failures"])
    ]
    assert offenders == [], f"以下平台的桩正文混入了中文：{offenders}"


def test_regression_is_deterministic():
    """同一份代码跑两轮必须一模一样，否则基线没有意义（桩不许用随机数）。"""
    assert run_regression() == run_regression()


def test_against_recorded_baseline():
    """与基线逐字段比对；分数或文案长度变了就必须人工确认后重记基线。"""
    current = run_regression()

    # 基线缺失、或显式要求重记（PULSE_UPDATE_BASELINE=1）时写入并跳过比对。
    # 修于 2026-09-22：原先只在"文件不存在"时才认这个环境变量，
    # 导致文案有意改动后无法按提示重记基线（只能手删文件）。
    if os.environ.get(UPDATE_ENV) == "1":
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        pytest.skip(f"已写入基线：{BASELINE_PATH}")

    if not BASELINE_PATH.exists():
        pytest.fail(
            f"缺少基线文件 {BASELINE_PATH}；确认当前分数合理后运行："
            f"$env:{UPDATE_ENV}=1; python -m pytest pulse/tests/test_generation_quality_regression.py"
        )

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert baseline["cases"] == current["cases"], (
        "生成结果相对基线发生了变化——确认是否属于有意改动；"
        f"如是，请用 {UPDATE_ENV}=1 重记基线"
    )
    assert baseline["score_avg"] == current["score_avg"]


def test_framework_actually_catches_a_bad_draft():
    """框架必须真的能拦住坏文案——拦不住的框架等于没有框架。"""
    bad = ScriptedCopywriter(
        "We are a world-class leading manufacturer. Tolerance ±0.05 mm, 5000 tons per month. "
        "中文对照混进正文。https://example.test/quote",
        hashtags=("#casting", "#not_in_whitelist"),
    )
    report = run_regression(copywriter=bad)
    failures = {case["platform"]: case["hard_failures"] for case in report["cases"]}

    for platform in ("linkedin", "facebook", "vk"):
        assert failures[platform], f"{platform} 的坏文案竟然通过了硬规则"
        joined = " ".join(failures[platform])
        assert "形容词" in joined
        assert "硬参数" in joined
        assert "中文" in joined
        assert "白名单" in joined
    assert "链接" in " ".join(failures["linkedin"])
    assert report["score_avg"] < 100


def test_scripted_copywriter_receives_the_rendered_prompt():
    """框架同时守 prompt 的可用性：渲染后的提示词要带上 brief、结构与硬约束。"""
    scripted = ScriptedCopywriter(
        "Casting plus pre-machining from one plant. TODO(need-real-data)."
    )
    run_regression(copywriter=scripted)

    assert len(scripted.requests) == len(FIXED_CASES), "每个固定用例都应渲染一次提示词"
    for request, (_, platform, _) in zip(scripted.requests, FIXED_CASES):
        assert request.platform == platform
        assert "【本次 brief】" in request.prompt
        assert "【结构（严格按顺序）】" in request.prompt
        assert "严禁编造材质牌号、公差、单重、月产能" in request.prompt
        assert prompt_template(platform).structure[0] in request.prompt
