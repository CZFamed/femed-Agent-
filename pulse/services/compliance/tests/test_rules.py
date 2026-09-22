"""W2-A4-1/2/3/4：规则引擎、版权、敏感词、平台质量规则。"""

from __future__ import annotations

from pulse.services.compliance.models import MediaView, VariantView
from pulse.services.compliance.rules import RuleEngine
from pulse.shared.enums import ComplianceSeverity


def _view(**overrides) -> VariantView:
    base = dict(
        variant_id="var_a4_1",
        platform="linkedin",
        text="x" * 700,
        hashtags=("#casting", "#foundry", "#machining"),
        media=(MediaView(license_status="owned"),),
    )
    base.update(overrides)
    return VariantView(**base)


def _rules(view: VariantView) -> set[str]:
    return {item.rule for item in RuleEngine().evaluate(view)}


def test_finding_fields_match_contract_shape():
    view = _view(media=(MediaView(license_status="pending"),))

    findings = RuleEngine().evaluate(view)

    assert findings
    payload = findings[0].as_payload()
    assert set(payload) == {"id", "rule", "severity", "message", "position", "waived_by"}
    assert payload["severity"] in {item.value for item in ComplianceSeverity}


def test_owned_and_licensed_media_pass():
    assert "copyright.license_pending" not in _rules(_view())
    assert "copyright.license_missing" not in _rules(
        _view(media=(MediaView(license_status="licensed"),))
    )


def test_pending_media_is_blocked():
    findings = RuleEngine().evaluate(_view(media=(MediaView(license_status="pending"),)))

    blocking = [item for item in findings if item.severity is ComplianceSeverity.BLOCK]
    assert blocking and blocking[0].rule == "copyright.license_pending"
    assert blocking[0].position["field"] == "media[0].license_status"


def test_unknown_license_is_blocked():
    findings = RuleEngine().evaluate(_view(media=(MediaView(license_status=""),)))

    assert any(item.rule == "copyright.license_missing" for item in findings)


def test_block_sensitive_word_is_blocked_with_position():
    findings = RuleEngine().evaluate(_view(text=("x" * 100) + " for military use only"))

    hit = next(item for item in findings if item.rule == "sensitive.blocked_term")
    assert hit.severity is ComplianceSeverity.BLOCK
    assert hit.position["field"] == "text"
    assert hit.position["offset"] > 0


def test_warn_sensitive_word_is_warn_only():
    findings = RuleEngine().evaluate(_view(text=("x" * 100) + " nuclear industry parts"))

    hit = next(item for item in findings if item.rule == "sensitive.review_term")
    assert hit.severity is ComplianceSeverity.WARN
    assert all(item.rule != "sensitive.blocked_term" for item in findings)


def test_banned_guarantee_phrase_is_blocked():
    findings = RuleEngine().evaluate(_view(text=("x" * 100) + " guaranteed delivery"))

    assert any(item.rule == "claims.guarantee" for item in findings)


def test_unverifiable_adjective_is_warn():
    findings = RuleEngine().evaluate(_view(text=("x" * 100) + " a world-class foundry"))

    hit = next(item for item in findings if item.rule == "claims.unverifiable_adjective")
    assert hit.severity is ComplianceSeverity.WARN


def test_spec_numbers_require_manual_confirmation():
    rules = _rules(_view(text=("x" * 100) + " material HT200, weight 2.5 tons"))

    assert "claims.unverified_spec" in rules


def test_platform_quality_rules_cover_length_tags_and_emoji():
    short = _rules(_view(text="too short"))
    assert "quality.length_below_min" in short

    long = _rules(_view(text="x" * 1300))
    assert "quality.length_above_max" in long

    few_tags = _rules(_view(hashtags=("#casting",)))
    assert "quality.hashtag_too_few" in few_tags

    many_tags = _rules(_view(hashtags=("#a", "#b", "#c", "#d", "#e", "#f")))
    assert "quality.hashtag_too_many" in many_tags

    emoji = _rules(_view(text=("x" * 600) + " 🏭🔥🔥🔥"))
    assert "quality.emoji_too_many" in emoji


def test_hashtag_shape_rules():
    rules = _rules(_view(hashtags=("casting", "#with space", "#ok", "#ok2")))

    assert "quality.hashtag_missing_hash" in rules
    assert "quality.hashtag_has_space" in rules


def test_machine_flavor_rules():
    opener = _rules(_view(text=("x" * 600) + " 本公司专业生产各类铸件"))
    assert "quality.machine_flavor_opener" in opener

    bangs = _rules(_view(text=("x" * 600) + " Great!!! Amazing!!! Wow!!!"))
    assert "quality.machine_flavor_bangs" in bangs


def test_clean_copy_has_no_blocking_findings():
    outcome = RuleEngine().evaluate(_view())

    assert not [item for item in outcome if item.severity is ComplianceSeverity.BLOCK]


def test_block_findings_sort_before_warnings():
    findings = RuleEngine().evaluate(
        _view(text=("x" * 100) + " military use and a world-class team", media=(MediaView(license_status="pending"),))
    )

    severities = [item.severity for item in findings]
    assert severities == sorted(severities, key=lambda item: 0 if item is ComplianceSeverity.BLOCK else 1)
