"""控制台测试：容量预警、上传、标记使用、召回、HTTP 路由。"""

from __future__ import annotations

import http.client
import json
import re
import struct
import threading
from pathlib import Path

import httpx
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


# ---------- 按平台召回 ----------


def _write_description(
    rag_root,
    process: str,
    sub_process: str,
    file_name: str,
    keywords: tuple[str, ...],
    summary: str,
    *,
    source_name: str | None = None,
) -> None:
    """写一份与库存格式一致的描述，供召回匹配。"""
    folder = rag_root / process / sub_process
    folder.mkdir(parents=True, exist_ok=True)
    keyword_text = ", ".join(f'"{item}"' for item in keywords)
    (folder / f"{file_name}.md").write_text(
        "---\n"
        f'source_file: "{source_name or file_name}"\n'
        f'process: "{process}"\n'
        f'sub_process: "{sub_process}"\n'
        'content_type: "图片描述"\n'
        f"keywords: [{keyword_text}]\n"
        "---\n\n"
        f"# {file_name} 图片描述\n\n"
        f"## {summary}\n",
        encoding="utf-8",
    )


def test_state_exposes_platform_profiles(tmp_path) -> None:
    app = make_app(tmp_path)
    platforms = app.state()["platforms"]
    assert [item["key"] for item in platforms] == ["linkedin", "facebook", "tiktok", "vk"]
    assert platforms[0]["name"] == "LinkedIn"
    assert platforms[0]["priority"] == "P0-A"
    assert platforms[0]["slot_count"] == 4


def test_page_ships_platform_dropdown(tmp_path) -> None:
    assert "id='platform-select'" in PAGE_HTML
    assert "<select name='platform'" in PAGE_HTML
    assert "applyPlatforms" in PAGE_HTML
    assert "platform-summary" in PAGE_HTML


def test_recall_by_platform_matches_each_slot(tmp_path) -> None:
    app = make_app(tmp_path)
    rag = app.rag_root
    _write_description(
        rag, "加工件", "机床件", "IMG_BED.jpg",
        ("机床床身", "导轨面", "机加工", "铸铁件"), "大型机床床身导轨面加工",
    )
    _write_description(
        rag, "铸件", "扫描", "IMG_SCAN.jpg",
        ("三维扫描", "尺寸检测", "点云", "偏差色谱"), "铸件三维扫描尺寸检测",
    )
    _write_description(
        rag, "铸件", "阀体", "IMG_PACK.jpg",
        ("木托盘", "成批包装", "编号标识", "灰铁铸件"), "托盘打包待发的小型阀体",
    )
    result = app.recall(platform="linkedin", top_k=1)
    assert result["platform"]["key"] == "linkedin"
    assert [slot["order"] for slot in result["slots"]] == [1, 2, 3, 4]

    by_order = {slot["order"]: slot for slot in result["slots"]}
    assert by_order[1]["picks"][0]["file_name"] == "IMG_BED.jpg", "能力证明该挑床身"
    assert by_order[3]["picks"][0]["file_name"] == "IMG_SCAN.jpg", "质量能力该挑扫描"
    assert by_order[4]["picks"][0]["file_name"] == "IMG_PACK.jpg", "交付能力该挑待发"
    assert result["count"] == len(result["picks"])


def test_recall_by_platform_reports_empty_slots(tmp_path) -> None:
    """没有合适素材的位次要明确留空，让页面提示补拍，而不是随便凑一张。"""
    app = make_app(tmp_path)
    _write_description(
        app.rag_root, "铸件", "阀体", "IMG_ONLY.jpg", ("阀体", "灰铁铸件"), "一件阀体铸件"
    )
    result = app.recall(platform="facebook", top_k=1)
    by_order = {slot["order"]: slot for slot in result["slots"]}
    # Facebook 第 2 位次要求"白模/发泡"，库里只有阀体 → 应为空
    assert by_order[2]["picks"] == []
    assert by_order[2]["matched"] == 0
    assert by_order[2]["blocked_by_cooldown"] is False, "本来就没这种素材，不是被冷却挡住"
    assert "补拍" not in by_order[2]["role"], "提示语由页面负责，接口只给空结果"


def test_empty_slot_distinguishes_cooldown_from_missing(tmp_path) -> None:
    """"有但都在冷却期"和"根本没有"要能区分开，否则会让人白跑一趟补拍。"""
    app = make_app(tmp_path)
    _write_description(
        app.rag_root, "加工件", "机床件", "IMG_BED.jpg",
        ("机床床身", "导轨面", "机加工"), "机床床身加工",
    )
    slot_before = app.recall(platform="linkedin", top_k=1)["slots"][0]
    assert slot_before["matched"] == 1 and slot_before["blocked_by_cooldown"] is False

    app.mark_used(asset_id=slot_before["picks"][0]["asset_id"])
    slot_after = app.recall(platform="linkedin", top_k=1)["slots"][0]
    assert slot_after["picks"] == []
    assert slot_after["matched"] == 1
    assert slot_after["blocked_by_cooldown"] is True


