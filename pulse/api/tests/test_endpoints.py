"""W2-A5-1/4/5：11 个端点各至少一例，路径与契约 §7 逐字一致。"""

from __future__ import annotations

from pulse.api.app import ApiApp


def test_route_table_matches_contract_section_7(app):
    """端点集合与契约 §7 的 11 条**逐字一致**（多一条少一条都要失败）。"""
    expected = {
        ("POST", "/api/v1/briefs"),
        ("GET", "/api/v1/contents/{content_id}/variants"),
        ("PATCH", "/api/v1/variants/{variant_id}/status"),
        ("POST", "/api/v1/variants/{variant_id}/schedule"),
        ("PATCH", "/api/v1/schedules/{schedule_id}"),
        ("POST", "/api/v1/schedules/{schedule_id}/publish"),
        ("GET", "/api/v1/schedules/{schedule_id}/semi-auto"),
        ("GET", "/api/v1/accounts"),
        ("POST", "/api/v1/accounts/{account_id}/oauth"),
        ("DELETE", "/api/v1/accounts/{account_id}/credential"),
        ("POST", "/api/v1/compliance/screen"),
    }

    actual = {(item["method"], item["path"]) for item in app.route_table()}

    assert actual == expected
    assert len(actual) == 11


def test_1_post_brief_creates_content_and_variant(app, created):
    assert created["content"]["brief_id"] == "b_a5_0001"
    assert len(created["variants"]) == 1
    variant = created["variants"][0]
    assert variant["platform"] == "linkedin"
    assert variant["status"] == "draft"
    assert variant["unified_post_id"].startswith("up_")


def test_2_get_content_variants(app, created):
    content_id = created["content"]["id"]

    response = app.handle("GET", f"/api/v1/contents/{content_id}/variants")

    assert response.status == 200
    assert [item["id"] for item in response.body["variants"]] == [created["variants"][0]["id"]]


def test_2_get_content_variants_404(app):
    response = app.handle("GET", "/api/v1/contents/src_missing/variants")

    assert response.status == 404
    assert response.body["error"]["code"] == "not_found"


def test_3_patch_variant_status_submit_then_approve(app, created):
    variant_id = created["variants"][0]["id"]

    submitted = app.handle(
        "PATCH",
        f"/api/v1/variants/{variant_id}/status",
        body={"action": "submit", "actor": "ops"},
    )
    approved = app.handle(
        "PATCH",
        f"/api/v1/variants/{variant_id}/status",
        body={
            "action": "approve",
            "actor": "brand_reviewer",
            "diff": {"caption.text": ["old line", "new line"]},
        },
    )

    assert submitted.body["status"] == "pending_review"
    assert approved.status == 200
    assert approved.body["status"] == "approved"
    assert approved.body["approval"]["diff"]["caption.text"] == ["old line", "new line"]
    assert approved.body["approval"]["actor"] == "brand_reviewer"


def test_3_patch_variant_status_rejects_unknown_action(app, created):
    variant_id = created["variants"][0]["id"]

    response = app.handle(
        "PATCH", f"/api/v1/variants/{variant_id}/status", body={"action": "publish_it"}
    )

    assert response.status == 409
    assert "未知审批动作" in response.body["error"]["message"]


def test_4_post_variant_schedule_requires_approval(app, created):
    variant_id = created["variants"][0]["id"]

    response = app.handle("POST", f"/api/v1/variants/{variant_id}/schedule", body={})

    assert response.status == 409
    assert "只有 approved" in response.body["error"]["message"]


def test_4_post_variant_schedule_creates_job_with_upstream_key(app, approved, clock):
    response = app.handle(
        "POST",
        f"/api/v1/variants/{approved}/schedule",
        body={"scheduled_at": "2026-09-23T10:00:00+05:30"},
        actor="ops",
    )

    assert response.status == 201, response.body
    body = response.body
    assert body["schedule"]["timezone"] == "Asia/Kolkata"
    assert body["job"]["id"].startswith("job_")
    job = app.dispatcher.jobs.get(body["job"]["id"])
    assert job.unified_post_id == body["unified_post_id"]


def test_4_post_variant_schedule_rejects_naive_datetime(app, approved):
    response = app.handle(
        "POST",
        f"/api/v1/variants/{approved}/schedule",
        body={"scheduled_at": "2026-09-23T10:00:00"},
    )

    assert response.status == 400
    assert "时区偏移" in response.body["error"]["message"]


