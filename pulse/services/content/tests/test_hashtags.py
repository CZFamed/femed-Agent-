"""W1-A1-4：话题标签库与契约规范。"""

from __future__ import annotations

import pytest

from pulse.services.content.errors import UnknownHashtagError
from pulse.services.content.hashtags import (
    ALLOWED_TAGS,
    compose_hashtags,
    ensure_allowed,
    normalize_tag,
    validate_hashtags,
)


def test_compose_hashtags_satisfies_contract_shape():
    tags = compose_hashtags(categories=("casting", "machining"), max_count=5)

    assert tags
    assert all(tag.startswith("#") for tag in tags)
    assert all(" " not in tag for tag in tags)
    assert len(tags) <= 5
    assert tags == tuple(dict.fromkeys(tags))  # 无重复且顺序稳定


def test_compose_hashtags_is_deterministic():
    first = compose_hashtags(categories=("casting",), max_count=4)
    second = compose_hashtags(categories=("casting",), max_count=4)

    assert first == second


def test_compose_hashtags_respects_max_count():
    tags = compose_hashtags(categories=("casting", "machining", "quality"), max_count=2)

    assert len(tags) == 2


def test_compose_hashtags_can_omit_brand_tag():
    tags = compose_hashtags(categories=("casting",), max_count=2, brand_tag=None)

    assert "#famed" not in tags


def test_compose_hashtags_rejects_unknown_extra():
    with pytest.raises(UnknownHashtagError) as exc:
        compose_hashtags(categories=("casting",), extra=("handmade",))

    assert "#handmade" in str(exc.value)


def test_ensure_allowed_lists_every_violation():
    with pytest.raises(UnknownHashtagError) as exc:
        ensure_allowed(["#casting", "#handmade", "#cheap"])

    details = exc.value.details
    assert details["not_allowed"] == ["#cheap", "#handmade"]
    assert "#casting" in details["allowed"]


def test_validate_hashtags_reports_shape_and_whitelist_problems():
    errors = validate_hashtags(["casting", "#with space", "#casting", "#nope"], min_count=5, max_count=1)

    joined = "；".join(errors)
    assert "必须以 # 开头" in joined
    assert "不能含空格" in joined
    assert "重复" in joined
    assert "白名单" in joined
    assert "数量不足" in joined
    assert "数量超限" in joined


def test_validate_hashtags_passes_for_good_input():
    tags = compose_hashtags(categories=("casting", "machining"), max_count=5)

    assert validate_hashtags(tags, min_count=3, max_count=5) == []


def test_normalize_tag_handles_hash_and_case():
    assert normalize_tag("Casting") == "#casting"
    assert normalize_tag("#LOSTFOAM") == "#lostfoam"
    assert normalize_tag("   ") == ""


def test_allowed_tags_are_lowercase_and_prefixed():
    assert all(tag.startswith("#") and tag == tag.lower() for tag in ALLOWED_TAGS)