def test_recall_by_platform_respects_cooldown(tmp_path) -> None:
    app = make_app(tmp_path)
    rag = app.rag_root
    for index in range(2):
        _write_description(
            rag, "加工件", "机床件", f"IMG_BED{index}.jpg",
            ("机床床身", "导轨面", "机加工"), f"机床床身 {index}",
        )
    first = app.recall(platform="linkedin", top_k=1)["slots"][0]["picks"][0]["asset_id"]
    app.mark_used(asset_id=first)
    again = app.recall(platform="linkedin", top_k=1)["slots"][0]["picks"]
    assert again, "还有第二张可用，不该整格空掉"
    assert all(pick["asset_id"] != first for pick in again), "冷却中的素材不得再被召回"


def test_recall_without_platform_keeps_keyword_behaviour(tmp_path) -> None:
    app = make_app(tmp_path)
    result = app.recall(query="阀体", top_k=3)
    assert result["platform"] is None
    assert result["slots"] == []


# ---------- 预览 ----------


def _png_bytes() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", 4, 4)
        + b"\x08\x02\x00\x00\x00"
    )


def test_media_path_resolves_despite_stale_source_path(tmp_path) -> None:
    """描述头部的 source_path 写的是别的机器的绝对路径，不能拿它定位文件。"""
    app = make_app(tmp_path)
    rag = app.rag_root
    _write_description(
        rag, "铸件", "阀体", "IMG_V.jpg", ("阀体", "灰铁铸件"), "一件阀体铸件"
    )
    media_dir = tmp_path / "菲美得产品图片" / "铸件" / "阀体"
    media_dir.mkdir(parents=True, exist_ok=True)
    payload = _png_bytes()
    (media_dir / "IMG_V.jpg").write_bytes(payload)

    asset = next(a for a in app.assets() if a.file_name == "IMG_V.jpg")
    resolved = app.media_path(asset)
    assert resolved is not None and resolved.name == "IMG_V.jpg"


def test_media_path_returns_none_when_file_missing(tmp_path) -> None:
    app = make_app(tmp_path)
    _write_description(app.rag_root, "铸件", "阀体", "GONE.jpg", ("阀体",), "已删除的图")
    asset = next(a for a in app.assets() if a.file_name == "GONE.jpg")
    assert app.media_path(asset) is None


def test_media_path_rejects_traversal(tmp_path) -> None:
    """描述头部里的文件名带路径时不得读到库外。"""
    app = make_app(tmp_path)
    _write_description(
        app.rag_root,
        "铸件",
        "阀体",
        "IMG_EVIL",
        ("阀体",),
        "越界尝试",
        source_name="../../../../secret.jpg",
    )
    (tmp_path / "secret.jpg").write_bytes(b"top secret")
    asset = next(a for a in app.assets() if "secret" in a.file_name)
    assert app.media_path(asset) is None


def test_asset_image_endpoint_serves_bytes(live_server) -> None:
    app, host, port = live_server
    _write_description(
        app.rag_root, "铸件", "阀体", "IMG_VIEW.png", ("阀体", "灰铁铸件"), "可预览的阀体"
    )
    media_dir = app.media_root / "铸件" / "阀体"
    media_dir.mkdir(parents=True, exist_ok=True)
    payload = _png_bytes()
    (media_dir / "IMG_VIEW.png").write_bytes(payload)
    asset = next(a for a in app.assets() if a.file_name == "IMG_VIEW.png")

    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", f"/api/asset-image?asset_id={asset.asset_id}")
    response = conn.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type") == "image/png"
    assert response.getheader("Accept-Ranges") == "bytes"
    assert response.read() == payload

    # Range 请求必须回 206，否则视频无法拖动进度
    conn.request(
        "GET",
        f"/api/asset-image?asset_id={asset.asset_id}",
        headers={"Range": "bytes=0-7"},
    )
    partial = conn.getresponse()
    assert partial.status == 206
    assert partial.getheader("Content-Range") == f"bytes 0-7/{len(payload)}"
    assert partial.read() == payload[:8]

    conn.request("GET", "/api/asset-image?asset_id=img_nope")
    assert conn.getresponse().status == 404
    conn.close()


# ---------- 短文生成 ----------


