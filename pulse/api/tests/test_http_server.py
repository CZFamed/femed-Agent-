"""HTTP 适配测试：**真的起服务、真的发 HTTP 请求**（httpx 打临时端口）。"""

from __future__ import annotations

import json

import httpx
import pytest

from pulse.api.http_server import PulseHttpServer
from pulse.api.wiring import DEMO_ACCOUNT_ID, build_demo_app


@pytest.fixture()
def live_server():
    app = build_demo_app()
    server = PulseHttpServer(app, port=0).serve_in_background()
    try:
        yield server, app
    finally:
        server.stop()


@pytest.fixture()
def client(live_server):
    server, _app = live_server
    with httpx.Client(base_url=server.base_url, timeout=10) as http:
        yield http


BRIEF = {
    "brief_id": "b_http_0001",
    "topic": "Valve body castings with pre-machining",
    "target_audience": "machine tool OEMs in India",
    "platforms": ["linkedin"],
}


def test_server_binds_ephemeral_port(live_server):
    server, _app = live_server

    assert server.port > 0
    assert server.base_url.startswith("http://127.0.0.1:")


def test_get_accounts_over_http(client):
    response = client.get("/api/v1/accounts")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["accounts"][0]["id"] == DEMO_ACCOUNT_ID
    assert response.headers["content-type"].startswith("application/json")


def test_full_chain_over_http(client):
    """11 个端点里最主要的一条链路：提交 → 审批 → 排期 → 半自动导出。"""
    created = client.post("/api/v1/briefs", json=BRIEF)
    assert created.status_code == 201, created.text
    variant_id = created.json()["variants"][0]["id"]
    content_id = created.json()["content"]["id"]

    listed = client.get(f"/api/v1/contents/{content_id}/variants")
    assert listed.status_code == 200
    assert listed.json()["variants"][0]["id"] == variant_id

    submitted = client.patch(
        f"/api/v1/variants/{variant_id}/status",
        json={"action": "submit", "actor": "ops"},
        headers={"X-Actor": "ops"},
    )
    assert submitted.json()["status"] == "pending_review"

    approved = client.patch(
        f"/api/v1/variants/{variant_id}/status",
        json={"action": "approve", "diff": {"caption.text": ["old", "new"]}},
        headers={"X-Actor": "brand_reviewer"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    # X-Actor 头部必须进审批留痕
    assert approved.json()["approval"]["actor"] == "brand_reviewer"

    scheduled = client.post(
        f"/api/v1/variants/{variant_id}/schedule",
        json={"scheduled_at": "2026-10-20T10:00:00+05:30"},
    )
    assert scheduled.status_code == 201, scheduled.text
    schedule_id = scheduled.json()["schedule"]["id"]
    assert scheduled.json()["unified_post_id"].startswith("up_")

    published = client.post(f"/api/v1/schedules/{schedule_id}/publish", json={})
    assert published.status_code == 200

    bundle = client.get(f"/api/v1/schedules/{schedule_id}/semi-auto")
    assert bundle.status_code == 200
    assert bundle.json()["platform"] == "linkedin"
    assert bundle.json()["text"].strip()
    assert bundle.json()["checklist"]


def test_patch_schedule_and_delete_credential_over_http(client):
    created = client.post("/api/v1/briefs", json=BRIEF | {"brief_id": "b_http_0002"})
    variant_id = created.json()["variants"][0]["id"]
    client.patch(f"/api/v1/variants/{variant_id}/status", json={"action": "submit", "actor": "ops"})
    client.patch(f"/api/v1/variants/{variant_id}/status", json={"action": "approve", "actor": "ops"})
    scheduled = client.post(
        f"/api/v1/variants/{variant_id}/schedule",
        json={"scheduled_at": "2026-10-20T10:00:00+05:30"},
    )
    schedule_id = scheduled.json()["schedule"]["id"]

    rescheduled = client.patch(
        f"/api/v1/schedules/{schedule_id}",
        json={"action": "reschedule", "scheduled_at": "2026-10-21T10:00:00+05:30"},
    )
    cancelled = client.patch(
        f"/api/v1/schedules/{schedule_id}", json={"action": "cancel", "reason": "改期"}
    )
    oauth = client.post(f"/api/v1/accounts/{DEMO_ACCOUNT_ID}/oauth", json={})
    revoked = client.request(
        "DELETE", f"/api/v1/accounts/{DEMO_ACCOUNT_ID}/credential", json={"actor": "admin"}
    )
    screened = client.post(
        "/api/v1/compliance/screen", json={"subject": "Haas Automation", "checked_by": "ops"}
    )

    assert rescheduled.status_code == 200
    assert cancelled.status_code == 200
    assert oauth.status_code == 202 and oauth.json()["oauth"]["authorize_url"] is None
    assert revoked.status_code in (200, 404), revoked.text
    assert screened.status_code == 200
    assert screened.json()["screening"]["result"] == "clear"


def test_unknown_path_returns_unified_error_over_http(client):
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert set(response.json()["error"]) == {"code", "message", "details"}


def test_invalid_json_body_is_400(client):
    response = client.post(
        "/api/v1/briefs",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert "JSON" in response.json()["error"]["message"]


def test_naive_datetime_is_rejected_over_http(client):
    created = client.post("/api/v1/briefs", json=BRIEF | {"brief_id": "b_http_0003"})
    variant_id = created.json()["variants"][0]["id"]
    client.patch(f"/api/v1/variants/{variant_id}/status", json={"action": "submit", "actor": "ops"})
    client.patch(f"/api/v1/variants/{variant_id}/status", json={"action": "approve", "actor": "ops"})

    response = client.post(
        f"/api/v1/variants/{variant_id}/schedule",
        json={"scheduled_at": "2026-10-20T10:00:00"},
    )

    assert response.status_code == 400
    assert "时区偏移" in response.json()["error"]["message"]


def test_unapproved_variant_cannot_be_scheduled_over_http(client):
    created = client.post("/api/v1/briefs", json=BRIEF | {"brief_id": "b_http_0004"})
    variant_id = created.json()["variants"][0]["id"]

    response = client.post(f"/api/v1/variants/{variant_id}/schedule", json={})

    assert response.status_code == 409
    assert "approved" in response.json()["error"]["message"]


def test_oversized_body_is_rejected(client):
    huge = {"brief_id": "b_huge", "topic": "x" * (3 * 1024 * 1024)}

    response = client.post("/api/v1/briefs", json=huge)

    # 语义上应当是 413；而且服务端要**先把 body 排空再回响应**，
    # 否则客户端看到的是连接被中止而不是一个干净的 413
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_error_response_does_not_leak_internals(client):
    response = client.post("/api/v1/briefs", json={"platforms": ["linkedin"]})

    text = json.dumps(response.json(), ensure_ascii=False)
    assert response.status_code == 400
    assert "Traceback" not in text
    assert "pulse.services" not in text
    assert "token" not in text.lower()


def test_options_preflight_is_supported(client):
    response = client.options("/api/v1/briefs")

    assert response.status_code == 204
    assert "POST" in response.headers["allow"]
