"""W1-A1-3：平台 Prompt 模板（含与媒体域规格的一致性比对）。"""

from __future__ import annotations

import pytest

from pulse.services.content.brief import parse_brief
from pulse.services.content.errors import UnknownPlatformError
from pulse.services.content.prompts import (
    PROMPT_TEMPLATES,
    missing_options,
    prompt_template,
    render_prompt,
    supported_platforms,
    template_as_dict,
)
from pulse.services.media.captions import CAPTION_SPECS


def _brief():
    return parse_brief(
        {
            "topic": "铸件 + 粗加工一体化",
            "target_audience": "印度机床厂采购",
            "platforms": ["linkedin"],
            "extra_terms": ["床身"],
        }
    )


def test_linkedin_and_youtube_are_the_p0a_templates():
    assert PROMPT_TEMPLATES["linkedin"].priority == "P0-A"
    assert PROMPT_TEMPLATES["youtube"].priority == "P0-A"
    assert supported_platforms()[:2] == ("linkedin", "youtube")


def test_every_platform_in_enum_has_a_template():
    from pulse.shared.enums import Platform

    assert set(PROMPT_TEMPLATES) == {item.value for item in Platform}


def test_render_prompt_contains_structure_limits_and_rules():
    text = render_prompt("linkedin", _brief())

    assert "钩子行" in text
    assert "600–1200 字符" in text
    assert "3–5 个" in text
    assert "links" not in text  # 不该出现英文残留
    assert "第一条评论" in text
    assert "严禁编造材质牌号" in text


def test_render_prompt_puts_evidence_gaps_as_hard_constraint():
    text = render_prompt(
        "linkedin",
        _brief(),
        media_brief="1. 位次1｜文件 IMG_0001.jpg｜品类 加工件/壳体｜画面 成品与机床同框",
        evidence_gaps=("TODO(need-real-data)：位次3 没有可用素材",),
    )

    assert "证据缺口——不得编造" in text
    assert "TODO(need-real-data)：位次3 没有可用素材" in text
    assert "IMG_0001.jpg" in text


def test_render_prompt_for_youtube_requires_title_field():
    text = render_prompt("youtube", _brief())

    assert "title（标题）" in text
    assert "100 字符" in text


def test_unknown_platform_raises():
    with pytest.raises(UnknownPlatformError) as exc:
        prompt_template("tiktok")

    assert "tiktok" in str(exc.value)


def test_template_specs_match_media_domain_caption_specs():
    """A1 的模板不得与媒体域的 CAPTION_SPECS 漂移（同一份报告的两个落点）。"""
    for key, spec in CAPTION_SPECS.items():
        template = PROMPT_TEMPLATES.get(key)
        if template is None:
            # 媒体域还留着 TikTok 的规格（历史遗留，本项目暂不投入内容生成）
            continue
        assert (template.min_chars, template.max_chars) == (spec.min_chars, spec.max_chars), key
        assert (template.hashtag_min, template.hashtag_max) == (
            spec.hashtag_min,
            spec.hashtag_max,
        ), key
        assert template.emoji_max == spec.emoji_max, key


def test_missing_options_reports_all_gaps():
    assert missing_options("linkedin", {}) == ["author_urn", "linkedin_visibility"]
    assert missing_options("linkedin", {"author_urn": "urn:li:organization:1"}) == [
        "linkedin_visibility"
    ]
    assert missing_options("instagram", {}) == []


def test_template_as_dict_is_json_friendly():
    payload = template_as_dict("vk")

    assert payload["platform"] == "vk"
    assert isinstance(payload["structure"], list)
    assert payload["required_options"] == ["owner_id"]
