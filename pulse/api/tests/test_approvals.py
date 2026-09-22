"""W2-A5-2：审批工作流（状态机、留痕、三条件强制单条复核、合规硬拦截）。"""

from __future__ import annotations

from pulse.services.compliance import MediaView, VariantView


def _variant_id(created: dict) -> str:
    return created["variants"][0]["id"]


def test_submit_moves_draft_to_pending_review(app, created):
    variant_id = _variant_id(created)

    response = app.handle(
        "PATCH", f"/api/v1/variants/{variant_id}/status", body={"action": "submit", "actor": "ops"}
    )

    assert response.status == 200
    assert response.body["status"] == "pending_review"


def test_submit_twice_is_conflict(app, created):
    variant_id = _variant_id(created)
    app.handle(
        "PATCH", f"/api/v1/variants/{variant_id}/status", body={"action": "submit", "actor": "ops"}
    )

    again = app.handle(
        "PATCH", f"/api/v1/variants/{variant_id}/status", body={"action": "submit", "actor": "ops"}
    )

    assert again.status == 409


def test_approve_requires_pending_review(app, created):
    variant_id = _variant_id(created)

    response = app.handle(
        "PATCH",
        f"/api/v1/variants/{variant_id}/status",
        body={"action": "approve", "actor": "brand_reviewer"},
    )

    assert response.status == 409
    assert "pending_review" in response.body["error"]["message"]


def test_approve_requires_actor_for_audit_trail(app, created):
    variant_id = _variant_id(created)
    app.handle(
        "PATCH", f"/api/v1/variants/{variant_id}/status", body={"action": "submit", "actor": "ops"}
    )

    response = app.handle(
        "PATCH", f"/api/v1/variants/{variant_id}/status", body={"action": "approve", "actor": "  "}
    )

    assert response.status == 403
    assert "留痕" in response.body["error"]["message"]


def test_reject_and_needs_revision_set_status(app, created):
    variant_id = _variant_id(created)
    app.handle(
        "PATCH", f"/api/v1/variants/{variant_id}/status", body={"action": "submit", "actor": "ops"}
    )

    rejected = app.handle(
        "PATCH",
        f"/api/v1/variants/{variant_id}/status",
        body={"action": "needs_revision", "actor": "ops", "diff": {"hashtags": ["老标签", "新标签"]}},
    )

    assert rejected.status == 200
    assert rejected.body["status"] == "needs_revision"
    assert rejected.body["approval"]["diff"]["hashtags"] == ["老标签", "新标签"]


def test_compliance_block_prevents_approval_but_admin_can_override(app, created, compliance):
    variant_id = _variant_id(created)
    variant = app.content.store.get_variant(variant_id)
    compliance.check(
        VariantView(
            variant_id=variant_id,
            platform="linkedin",
            text="x" * 700,
            hashtags=("#casting", "#foundry", "#machining"),
            media=(MediaView(license_status="pending"),),
        )
    )
    app.handle(
        "PATCH", f"/api/v1/variants/{variant_id}/status", body={"action": "submit", "actor": "ops"}
    )

    refused = app.handle(
        "PATCH",
        f"/api/v1/variants/{variant_id}/status",
        body={"action": "approve", "actor": "ops"},
    )
    overridden = app.handle(
        "PATCH",
        f"/api/v1/variants/{variant_id}/status",
        body={"action": "approve", "actor": "admin", "is_admin": True},
    )

    assert refused.status == 403
    assert refused.body["error"]["details"]["blocking"] == ["copyright.license_pending"]
    assert overridden.status == 200
    assert overridden.body["status"] == "approved"
    del variant


def test_batch_approve_refuses_variants_hit_by_warning_trigger(app, created, compliance):
    variant_id = _variant_id(created)
    compliance.check(
        VariantView(
            variant_id=variant_id,
            platform="linkedin",
            text=("x" * 100) + " a world-class team",
            hashtags=("#casting", "#foundry", "#machining"),
            media=(MediaView(license_status="owned"),),
        )
    )
    app.handle(
        "PATCH", f"/api/v1/variants/{variant_id}/status", body={"action": "submit", "actor": "ops"}
    )

    response = app.handle(
        "PATCH",
        f"/api/v1/variants/{variant_id}/status",
        body={"action": "batch_approve", "actor": "ops", "variant_ids": [variant_id]},
    )

    assert response.status == 200
    assert response.body["approved"] == []
    assert response.body["refused"][0]["variant_id"] == variant_id
    assert "compliance_warning" in response.body["refused"][0]["reason"]


def test_batch_approve_refuses_everything_when_brand_guide_changed(app, created):
    variant_id = _variant_id(created)
    app.handle(
        "PATCH", f"/api/v1/variants/{variant_id}/status", body={"action": "submit", "actor": "ops"}
    )

    response = app.handle(
        "PATCH",
        f"/api/v1/variants/{variant_id}/status",
        body={
            "action": "batch_approve",
            "actor": "ops",
            "variant_ids": [variant_id],
            "brand_guide_changed": True,
        },
    )

    assert response.status == 200
    assert response.body["approved"] == []
    assert "brand_guide_changed" in response.body["refused"][0]["reason"]


def test_batch_approve_accepts_clean_variants(app, created):
    variant_id = _variant_id(created)
    app.handle(
        "PATCH", f"/api/v1/variants/{variant_id}/status", body={"action": "submit", "actor": "ops"}
    )

    response = app.handle(
        "PATCH",
        f"/api/v1/variants/{variant_id}/status",
        body={"action": "batch_approve", "actor": "ops", "variant_ids": [variant_id]},
    )

    assert response.status == 200
    assert response.body["approved"] == [variant_id]
    assert app.content.store.get_variant(variant_id).status.value == "approved"


def test_approval_history_is_queryable_with_actor_and_diff(app, created):
    variant_id = _variant_id(created)
    app.handle(
        "PATCH", f"/api/v1/variants/{variant_id}/status", body={"action": "submit", "actor": "ops"}
    )
    app.handle(
        "PATCH",
        f"/api/v1/variants/{variant_id}/status",
        body={"action": "approve", "actor": "brand_reviewer", "diff": {"caption.text": ["a", "b"]}},
    )

    history = app.approvals.history(variant_id)

    assert [item.action for item in history] == ["approve"]
    assert history[0].actor == "brand_reviewer"
    assert history[0].diff == {"caption.text": ["a", "b"]}
