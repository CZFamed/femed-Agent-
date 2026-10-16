"""审批台接进素材库控制台的验证：同一进程、真实 HTTP、11 个端点可用。"""

from __future__ import annotations

import threading

import httpx
import pytest

from pulse.api.wiring import build_demo_app
from pulse.console.media_console import MediaConsoleApp, create_server
from pulse.console.pipeline import PipelineConsole
from pulse.services.media.config import RecallConfig
from pulse.services.media.describe import Description


class _StubDescriber:
    """素材库控制台要求一个描述器；审批台测试不碰它，给个最小实现。"""

    def describe(self, image_path, *, extra_note: str = "") -> Description:  # noqa: ANN001
        return Description(summary="测试素材", details="", keywords=(), source="heuristic")


@pytest.fixture()
def console_server(tmp_path):
    """起一个「素材库控制台 + 审批台」的合并服务（真实端口）。"""
    media_app = MediaConsoleApp(
        rag_root=tmp_path / "RAG知识库" / "图片描述",
        media_root=tmp_path / "菲美得产品图片",
        ledger_path=tmp_path / "ledger.sqlite3",
        config=RecallConfig(),
        describer=_StubDescriber(),
        require_vision=False,
    )
    pipeline = PipelineConsole(app=build_demo_app(), demo=True)
    server = create_server(media_app, port=0, pipeline=pipeline)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture()
def client(console_server):
    with httpx.Client(base_url=console_server, timeout=15) as http:
        yield http


BRIEF = {
    "brief_id": "b_console_0001",
    "topic": "Valve body castings with pre-machining",
    "target_audience": "machine tool OEMs in India",
    "platforms": ["linkedin"],
}


def test_pipeline_page_is_served_with_demo_banner(client):
    response = client.get("/pipeline")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "审批台" in body
    assert "演示接线" in body          # 必须把"这是演示"写在页面上
    assert "/api/v1/briefs" in body   # 页面走真实契约端点


def test_media_console_page_links_to_pipeline(client):
    body = client.get("/").text

    assert "/pipeline" in body
    assert "前往内容流水线" in body


def test_media_console_own_routes_still_work(client):
    """接了审批台之后，素材库自己的状态接口不能被抢走。"""
    response = client.get("/api/state")

    assert response.status_code == 200
    assert "capacity" in response.json()


def test_pipeline_state_aggregates_variants_schedules_and_dashboard(client):
    client.post("/api/v1/briefs", json=BRIEF)

    state = client.get("/api/pipeline/state").json()

    assert state["version"]
    assert state["demo"] is True
    assert len(state["variants"]) == 1
    variant = state["variants"][0]
    assert variant["platform"] == "linkedin"
    assert variant["status"] == "draft"
    assert variant["compliance"]["available"] is True
    assert state["accounts"][0]["id"].startswith("acct_")
    assert "kpi_basis" in state["dashboard"]


def test_full_flow_through_console_http(client):
    """生成 → 提交待审 → 通过 → 排期 → 立即发布 → 半自动导出，全走控制台端口。"""
    created = client.post("/api/v1/briefs", json=BRIEF)
    assert created.status_code == 201, created.text
    variant_id = created.json()["variants"][0]["id"]

    submitted = client.patch(
        f"/api/v1/variants/{variant_id}/status",
        json={"action": "submit"},
        headers={"X-Actor": "ops_reviewer"},
    )
    assert submitted.json()["status"] == "pending_review"

    approved = client.patch(
        f"/api/v1/variants/{variant_id}/status",
        json={"action": "approve", "diff": {"note": "参数已核对"}},
        headers={"X-Actor": "brand_reviewer"},
    )
    assert approved.status_code == 200
    assert approved.json()["approval"]["actor"] == "brand_reviewer"

    scheduled = client.post(
        f"/api/v1/variants/{variant_id}/schedule",
        json={"scheduled_at": "2026-10-20T10:00:00+05:30"},
    )
    assert scheduled.status_code == 201, scheduled.text
    schedule_id = scheduled.json()["schedule"]["id"]

    state = client.get("/api/pipeline/state").json()
    assert state["schedules"][0]["id"] == schedule_id
    assert state["schedules"][0]["job"]["unified_post_id"].startswith("up_")

    published = client.post(f"/api/v1/schedules/{schedule_id}/publish", json={})
    assert published.status_code == 200

    bundle = client.get(f"/api/v1/schedules/{schedule_id}/semi-auto").json()
    assert bundle["platform"] == "linkedin"
    assert bundle["text"].strip()
    assert bundle["checklist"]


def test_console_forwards_patch_and_delete_methods(client):
    """PATCH / DELETE 也要能转发（控制台原来只有 GET/POST）。"""
    created = client.post("/api/v1/briefs", json=BRIEF | {"brief_id": "b_console_0002"})
    variant_id = created.json()["variants"][0]["id"]

    patched = client.patch(
        f"/api/v1/variants/{variant_id}/status", json={"action": "submit", "actor": "ops"}
    )
    assert patched.status_code == 200

    account_id = client.get("/api/v1/accounts").json()["accounts"][0]["id"]
    deleted = client.request(
        "DELETE", f"/api/v1/accounts/{account_id}/credential", json={"actor": "admin"}
    )
    assert deleted.status_code in (200, 404)


def test_unapproved_variant_cannot_be_scheduled_from_console(client):
    created = client.post("/api/v1/briefs", json=BRIEF | {"brief_id": "b_console_0003"})
    variant_id = created.json()["variants"][0]["id"]

    response = client.post(f"/api/v1/variants/{variant_id}/schedule", json={})

    assert response.status_code == 409
    assert "approved" in response.json()["error"]["message"]


def test_unknown_api_path_uses_unified_error_body(client):
    response = client.get("/api/v1/nope")

    assert response.status_code == 404
    assert set(response.json()["error"]) == {"code", "message", "details"}


def test_sanctions_screen_from_console_page(client):
    response = client.post(
        "/api/v1/compliance/screen", json={"subject": "Taksan Makina", "checked_by": "ops"}
    )

    body = response.json()["screening"]
    # 项目级风险提示命中时必须是 review_required，且带免责口径
    assert body["result"] in ("review_required", "clear")
    assert body["disclaimer"].startswith("本结论仅表示")


def test_default_wiring_shares_state_across_requests(tmp_path):
    """默认接线（不显式注入 pipeline）必须跨请求共享状态。

    回归守卫：handler 是每请求新建的，如果审批台是"每请求懒建"，
    提交的草稿在下一个请求里就查不到了（页面看起来像什么都没发生）。
    """
    media_app = MediaConsoleApp(
        rag_root=tmp_path / "RAG知识库" / "图片描述",
        media_root=tmp_path / "菲美得产品图片",
        ledger_path=tmp_path / "ledger.sqlite3",
        config=RecallConfig(),
        describer=_StubDescriber(),
        require_vision=False,
    )
    server = create_server(media_app, port=0)  # 不传 pipeline → 默认演示接线
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=15) as http:
            assert http.get("/pipeline").status_code == 200
            assert http.get("/api/pipeline/state").json()["variants"] == []

            created = http.post("/api/v1/briefs", json=BRIEF | {"brief_id": "b_shared_1"})
            assert created.status_code == 201, created.text

            # 第二个请求必须看到第一个请求产生的草稿（同一份内存状态）
            state = http.get("/api/pipeline/state").json()
            assert len(state["variants"]) == 1
            assert state["variants"][0]["id"] == created.json()["variants"][0]["id"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
