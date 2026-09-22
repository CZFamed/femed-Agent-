"""W2-A4-6/7：判定落库、豁免留痕、发布前闸门、队列入口。"""

from __future__ import annotations

import pytest

from pulse.services.compliance.errors import (
    FindingNotFoundError,
    WaiverNotAllowedError,
)
from pulse.services.compliance.models import MediaView, VariantView
from pulse.services.compliance.service import INDIVIDUAL_REVIEW_TRIGGERS, ComplianceService
from pulse.shared.enums import ComplianceSeverity


def _view(**overrides) -> VariantView:
    base = dict(
        variant_id="var_a4_svc",
        platform="linkedin",
        text="x" * 700,
        hashtags=("#casting", "#foundry", "#machining"),
        media=(MediaView(license_status="owned"),),
    )
    base.update(overrides)
    return VariantView(**base)


def test_check_stores_findings_with_ids():
    service = ComplianceService()

    outcome = service.check(_view(text=("x" * 100) + " a world-class team"))

    assert outcome.findings
    assert all(item.id is not None for item in outcome.findings)
    assert service.findings_for("var_a4_svc") == outcome.findings
    assert outcome.findings_ref


def test_blocked_when_block_finding_exists():
    service = ComplianceService()

    outcome = service.check(_view(media=(MediaView(license_status="pending"),)))

    assert outcome.blocked is True
    assert [item.rule for item in outcome.blocking] == ["copyright.license_pending"]


def test_waive_warn_requires_actor_and_records_it():
    service = ComplianceService()
    outcome = service.check(_view(text=("x" * 100) + " a world-class team"))
    warn = next(item for item in outcome.findings if item.severity is ComplianceSeverity.WARN)

    with pytest.raises(WaiverNotAllowedError):
        service.waive(warn.id, actor="   ")

    waived = service.waive(warn.id, actor="ops_reviewer", reason="已核实为可核验表述")

    assert waived.waived_by == "ops_reviewer（已核实为可核验表述）"
    assert service.outcome_for("var_a4_svc").waived == (waived,)


def test_block_cannot_be_waived_by_normal_user_but_admin_can():
    service = ComplianceService()
    outcome = service.check(_view(media=(MediaView(license_status="pending"),)))
    block = outcome.blocking[0]

    with pytest.raises(WaiverNotAllowedError) as exc:
        service.waive(block.id, actor="ops_reviewer")
    assert "默认不可绕过" in str(exc.value)

    service.waive(block.id, actor="compliance_admin", reason="已补齐授权登记", is_admin=True)

    assert service.outcome_for("var_a4_svc").blocked is False


def test_unknown_finding_raises():
    with pytest.raises(FindingNotFoundError):
        ComplianceService().waive(999, actor="ops")


def test_compliance_info_is_contract_valid_when_blocked():
    service = ComplianceService()
    service.check(_view(media=(MediaView(license_status="pending"),)))

    info = service.compliance_info("var_a4_svc")

    assert info.blocked is True
    assert info.findings_ref
    assert info.validate() == []


def test_compliance_info_is_clean_when_no_blocking_findings():
    service = ComplianceService()
    service.check(_view())

    info = service.compliance_info("var_a4_svc")

    assert info.blocked is False
    assert info.validate() == []


def test_must_review_individually_reports_all_triggers():
    service = ComplianceService()
    view = _view(first_time_account=True, text=("x" * 100) + " a world-class team")
    outcome = service.check(view)

    triggers = service.must_review_individually(view, outcome, brand_guide_changed=True)

    assert triggers == INDIVIDUAL_REVIEW_TRIGGERS


def test_clean_copy_needs_no_individual_review():
    service = ComplianceService()
    view = _view()

    outcome = service.check(view)

    assert service.must_review_individually(view, outcome) == ()


def test_queue_handler_uses_id_only_payload():
    service = ComplianceService()
    view = _view()

    result = service.handle_check({"variant_id": "var_a4_svc"}, view)

    assert "pulse.compliance.check" in result
    assert result["pulse.compliance.check"]["variant_id"] == "var_a4_svc"
    assert "must_review_individually" in result


def test_queue_handler_rejects_mismatched_or_missing_id():
    service = ComplianceService()

    with pytest.raises(FindingNotFoundError):
        service.handle_check({}, _view())

    with pytest.raises(FindingNotFoundError):
        service.handle_check({"variant_id": "var_other"}, _view())