def _caption_client(text: str, hashtags: list[str], **extra):
    payload = {"text": text, "hashtags": hashtags, **extra}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(payload, ensure_ascii=False),
                            }
                        ],
                    }
                ]
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _caption_app(tmp_path, *, client):
    _seed_categories(tmp_path / "RAG知识库" / "图片描述")
    return MediaConsoleApp(
        rag_root=tmp_path / "RAG知识库" / "图片描述",
        media_root=tmp_path / "菲美得产品图片",
        ledger_path=tmp_path / "ledger.sqlite3",
        config=RecallConfig(capacity_red_threshold=1),
        describer=StubDescriber(),
        require_vision=False,
        caption_client=client,
        env_root=tmp_path,
    )


def test_state_exposes_caption_specs(tmp_path) -> None:
    app = make_app(tmp_path)
    specs = {item["key"]: item for item in app.state()["caption_specs"]}
    assert set(specs) == {"linkedin", "facebook", "tiktok", "vk"}
    assert specs["linkedin"]["structure_text"].startswith("钩子行")
    assert specs["tiktok"]["max_chars"] == 150


def test_caption_uses_platform_recall_when_no_ids(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PULSE_VISION_API_KEY", "test-key")
    text = "Most machine tool builders don't own a foundry. " + "Casting and machining. " * 30
    app = _caption_app(
        tmp_path,
        client=_caption_client(text, ["#casting", "#foundry", "#machinetools"]),
    )
    _write_description(
        app.rag_root, "加工件", "机床件", "IMG_BED.jpg",
        ("机床床身", "导轨面", "机加工"), "大型机床床身加工",
    )
    result = app.caption(platform="linkedin")
    assert result["ok"] is True
    assert result["platform_name"] == "LinkedIn"
    assert result["material_count"] >= 1
    assert result["hashtags"] == ["#casting", "#foundry", "#machinetools"]
    assert result["checks"], "必须回传逐条校验结果"
    assert result["char_count"] == len(text.strip())


def test_caption_rejects_platform_without_material(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PULSE_VISION_API_KEY", "test-key")
    app = _caption_app(tmp_path, client=_caption_client("x", ["#a"]))
    with pytest.raises(ValueError, match="没有可用素材"):
        app.caption(platform="tiktok")


def test_caption_rejects_unknown_platform(tmp_path) -> None:
    app = make_app(tmp_path)
    with pytest.raises(ValueError, match="不支持的平台"):
        app.caption(platform="weibo")


def test_page_ships_preview_and_caption_ui(tmp_path) -> None:
    assert "/api/asset-image?asset_id=" in PAGE_HTML
    assert "class='thumb'" in PAGE_HTML
    assert "id='caption-btn'" in PAGE_HTML
    assert "id='caption-checks'" in PAGE_HTML
    assert "/api/caption" in PAGE_HTML


# ---------- 导出 ----------


def _export_app(tmp_path):
    """召回一条 LinkedIn 素材并落地对应文件，"所见即所得"地测导出。"""
    _seed_categories(tmp_path / "RAG知识库" / "图片描述")
    app = MediaConsoleApp(
        rag_root=tmp_path / "RAG知识库" / "图片描述",
        media_root=tmp_path / "菲美得产品图片",
        ledger_path=tmp_path / "ledger.sqlite3",
        describer=StubDescriber(),
        require_vision=False,
        export_root=tmp_path / "Pulse导出",
    )
    _write_description(
        app.rag_root, "加工件", "机床件", "IMG_BED.png",
        ("机床床身", "导轨面", "机加工"), "大型机床床身导轨面加工",
    )
    media_dir = app.media_root / "加工件" / "机床件"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "IMG_BED.png").write_bytes(_png_bytes())
    return app


def test_export_writes_package_for_recalled_slots(tmp_path) -> None:
    app = _export_app(tmp_path)
    recalled = app.recall(platform="linkedin", top_k=1)
    slots = [
        {"order": slot["order"], "role": slot["role"], "asset_id": pick["asset_id"]}
        for slot in recalled["slots"]
        for pick in slot["picks"]
    ]
    result = app.export(platform="linkedin", slots=slots)
    assert result["ok"] is True
    assert result["file_count"] == 1
    directory = Path(result["directory"])
    assert directory.is_dir()
    assert (directory / "说明.txt").is_file()
    copied = directory / result["copied"][0]
    assert copied.read_bytes() == _png_bytes(), "必须是原文件字节"


def test_export_includes_caption_file_when_given(tmp_path) -> None:
    app = _export_app(tmp_path)
    recalled = app.recall(platform="linkedin", top_k=1)
    slots = [
        {"order": slot["order"], "asset_id": pick["asset_id"]}
        for slot in recalled["slots"]
        for pick in slot["picks"]
    ]
    result = app.export(
        platform="linkedin",
        slots=slots,
        caption={"platform_name": "LinkedIn", "text": "hello", "hashtags": ["#casting"]},
    )
    text = (Path(result["directory"]) / "文案.txt").read_text(encoding="utf-8-sig")
    assert "hello" in text and "#casting" in text


def test_export_rejects_empty_slots(tmp_path) -> None:
    app = _export_app(tmp_path)
    with pytest.raises(ValueError, match="没有可导出的素材"):
        app.export(platform="linkedin", slots=[])


def test_export_rejects_unknown_platform(tmp_path) -> None:
    app = _export_app(tmp_path)
    with pytest.raises(ValueError, match="不支持的平台"):
        app.export(platform="weibo", slots=[{"order": 1}])


def test_export_reports_missing_media_file(tmp_path) -> None:
    """素材文件不在磁盘上时要如实报告，而不是静默少导一张。"""
    app = _export_app(tmp_path)
    recalled = app.recall(platform="linkedin", top_k=1)
    slots = [
        {"order": slot["order"], "asset_id": pick["asset_id"]}
        for slot in recalled["slots"]
        for pick in slot["picks"]
    ]
    (app.media_root / "加工件" / "机床件" / "IMG_BED.png").unlink()
    result = app.export(platform="linkedin", slots=slots)
    assert result["file_count"] == 0
    assert result["missing"]


def test_export_endpoint_returns_directory(live_server) -> None:
    app, host, port = live_server
    _write_description(
        app.rag_root, "加工件", "机床件", "IMG_EXP.png", ("机床床身",), "可导出的床身"
    )
    media_dir = app.media_root / "加工件" / "机床件"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "IMG_EXP.png").write_bytes(_png_bytes())
    asset = next(a for a in app.assets() if a.file_name == "IMG_EXP.png")

    conn = http.client.HTTPConnection(host, port, timeout=5)
    body = json.dumps(
        {"platform": "linkedin", "slots": [{"order": 1, "asset_id": asset.asset_id}]}
    ).encode("utf-8")
    conn.request(
        "POST", "/api/export", body=body, headers={"Content-Type": "application/json"}
    )
    payload = json.loads(conn.getresponse().read().decode("utf-8"))
    assert payload["ok"] is True
    assert payload["file_count"] == 1
    assert Path(payload["directory"]).is_dir()
    conn.close()


