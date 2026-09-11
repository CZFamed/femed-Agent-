"""控制台测试：容量预警、上传、标记使用、召回、HTTP 路由。"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from pulse.console.media_console import MediaConsoleApp, create_server, parse_multipart
from pulse.services.media.config import RecallConfig


def make_app(tmp_path, *, threshold: int = 112) -> MediaConsoleApp:
    return MediaConsoleApp(
        rag_root=tmp_path / "RAG知识库" / "图片描述",
        media_root=tmp_path / "菲美得产品图片",
        ledger_path=tmp_path / "ledger.sqlite3",
        config=RecallConfig(capacity_red_threshold=threshold),
    )


def test_parse_multipart_extracts_fields_and_file() -> None:
    boundary = "----pulse"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"process\"\r\n\r\n铸件\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.jpg\"\r\n"
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode("utf-8") + b"\xff\xd8\xff\xe0data" + f"\r\n--{boundary}--\r\n".encode("utf-8")
    fields, files = parse_multipart(body, boundary)
    assert fields["process"] == "铸件"
    assert files["file"][0] == "a.jpg"
    assert files["file"][1] == b"\xff\xd8\xff\xe0data"


def test_state_reports_red_alert_when_capacity_is_low(tmp_path) -> None:
    app = make_app(tmp_path, threshold=2)
    state = app.state()
    assert state["capacity"]["available"] == 0
    assert state["capacity"]["alert"] == "red"
    assert state["config"]["cooldown_days"] == 15
    assert state["brand"] == "沧州菲美得"


def test_upload_then_recall_then_mark_used(tmp_path) -> None:
    app = make_app(tmp_path, threshold=1)
    result = app.upload(
        file_name="IMG_9001.jpg",
        data=b"\xff\xd8\xff\xe0payload",
        process="加工件",
        sub_process="机床件",
        keywords=("机床床身",),
        summary="机床床身实拍",
    )
    assert result["ok"] is True
    state = app.state()
    assert state["capacity"]["available"] == 1
    assert state["capacity"]["alert"] == "ok"
    assert state["assets"][0]["status"] == "new"

    picks = app.recall(query="机床 床身", top_k=1)["picks"]
    assert [pick["file_name"] for pick in picks] == ["IMG_9001.jpg"]
    assert picks[0]["is_new"] is True

    assert app.mark_used(asset_id=result["asset_id"], content_id="var_1")["ok"] is True
    state = app.state()
    assert state["capacity"]["cooling"] == 1
    assert state["capacity"]["available"] == 0
    assert state["capacity"]["alert"] == "red"
    assert state["assets"][0]["status"] == "cooling"
    assert state["assets"][0]["cooldown_days_left"] == 15
    assert app.recall(query="机床 床身")["picks"] == []


@pytest.fixture()
def live_server(tmp_path):
    app = make_app(tmp_path, threshold=1)
    server = create_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, server.server_address[0], server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _multipart_body(boundary: str, fields: dict[str, str], file_name: str, data: bytes) -> bytes:
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
            f"{value}\r\n".encode("utf-8")
        )
    chunks.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{file_name}\"\r\n"
        "Content-Type: image/jpeg\r\n\r\n".encode("utf-8") + data + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks)


def test_http_routes(live_server) -> None:
    _app, host, port = live_server
    conn = http.client.HTTPConnection(host, port, timeout=5)

    conn.request("GET", "/")
    page = conn.getresponse()
    html = page.read().decode("utf-8")
    assert page.status == 200
    assert "Pulse 素材库控制台" in html

    boundary = "----pulseHttp"
    body = _multipart_body(
        boundary,
        {"process": "铸件", "sub_process": "阀体", "keywords": "阀体, 铸件", "summary": "阀体实拍"},
        "IMG_HTTP.jpg",
        b"\xff\xd8\xff\xe0http",
    )
    conn.request(
        "POST",
        "/api/assets",
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    upload = conn.getresponse()
    payload = json.loads(upload.read().decode("utf-8"))
    assert upload.status == 200
    assert payload["ok"] is True

    conn.request("GET", "/api/state")
    state = json.loads(conn.getresponse().read().decode("utf-8"))
    assert state["capacity"]["available"] == 1
    asset_id = state["assets"][0]["asset_id"]

    conn.request(
        "POST",
        "/api/usage",
        body=json.dumps({"asset_id": asset_id, "content_id": "var_http"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    usage = json.loads(conn.getresponse().read().decode("utf-8"))
    assert usage["ok"] is True

    conn.request(
        "POST",
        "/api/recall",
        body=json.dumps({"query": "阀体", "top_k": 3}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    recall = json.loads(conn.getresponse().read().decode("utf-8"))
    assert recall["picks"] == []

    conn.request(
        "POST",
        "/api/assets",
        body=_multipart_body(
            boundary, {"process": "铸件", "sub_process": "阀体"}, "bad.txt", b"nope"
        ),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    bad = conn.getresponse()
    assert bad.status == 400
    conn.close()
