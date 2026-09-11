"""按平台短文生成测试：规格对齐报告、校验规则、模型调用与解析。"""

from __future__ import annotations

import json

import httpx
import pytest

from pulse.services.media.captions import (
    BANNED_PHRASES,
    CAPTION_SPECS,
    CaptionSpec,
    caption_spec,
    count_emoji,
    generate_caption,
    validate_caption,
)
from pulse.services.media.describe import VisionConfig


def _material(**overrides):
    base = {
        "file_name": "IMG_1.jpg",
        "category": "加工件/机床件",
        "summary": "大型机床床身导轨面加工",
        "keywords": ("机床床身", "导轨面", "机加工"),
        "is_video": False,
    }
    base.update(overrides)

    class _M:
        pass

    item = _M()
    for key, value in base.items():
        setattr(item, key, value)
    return item


# ---------- 规格：必须与报告 §2.1 / §3 一致 ----------


def test_specs_cover_four_platforms() -> None:
    assert set(CAPTION_SPECS) == {"linkedin", "facebook", "tiktok", "vk"}


def test_spec_structure_matches_report() -> None:
    assert CAPTION_SPECS["linkedin"].structure == (
        "钩子行", "痛点", "能力证据（分点）", "产品范围", "CTA", "标签",
    )
    assert CAPTION_SPECS["facebook"].structure[0] == "场景描述"
    assert CAPTION_SPECS["facebook"].structure[-2] == "提问收尾"
    assert CAPTION_SPECS["tiktok"].structure == ("钩子（首行）", "一句结论", "标签")
    assert CAPTION_SPECS["vk"].structure == ("企业介绍", "工艺", "产品", "设备", "合作方式")


def test_spec_lengths_and_tags_match_report() -> None:
    assert (CAPTION_SPECS["linkedin"].min_chars, CAPTION_SPECS["linkedin"].max_chars) == (600, 1200)
    assert (CAPTION_SPECS["facebook"].min_chars, CAPTION_SPECS["facebook"].max_chars) == (150, 400)
    assert CAPTION_SPECS["tiktok"].max_chars == 150
    assert (CAPTION_SPECS["vk"].min_chars, CAPTION_SPECS["vk"].max_chars) == (500, 1500)
    assert CAPTION_SPECS["facebook"].hashtag_max == 2, "报告：FB 标签只 1–2 个"
    assert CAPTION_SPECS["tiktok"].hashtag_min == 4, "报告：TikTok 4–5 个"


def test_link_rule_per_platform() -> None:
    assert CAPTION_SPECS["linkedin"].needs_first_comment is True
    assert CAPTION_SPECS["facebook"].needs_first_comment is True
    assert CAPTION_SPECS["tiktok"].needs_first_comment is False
    assert "正文" in CAPTION_SPECS["vk"].link_rule


def test_vk_is_the_only_russian_platform() -> None:
    assert CAPTION_SPECS["vk"].language == "ru"
    assert {spec.language for key, spec in CAPTION_SPECS.items() if key != "vk"} == {"en"}


def test_caption_spec_lookup() -> None:
    assert caption_spec("LinkedIn").key == "linkedin"
    assert caption_spec(" unknown ") is None


# ---------- 校验 ----------


def _spec(**overrides) -> CaptionSpec:
    base = {
        "key": "x",
        "name": "X",
        "language": "en",
        "min_chars": 10,
        "max_chars": 50,
        "hashtag_min": 1,
        "hashtag_max": 2,
        "emoji_max": 1,
        "fold_chars": 20,
        "structure": ("a", "b"),
        "link_rule": "链接放评论",
    }
    base.update(overrides)
    return CaptionSpec(**base)  # type: ignore[arg-type]


def _check(checks, name: str):
    return next(item for item in checks if item.name == name)


def test_validate_flags_length() -> None:
    checks, _ = validate_caption(_spec(), "太短", ("#casting",))
    assert _check(checks, "长度").ok is False
    assert "偏短" in _check(checks, "长度").detail


def test_validate_flags_hashtag_count_and_shape() -> None:
    checks, _ = validate_caption(_spec(), "x" * 20, ("#a", "#b", "#c"))
    assert _check(checks, "标签数").ok is False

    checks, _ = validate_caption(_spec(), "x" * 20, ("casting",))
    assert _check(checks, "标签规范").ok is False


def test_validate_flags_banned_phrases() -> None:
    checks, warnings = validate_caption(
        _spec(), "We are a world-class foundry with best price", ("#casting",)
    )
    assert _check(checks, "禁用语").ok is False
    assert any("禁用语" in item for item in warnings)
    assert "world-class" in BANNED_PHRASES


