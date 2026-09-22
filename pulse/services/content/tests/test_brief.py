"""W1-A1-2：brief 解析与非法输入。"""

from __future__ import annotations

import pytest

from pulse.services.content.brief import DEFAULT_TOKEN_BUDGET, parse_brief
from pulse.services.content.errors import BriefValidationError
from pulse.shared.enums import Platform


def _valid_payload() -> dict:
    return {
        "topic": "消失模铸造 + 粗加工一体化产能",
        "target_audience": "印度机床整机厂的采购与供应链负责人",
        "platforms": ["linkedin", "youtube"],
    }


def test_parse_brief_happy_path():
    brief = parse_brief(_valid_payload())

    assert brief.platforms == (Platform.LINKEDIN, Platform.YOUTUBE)
    assert brief.token_budget == DEFAULT_TOKEN_BUDGET
    assert brief.brief_id.startswith("b_")
    assert brief.title == brief.topic
    assert "印度机床整机厂" in brief.body_seed


def test_parse_brief_platform_is_case_insensitive_and_deduped():
    payload = _valid_payload() | {"platforms": ["LinkedIn", "linkedin", "VK"]}

    brief = parse_brief(payload)

    assert brief.platforms == (Platform.LINKEDIN, Platform.VK)


def test_parse_brief_defaults_are_explicit():
    brief = parse_brief(_valid_payload())

    assert brief.budget_media == 3
    assert brief.language == "en"
    assert brief.brand_guide_id is None
    assert brief.extra_terms == ()
    assert brief.cta is None


def test_parse_brief_rejects_tiktok_with_reason():
    payload = _valid_payload() | {"platforms": ["linkedin", "tiktok"]}

    with pytest.raises(BriefValidationError) as exc:
        parse_brief(payload)

    message = str(exc.value)
    assert "TikTok" in message
    assert "暂不投入" in message


def test_parse_brief_reports_all_errors_at_once():
    payload = {
        "topic": "   ",
        "target_audience": "",
        "platforms": [],
        "language": "th",
        "budget_media": -1,
        "token_budget": 10,
    }

    with pytest.raises(BriefValidationError) as exc:
        parse_brief(payload)

    errors = exc.value.errors
    assert len(errors) >= 6
    assert any("topic" in item for item in errors)
    assert any("target_audience" in item for item in errors)
    assert any("platforms" in item for item in errors)
    assert any("language" in item for item in errors)
    assert any("budget_media" in item for item in errors)
    assert any("token_budget" in item for item in errors)


def test_parse_brief_rejects_unknown_fields():
    payload = _valid_payload() | {"tone": "轻快"}

    with pytest.raises(BriefValidationError) as exc:
        parse_brief(payload)

    assert any("tone" in item for item in exc.value.errors)


def test_parse_brief_rejects_non_mapping():
    with pytest.raises(BriefValidationError):
        parse_brief(["linkedin"])  # type: ignore[arg-type]


def test_parse_brief_validates_brief_id_prefix():
    payload = _valid_payload() | {"brief_id": "src_123"}

    with pytest.raises(BriefValidationError) as exc:
        parse_brief(payload)

    assert any("b_" in item for item in exc.value.errors)


def test_parse_brief_accepts_extra_terms_and_cta():
    payload = _valid_payload() | {"extra_terms": ["床身", "  立柱  "], "cta": "索取产能表"}

    brief = parse_brief(payload)

    assert brief.extra_terms == ("床身", "立柱")
    assert brief.cta == "索取产能表"
    assert "索取产能表" in brief.body_seed
