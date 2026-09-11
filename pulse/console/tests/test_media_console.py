"""控制台测试：容量预警、上传、标记使用、召回、HTTP 路由。"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from pulse.console.media_console import MediaConsoleApp, create_server, parse_multipart
from pulse.services.media.config import RecallConfig
from pulse.services.media.describe import Description


class StubDescriber:
    """固定输出的描述器：测试不依赖网络与 .env。"""

    def __init__(
        self,
        *,
        summary: str = "多件阀体铸件整齐堆放",
        details: str = "画面可见多件灰色阀体铸件，表面喷防锈底漆。",
        keywords: tuple[str, ...] = ("阀体", "铸件"),
        source: str = "vision",
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.summary = summary
        self.details = details
        self.keywords = keywords
        self.source = source
        self.warnings = warnings
        self.calls: list[dict] = []

    def __call__(self, data: bytes, *, file_name: str, process: str, sub_process: str) -> Description:
        self.calls.append(
            {"file_name": file_name, "process": process, "sub_process": sub_process, "size": len(data)}
        )
        return Description(
            summary=self.summary,
            details=self.details,
            keywords=self.keywords,
            source=self.source,
            warnings=self.warnings,
        )


def make_app(tmp_path, *, threshold: int = 112, describer=None) -> MediaConsoleApp:
    return MediaConsoleApp(
        rag_root=tmp_path / "RAG知识库" / "图片描述",
        media_root=tmp_path / "菲美得产品图片",
        ledger_path=tmp_path / "ledger.sqlite3",
        config=RecallConfig(capacity_red_threshold=threshold),
        describer=describer,
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


def test_describe_generates_fields_without_user_input(tmp_path) -> None:
    describer = StubDescriber()
    app = make_app(tmp_path, describer=describer)
    result = app.describe(file_name="a.jpg", data=b"\xff\xd8data", process="铸件", sub_process="阀体")
    assert result["ok"] is True
    assert result["source"] == "vision"
    assert result["summary"] == describer.summary
    assert result["keywords"] == ["阀体", "铸件"]
    assert describer.calls[0]["process"] == "铸件"


def test_upload_without_description_falls_back_to_auto(tmp_path) -> None:
    describer = StubDescriber(summary="自动生成的摘要")
    app = make_app(tmp_path, threshold=1, describer=describer)
    result = app.upload(
        file_name="IMG_AUTO.jpg", data=b"\xff\xd8\xff\xe0auto", process="铸件", sub_process="阀体"
    )
    assert result["description_source"] == "vision"
    assert describer.calls, "未提供描述时应调用自动生成"
    description_file = tmp_path / "RAG知识库" / "图片描述" / "铸件" / "阀体" / "IMG_AUTO.md"
    text = description_file.read_text(encoding="utf-8")
    assert "自动生成的摘要" in text
    assert '"阀体", "铸件"' in text


def test_upload_keeps_manual_description_when_given(tmp_path) -> None:
    describer = StubDescriber()
    app = make_app(tmp_path, describer=describer)
    result = app.upload(
        file_name="IMG_MANUAL.jpg",
        data=b"\xff\xd8\xff\xe0manual",
        process="铸件",
        sub_process="阀体",
        summary="人工写的摘要",
        keywords=("人工",),
    )
    assert result["description_source"] == "manual"
    assert describer.calls == []


def test_describe_survives_describer_failure(tmp_path) -> None:
    def broken(data, *, file_name, process, sub_process):
        raise RuntimeError("模型超时")

    app = make_app(tmp_path, describer=broken)
    result = app.describe(file_name="a.jpg", data=b"\xff\xd8x", process="铸件", sub_process="阀体")
    assert result["ok"] is True
    assert result["source"] == "heuristic"
    assert any("视觉模型调用失败" in item for item in result["warnings"])


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
        "/api/describe",
        body=_multipart_body(
            boundary, {"process": "铸件", "sub_process": "阀体"}, "IMG_DESC.jpg", b"\xff\xd8\xff\xe0d"
        ),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    described = json.loads(conn.getresponse().read().decode("utf-8"))
    assert described["ok"] is True
    assert described["summary"]
    assert described["source"] in {"vision", "heuristic"}

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