def test_validate_flags_unverified_specs() -> None:
    """不伪造数据：编出来的材质/公差/月产能必须被标出。"""
    checks, warnings = validate_caption(
        _spec(max_chars=200), "材质 HT250，单重 2.5 吨，月产能 300 件。", ("#casting",)
    )
    assert _check(checks, "未核实参数").ok is False
    assert any("未核实" in item or "规格类" in item for item in warnings)


def test_validate_flags_link_in_body_for_linkedin() -> None:
    checks, _ = validate_caption(
        _spec(needs_first_comment=True),
        "Casting plus machining, see https://example.com for details",
        ("#casting",),
    )
    assert _check(checks, "外链位置").ok is False


def test_validate_passes_when_linkedin_body_has_no_link() -> None:
    checks, _ = validate_caption(
        _spec(needs_first_comment=True), "x" * 20, ("#casting",)
    )
    assert _check(checks, "外链位置").ok is True
    assert "链接放评论" in _check(checks, "外链位置").detail


def test_validate_allows_link_in_vk_body() -> None:
    checks, _ = validate_caption(
        _spec(language="ru", max_chars=400, link_in_body_allowed=True),
        "Отливки и механообработка. Сайт: https://example.com",
        ("#литьё",),
    )
    assert _check(checks, "外链位置").ok is True
    assert _check(checks, "俄语").ok is True


def test_vk_spec_allows_link_in_body_and_others_do_not() -> None:
    assert CAPTION_SPECS["vk"].link_in_body_allowed is True
    for key in ("linkedin", "facebook", "tiktok"):
        assert CAPTION_SPECS[key].link_in_body_allowed is False, f"{key} 正文不得放链接"


def test_validate_flags_non_russian_vk_text() -> None:
    checks, _ = validate_caption(_spec(language="ru"), "x" * 20, ("#a",))
    assert _check(checks, "俄语").ok is False


def test_count_emoji() -> None:
    assert count_emoji("hello 🏭 world 🔥") == 2
    assert count_emoji("plain text") == 0


# ---------- 生成 ----------


def _client(payload_text: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "model" in body
        assert "instructions" in body or "messages" in body
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": payload_text}],
                    }
                ]
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_generate_caption_returns_checks() -> None:
    text = "Most machine tool builders don't own a foundry. " + "We cast and pre-machine. " * 20
    client = _client(
        json.dumps(
            {
                "text": text,
                "hashtags": ["#casting", "#foundry", "#machinetools"],
                "first_comment": "Capability sheet here → [link]",
            },
            ensure_ascii=False,
        )
    )
    result = generate_caption(
        VisionConfig(api_key="k"), platform="linkedin", materials=[_material()], client=client
    )
    assert result.platform == "linkedin"
    assert result.language == "en"
    assert result.hashtags == ("#casting", "#foundry", "#machinetools")
    assert result.first_comment.startswith("Capability sheet")
    assert result.full_text.endswith("#machinetools")
    assert any(item.name == "长度" for item in result.checks)


def test_generate_caption_normalises_hashtags() -> None:
    text = "x" * 700
    client = _client(json.dumps({"text": text, "hashtags": "casting foundry machinetools"}))
    result = generate_caption(
        VisionConfig(api_key="k"), platform="linkedin", materials=[_material()], client=client
    )
    assert result.hashtags == ("#casting", "#foundry", "#machinetools"), "缺 # 要自动补"


def test_generate_caption_keeps_english_master_for_vk() -> None:
    text = "Отливки и механообработка на одном заводе. " * 20
    client = _client(
        json.dumps({"text": text, "hashtags": ["#литьё"], "text_en": "Casting and machining."})
    )
    result = generate_caption(
        VisionConfig(api_key="k"), platform="vk", materials=[_material()], client=client
    )
    assert result.language == "ru"
    assert result.text_en == "Casting and machining."


def test_generate_caption_rejects_unknown_platform() -> None:
    with pytest.raises(ValueError, match="不支持的平台"):
        generate_caption(
            VisionConfig(api_key="k"), platform="weibo", materials=[_material()]
        )


def test_generate_caption_requires_key() -> None:
    with pytest.raises(ValueError, match="PULSE_VISION_API_KEY"):
        generate_caption(VisionConfig(api_key=""), platform="linkedin", materials=[_material()])


def test_generate_caption_rejects_empty_text() -> None:
    client = _client(json.dumps({"text": "", "hashtags": []}))
    with pytest.raises(ValueError, match="没有返回正文"):
        generate_caption(
            VisionConfig(api_key="k"), platform="linkedin", materials=[_material()], client=client
        )
