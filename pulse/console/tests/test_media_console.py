"""控制台测试：容量预警、上传、标记使用、召回、HTTP 路由。"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from pulse.console.media_console import (
    PAGE_HTML,
    MediaConsoleApp,
    VisionRequiredError,
    create_server,
    parse_multipart,
    resolve_require_vision,
)
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
        process: str = "",
        sub_process: str = "",
        category_source: str = "",
    ) -> None:
        self.summary = summary
        self.details = details
        self.keywords = keywords
        self.source = source
        self.warnings = warnings
        self.process = process
        self.sub_process = sub_process
        self.category_source = category_source
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
            process=self.process,
            sub_process=self.sub_process,
            category_source=self.category_source,
        )


def _seed_categories(root) -> None:
    """预置几种既有品类，模拟真实 RAG 库的目录形态。"""
    for folder in ("铸件/阀体", "铸件/箱体_支座", "加工件/机床件", "人员"):
        (root / folder).mkdir(parents=True, exist_ok=True)


def make_app(
    tmp_path, *, threshold: int = 112, describer=None, require_vision: bool = False
) -> MediaConsoleApp:
    """默认关闭入库校验，便于测试入库/召回等其它路径；门禁本身单独测。"""
    _seed_categories(tmp_path / "RAG知识库" / "图片描述")
    return MediaConsoleApp(
        rag_root=tmp_path / "RAG知识库" / "图片描述",
        media_root=tmp_path / "菲美得产品图片",
        ledger_path=tmp_path / "ledger.sqlite3",
        config=RecallConfig(capacity_red_threshold=threshold),
        describer=describer or StubDescriber(),
        require_vision=require_vision,
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


# ---------- 入库许可：必须先完成视觉识别 ----------


def test_describe_issues_ticket_only_after_vision_success(tmp_path) -> None:
    app = make_app(tmp_path, require_vision=True, describer=StubDescriber())
    result = app.describe(file_name="a.jpg", data=b"\xff\xd8data", process="铸件", sub_process="阀体")
    assert result["vision_required"] is True
    assert result["vision_ticket"], "视觉识别成功必须签发入库凭据"

    fallback = make_app(
        tmp_path / "fallback", require_vision=True, describer=StubDescriber(source="heuristic")
    )
    missed = fallback.describe(
        file_name="a.jpg", data=b"\xff\xd8data", process="铸件", sub_process="阀体"
    )
    assert missed["vision_ticket"] == "", "退回基础信息描述时不得签发凭据"


def test_upload_without_ticket_is_rejected(tmp_path) -> None:
    app = make_app(tmp_path, require_vision=True)
    with pytest.raises(VisionRequiredError):
        app.upload(
            file_name="IMG_X.jpg",
            data=b"\xff\xd8payload",
            process="铸件",
            sub_process="阀体",
            summary="人工填的摘要",
            keywords=("阀体",),
        )
    # 被拒绝后不应写进任何描述文件
    description_dir = tmp_path / "RAG知识库" / "图片描述" / "铸件" / "阀体"
    assert not list(description_dir.glob("*.md"))


def test_upload_with_valid_ticket_succeeds(tmp_path) -> None:
    app = make_app(tmp_path, threshold=1, require_vision=True)
    data = b"\xff\xd8payload"
    ticket = app.describe(
        file_name="IMG_OK.jpg", data=data, process="铸件", sub_process="阀体"
    )["vision_ticket"]
    result = app.upload(
        file_name="IMG_OK.jpg",
        data=data,
        process="铸件",
        sub_process="阀体",
        summary="阀体铸件实拍",
        keywords=("阀体",),
        vision_ticket=ticket,
    )
    assert result["ok"] is True
    assert app.state()["capacity"]["available"] == 1


def test_ticket_is_bound_to_image_content(tmp_path) -> None:
    app = make_app(tmp_path, require_vision=True)
    ticket = app.describe(
        file_name="IMG_A.jpg", data=b"\xff\xd8first", process="铸件", sub_process="阀体"
    )["vision_ticket"]
    with pytest.raises(VisionRequiredError) as excinfo:
        app.upload(
            file_name="IMG_B.jpg",
            data=b"\xff\xd8second",
            process="铸件",
            sub_process="阀体",
            summary="另一张图",
            vision_ticket=ticket,
        )
    assert "不一致" in str(excinfo.value)


def test_upload_rejects_unknown_ticket(tmp_path) -> None:
    app = make_app(tmp_path, require_vision=True)
    with pytest.raises(VisionRequiredError) as excinfo:
        app.upload(
            file_name="IMG_Y.jpg",
            data=b"\xff\xd8payload",
            process="铸件",
            sub_process="阀体",
            summary="摘要",
            vision_ticket="伪造的凭据",
        )
    assert "还没有完成视觉识别" in str(excinfo.value)


def test_upload_without_description_needs_vision_too(tmp_path) -> None:
    """不填描述时由服务端自己识别——识别不成功同样不许入库。"""
    app = make_app(tmp_path, require_vision=True, describer=StubDescriber(source="heuristic"))
    with pytest.raises(VisionRequiredError):
        app.upload(
            file_name="IMG_Z.jpg", data=b"\xff\xd8payload", process="铸件", sub_process="阀体"
        )


def test_gate_can_be_disabled_for_emergency(tmp_path) -> None:
    app = make_app(tmp_path, require_vision=False)
    result = app.upload(
        file_name="IMG_FREE.jpg",
        data=b"\xff\xd8payload",
        process="铸件",
        sub_process="阀体",
        summary="人工摘要",
    )
    assert result["ok"] is True


def test_resolve_require_vision_reads_env(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PULSE_MEDIA_REQUIRE_VISION", raising=False)
    assert resolve_require_vision() is True, "默认必须开启入库校验"
    (tmp_path / ".env").write_text("PULSE_MEDIA_REQUIRE_VISION=0\n", encoding="utf-8")
    assert resolve_require_vision(tmp_path) is False
    (tmp_path / ".env").write_text("PULSE_MEDIA_REQUIRE_VISION=1\n", encoding="utf-8")
    assert resolve_require_vision(tmp_path) is True


def test_page_ships_disabled_button_and_gate_script(tmp_path) -> None:
    app = make_app(tmp_path, require_vision=True)
    assert "id='upload-btn' disabled" in PAGE_HTML
    assert "name='vision_ticket'" in PAGE_HTML
    assert "setGate" in PAGE_HTML
    assert app.require_vision is True


# ---------- 品类：由图片决定，而不是写死 ----------


def test_page_uses_selects_and_fixed_grid(tmp_path) -> None:
    """品类/子类改成下拉框；四列布局用固定网格，避免列宽忽宽忽窄。"""
    assert "id='process-select'" in PAGE_HTML
    assert "id='sub-process-select'" in PAGE_HTML
    assert "<select name='process'" in PAGE_HTML
    assert "grid-template-columns:minmax(0,1.6fr) minmax(0,1fr) minmax(0,1fr) auto" in PAGE_HTML
    assert "auto-fit" not in PAGE_HTML, "auto-fit 是列宽对不齐的根因"
    assert ".row .field label{height:20px" in PAGE_HTML, "标签要同高，输入框才会对齐"


def test_state_exposes_category_options(tmp_path) -> None:
    app = make_app(tmp_path)
    categories = app.state()["categories"]
    labels = {item["label"] for item in categories}
    assert {"铸件/阀体", "加工件/机床件", "人员"} <= labels
    assert all("count" in item for item in categories)


def test_describe_returns_category_from_image(tmp_path) -> None:
    describer = StubDescriber(process="铸件", sub_process="箱体_支座", category_source="vision")
    app = make_app(tmp_path, describer=describer)
    result = app.describe(file_name="a.jpg", data=b"\xff\xd8x", process="", sub_process="")
    assert (result["process"], result["sub_process"]) == ("铸件", "箱体_支座")
    assert result["category_source"] == "vision"


def test_describe_falls_back_to_keyword_category(tmp_path) -> None:
    """描述器没给出品类时，控制台按关键词兜底，而不是用写死的默认值。"""
    describer = StubDescriber(
        summary="车间整齐堆放的多件阀体铸件",
        details="画面为堆放整齐的灰色阀体铸件。",
        keywords=("阀体", "铸件"),
    )
    app = make_app(tmp_path, describer=describer)
    result = app.describe(file_name="a.jpg", data=b"\xff\xd8x", process="", sub_process="")
    assert (result["process"], result["sub_process"]) == ("铸件", "阀体")
    assert result["category_source"] == "keyword"


def test_describe_keeps_manual_choice_when_undecided(tmp_path) -> None:
    describer = StubDescriber(
        summary="一只猫", details="画面里只有猫。", keywords=("猫",)
    )
    app = make_app(tmp_path, describer=describer)
    result = app.describe(
        file_name="a.jpg", data=b"\xff\xd8x", process="加工件", sub_process="机床件"
    )
    assert (result["process"], result["sub_process"]) == ("加工件", "机床件")
    assert result["category_source"] == "manual"


def test_upload_into_singleton_category_without_sub_process(tmp_path) -> None:
    """像"人员"这种没有子目录的品类，也要能入库。"""
    app = make_app(tmp_path, describer=StubDescriber(process="人员", category_source="vision"))
    result = app.upload(
        file_name="IMG_STAFF.jpg",
        data=b"\xff\xd8payload",
        process="人员",
        sub_process="",
        summary="车间人员合影",
    )
    assert result["ok"] is True
    description = tmp_path / "RAG知识库" / "图片描述" / "人员" / "IMG_STAFF.md"
    assert description.is_file()
    text = description.read_text(encoding="utf-8")
    assert 'sub_process: ""' in text
    assert "# 人员 图片描述汇总索引" in (
        tmp_path / "RAG知识库" / "图片描述" / "人员" / "00_汇总索引.md"
    ).read_text(encoding="utf-8")
    assert app.state()["capacity"]["available"] == 1


@pytest.fixture()
def live_server(tmp_path):
    app = make_app(tmp_path, threshold=1, require_vision=True)
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
    # 入库按钮默认必须是灰色不可点的
    assert "id='upload-btn' disabled" in html
    assert "button[disabled]" in html

    boundary = "----pulseHttp"
    # 1) 没有识别凭据就入库 → 必须被服务端挡住
    no_ticket = _multipart_body(
        boundary,
        {"process": "铸件", "sub_process": "阀体", "keywords": "阀体, 铸件", "summary": "阀体实拍"},
        "IMG_HTTP.jpg",
        b"\xff\xd8\xff\xe0http",
    )
    conn.request(
        "POST",
        "/api/assets",
        body=no_ticket,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    upload = conn.getresponse()
    payload = json.loads(upload.read().decode("utf-8"))
    assert upload.status == 403
    assert payload["ok"] is False
    assert payload["vision_required"] is True

    # 2) 先识别拿凭据
    conn.request(
        "POST",
        "/api/describe",
        body=_multipart_body(
            boundary,
            {"process": "铸件", "sub_process": "阀体"},
            "IMG_HTTP.jpg",
            b"\xff\xd8\xff\xe0http",
        ),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    described = json.loads(conn.getresponse().read().decode("utf-8"))
    assert described["ok"] is True
    assert described["summary"]
    assert described["source"] == "vision"
    ticket = described["vision_ticket"]
    assert ticket

    # 3) 带上凭据再入库 → 通过
    with_ticket = _multipart_body(
        boundary,
        {
            "process": "铸件",
            "sub_process": "阀体",
            "keywords": "阀体, 铸件",
            "summary": "阀体实拍",
            "vision_ticket": ticket,
        },
        "IMG_HTTP.jpg",
        b"\xff\xd8\xff\xe0http",
    )
    conn.request(
        "POST",
        "/api/assets",
        body=with_ticket,
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