def test_page_ships_export_ui(tmp_path) -> None:
    assert "id='export-btn'" in PAGE_HTML
    assert "/api/export" in PAGE_HTML
    assert "renderExportButton" in PAGE_HTML


# ---------- 可读性：禁用态按钮的文字不能被"洗掉" ----------


def _relative_luminance(color: str) -> float:
    value = color.lstrip("#")
    if len(value) == 3:  # #fff → #ffffff
        value = "".join(char * 2 for char in value)
    parts = [int(value[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    converted = [
        value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
        for value in parts
    ]
    return 0.2126 * converted[0] + 0.7152 * converted[1] + 0.0722 * converted[2]


def _contrast(foreground: str, background: str) -> float:
    a, b = _relative_luminance(foreground), _relative_luminance(background)
    high, low = max(a, b), min(a, b)
    return (high + 0.05) / (low + 0.05)


def test_disabled_button_label_stays_readable() -> None:
    """禁用态按钮的文字必须能读出来。

    曾经用近白字配浅灰底（对比度 1.42:1），按钮看着就是个灰块——
    "上传并入库"和"导出这一帖的素材"都默认禁用，标签等于不存在。
    """
    match = re.search(
        r"button\[disabled\]\{background:(#[0-9a-f]{3,6});color:(#[0-9a-f]{3,6})", PAGE_HTML
    )
    assert match, "找不到禁用态按钮样式"
    background, foreground = match.group(1), match.group(2)
    ratio = _contrast(foreground, background)
    assert ratio >= 4.5, f"禁用态文字对比度只有 {ratio:.2f}:1，读不出来（要求 ≥ 4.5:1）"


def test_enabled_button_label_is_readable() -> None:
    match = re.search(r"button\{background:(#[0-9a-f]{3,6});color:(#[0-9a-f]{3,6})", PAGE_HTML)
    assert match, "找不到按钮基础样式"
    assert _contrast(match.group(2), match.group(1)) >= 4.5


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

    # 指定平台时走位次召回，平台信息要原样透传回前端
    conn.request(
        "POST",
        "/api/recall",
        body=json.dumps({"platform": "linkedin", "top_k": 1}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    by_platform = json.loads(conn.getresponse().read().decode("utf-8"))
    assert by_platform["platform"]["key"] == "linkedin"
    assert len(by_platform["slots"]) == 4

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
