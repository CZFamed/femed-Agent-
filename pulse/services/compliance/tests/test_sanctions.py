"""W2-A4-5：制裁与出口管制筛查（三类结果 + 留痕完整性）。"""

from __future__ import annotations

import pytest

from pulse.services.compliance.errors import RuleConfigError
from pulse.services.compliance.sanctions import (
    DISCLAIMER,
    SanctionsList,
    load_lists,
    normalize_subject,
    screen,
)
from pulse.shared.enums import ScreeningResult


def _lists() -> tuple[SanctionsList, ...]:
    return (
        SanctionsList(
            name="OFAC SDN",
            source="https://example.invalid/ofac",
            updated="2026-09-01",
            entries=("Acme Trading FZE",),
        ),
        SanctionsList(
            name="BIS Entity List",
            source="https://example.invalid/bis",
            updated="2026-09-01",
            entries=(),
        ),
    )


def test_normalize_subject_strips_legal_suffixes_and_punctuation():
    assert normalize_subject("ACME Trading FZE") == "acme trading fze"
    assert normalize_subject("Haas Automation, Inc.") == "haas automation"
    assert normalize_subject("  沧州菲美得机械设备有限公司 ") == "沧州菲美得机械设备"


def test_hit_records_every_list_checked():
    record = screen("Acme Trading FZE", lists=_lists(), review_hints=(), checked_by="ops")

    assert record.result is ScreeningResult.HIT
    assert record.lists_checked == ("OFAC SDN", "BIS Entity List")
    assert record.evidence["hits"][0]["list"] == "OFAC SDN"
    assert record.checked_by == "ops"
    assert record.disclaimer == DISCLAIMER


def test_hit_is_insensitive_to_suffix_and_case():
    record = screen("ACME TRADING FZE", lists=_lists(), review_hints=())

    assert record.result is ScreeningResult.HIT


def test_review_required_on_project_risk_hint():
    record = screen("Taksan Makina", lists=_lists(), review_hints=("turkey", "taksan"))

    assert record.result is ScreeningResult.REVIEW_REQUIRED
    assert set(record.evidence["review_hints"]) == {"taksan"}
    assert "非法律清单" in record.evidence["next_step"]


def test_clear_when_nothing_matches_but_still_traceable():
    record = screen("Haas Automation", lists=_lists(), review_hints=())

    assert record.result is ScreeningResult.CLEAR
    assert record.lists_checked == ("OFAC SDN", "BIS Entity List")
    assert record.evidence["lists_updated"] == {"OFAC SDN": "2026-09-01", "BIS Entity List": "2026-09-01"}
    assert "TODO(need-real-data)" in record.evidence["note"]


def test_empty_subject_is_rejected():
    with pytest.raises(RuleConfigError):
        screen("   ")


def test_payload_carries_disclaimer_and_no_legal_conclusion():
    payload = screen("Haas Automation", lists=_lists(), review_hints=()).as_payload()

    assert payload["disclaimer"].startswith("本结论仅表示")
    blob = str(payload)
    for forbidden in ("合规", "合法", "无风险"):
        assert forbidden not in blob.replace(payload["disclaimer"], "")


def test_shipped_lists_are_present_but_marked_as_placeholder():
    lists, hints = load_lists()

    names = {item.name for item in lists}
    assert {"OFAC SDN", "BIS Entity List"} <= names
    assert all(item.updated == "TODO(need-real-data)" for item in lists)
    assert all(item.entries == () for item in lists), "真实名单未导入前不得伪造条目"
    assert hints, "项目级风险提示（土耳其等）必须有数据"


def test_screen_with_shipped_data_defaults_to_clear_for_unrelated_name():
    record = screen("Haas Automation", checked_by="ops")

    assert record.result is ScreeningResult.CLEAR
    assert len(record.lists_checked) >= 3