def test_5_patch_schedule_cancel_and_reschedule(app, approved, clock):
    scheduled = app.handle(
        "POST",
        f"/api/v1/variants/{approved}/schedule",
        body={"scheduled_at": "2026-09-23T10:00:00+05:30"},
    )
    schedule_id = scheduled.body["schedule"]["id"]

    rescheduled = app.handle(
        "PATCH",
        f"/api/v1/schedules/{schedule_id}",
        body={"action": "reschedule", "scheduled_at": "2026-09-24T10:00:00+05:30"},
    )
    cancelled = app.handle(
        "PATCH", f"/api/v1/schedules/{schedule_id}", body={"action": "cancel", "reason": "改期"}
    )

    assert rescheduled.status == 200
    assert rescheduled.body["action"] == "reschedule"
    assert cancelled.status == 200
    assert app.dispatcher.schedules.get(schedule_id).status_value.value == "cancelled"


def test_6_post_schedule_publish(app, approved):
    scheduled = app.handle(
        "POST",
        f"/api/v1/variants/{approved}/schedule",
        body={"scheduled_at": "2026-09-23T10:00:00+05:30"},
    )
    schedule_id = scheduled.body["schedule"]["id"]

    response = app.handle("POST", f"/api/v1/schedules/{schedule_id}/publish", body={"actor": "ops"})

    assert response.status == 200
    assert response.body["job_id"]
    assert "立即投递" in response.body["reason"]


def test_7_get_semi_auto_returns_copy_bundle(app, approved, exporter):
    scheduled = app.handle(
        "POST",
        f"/api/v1/variants/{approved}/schedule",
        body={"scheduled_at": "2026-09-23T10:00:00+05:30"},
    )
    schedule_id = scheduled.body["schedule"]["id"]

    response = app.handle("GET", f"/api/v1/schedules/{schedule_id}/semi-auto")

    assert response.status == 200
    body = response.body
    assert body["platform"] == "linkedin"
    assert body["text"].strip()
    assert body["deep_link"]
    assert body["checklist"]
    assert exporter.calls == [scheduled.body["unified_post_id"]]


def test_8_get_accounts_lists_status_and_quota_without_credentials(app):
    response = app.handle("GET", "/api/v1/accounts")

    assert response.status == 200
    account = response.body["accounts"][0]
    assert account["id"] == "acct_li_01"
    assert account["platform"] == "linkedin"
    assert account["status"] == "active"
    assert account["quota_config"] == {}
    assert account["has_credential"] is False
    assert "credential" not in str(account).lower().replace("has_credential", "").replace(
        "credential_record", ""
    )


def test_9_post_account_oauth_does_not_fabricate_authorize_url(app):
    response = app.handle("POST", "/api/v1/accounts/acct_li_01/oauth", body={})

    assert response.status == 202
    oauth = response.body["oauth"]
    assert oauth["status"] == "pending_configuration"
    assert oauth["authorize_url"] is None
    assert set(oauth["missing"]) == {"client_id", "redirect_uri"}


def test_10_delete_credential_revokes_and_suspends(app, identity, clock):
    from datetime import timedelta

    from pulse.services.identity.credentials import TokenSet

    identity.store_tokens(
        "acct_li_01",
        tokens=TokenSet(
            access_token="access-placeholder",
            refresh_token="refresh-placeholder",
            expires_at=clock() + timedelta(days=30),
            refresh_expires_at=clock() + timedelta(days=90),
        ),
        reauthorize=True,
    )

    response = app.handle(
        "DELETE", "/api/v1/accounts/acct_li_01/credential", body={"actor": "admin"}
    )

    assert response.status == 200
    assert response.body["credential_status"] == "revoked"
    assert identity.get_account("acct_li_01").status_value.value == "paused"


def test_11_post_compliance_screen(app):
    response = app.handle(
        "POST", "/api/v1/compliance/screen", body={"subject": "Haas Automation", "checked_by": "ops"}
    )

    assert response.status == 200
    screening = response.body["screening"]
    assert screening["result"] == "clear"
    assert screening["lists_checked"]
    assert screening["disclaimer"].startswith("本结论仅表示")


def test_11_post_compliance_screen_requires_subject(app):
    response = app.handle("POST", "/api/v1/compliance/screen", body={})

    assert response.status == 400
    assert "subject" in response.body["error"]["message"]


def test_unknown_route_returns_unified_error_body(app):
    response = app.handle("GET", "/api/v1/nope")

    assert response.status == 404
    assert set(response.body["error"]) == {"code", "message", "details"}
