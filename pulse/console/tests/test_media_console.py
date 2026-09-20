"""控制台测试：容量预警、上传、标记使用、召回、HTTP 路由。"""

from __future__ import annotations

import http.client
import hashlib
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
    for folder in (
        "铸件/阀体",
        "铸件/箱体_支座",
        "加工件/机床件",
        "生产流程/发泡",
        "人员",
    ):
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


def test_state_exposes_running_version(tmp_path) -> None:
    """启动器靠这个字段判断"端口上那个控制台是不是同一版代码"。"""
    import pulse

    assert make_app(tmp_path).state()["version"] == pulse.__version__


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


def test_sub_process_select_allows_empty_singleton_category() -> None:
    """没有子类的品类（如「人员」）子类下拉的值就是空字符串，不能被 required 拦住。

    2026-09-15 的 BUG：子类下拉带 `required`，而 `人员` 只有一个 value="" 的
    「（无子类）」选项——浏览器把空值判定为"未选择"，直接拦下整张表单，
    表现就是"选了人员，入库按钮点了没反应"。
    """
    assert "id='sub-process-select' required" not in PAGE_HTML, "子类可以为空，不能 required"
    assert "<select name='sub_process' id='sub-process-select'>" in PAGE_HTML
    assert "<select name='process' id='process-select' required>" in PAGE_HTML, "品类仍然必选"


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


# ---------- 通过 HTTP 走一遍"没有子类的品类"入库（2026-09-15 修复的 BUG） ----------


def _serve(app) -> tuple[object, str, int, threading.Thread]:
    server = create_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[0], server.server_address[1], thread


def test_http_singleton_category_keeps_manual_choice_and_ingests(tmp_path) -> None:
    """页面发的是 `process=人员 & sub_process=`；描述看不出品类时也必须保住用户的选择。

    修复前 `/api/describe` 会把空的子类改写成"未分类"，于是
    `catalog.has("人员", "未分类")` 为假 → 用户手选的"人员"被丢掉 →
    页面提示"没能判断出品类"，人也就卡在这儿入不了库。
    """
    app = make_app(
        tmp_path,
        threshold=1,
        require_vision=True,
        describer=StubDescriber(
            summary="几名员工在厂区门口合影",
            details="画面为多人合影，背景是厂区大门。",
            keywords=("合影", "员工"),
        ),
    )
    server, host, port, thread = _serve(app)
    boundary = "----pulseSingleton"
    data = b"\xff\xd8\xff\xe0staff"
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/api/describe",
            body=_multipart_body(
                boundary, {"process": "人员", "sub_process": ""}, "IMG_STAFF.jpg", data
            ),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        described = json.loads(conn.getresponse().read().decode("utf-8"))
        assert described["ok"] is True
        assert described["process"] == "人员", "用户手选的品类不能被丢掉"
        assert described["sub_process"] == ""
        assert described["category_source"] == "manual"
        ticket = described["vision_ticket"]
        assert ticket

        conn.request(
            "POST",
            "/api/assets",
            body=_multipart_body(
                boundary,
                {
                    "process": "人员",
                    "sub_process": "",
                    "keywords": "合影, 员工",
                    "summary": "几名员工在厂区门口合影",
                    "vision_ticket": ticket,
                },
                "IMG_STAFF.jpg",
                data,
            ),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        response = conn.getresponse()
        stored = json.loads(response.read().decode("utf-8"))
        assert response.status == 200, stored
        assert stored["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    rag = tmp_path / "RAG知识库" / "图片描述"
    assert (rag / "人员" / "IMG_STAFF.md").is_file(), "描述要落在 人员 目录下"
    assert not (rag / "人员" / "未分类").exists(), "不能凭空造出「未分类」子类"
    assert (tmp_path / "菲美得产品图片" / "人员" / "IMG_STAFF.jpg").is_file()


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


def test_recall_fills_every_slot_when_library_has_enough_material(tmp_path) -> None:
    """库里有料就必须每格都出图——逐级放宽补足数量。

    2026-09-20 改：原先"位次挑不到就留空"，叠加同档阈值（157 张砍到 4 张）
    与冷却后，库里 468 张图、LinkedIn 四个位次只出得来 1 张——"一堆图却一张也召回不来"。
    现在改为逐级放宽补足数量，同时在 `level` / `relaxed` 里如实标明档次，
    不让人误以为放宽挑来的图是精准匹配。
    """
    app = make_app(tmp_path)
    rag = app.rag_root
    # 只有"阀体"一种题材：四个位次里只有部分能精准匹配，其余靠放宽补足
    for index in range(6):
        _write_description(
            rag, "铸件", "阀体", f"IMG_V{index}.jpg",
            ("阀体", "灰铁铸件"), f"第 {index} 件阀体铸件",
        )
    result = app.recall(platform="facebook", top_k=1)
    assert result["requested"] == 4
    assert result["count"] == 4, "库里有料，每格都要出图"
    assert result["shortfall"] == 0

    relaxed = [slot for slot in result["slots"] if slot["relaxed"]]
    assert relaxed, "没有精准匹配的位次要标注为放宽"
    assert all(slot["level"] != "strict" for slot in relaxed)
    assert all(slot["picks"] for slot in result["slots"]), "放宽后仍应给出图，不能空着"
    # 同一条素材不该在同一帖里被两个位次重复选中
    ids = [pick["asset_id"] for pick in result["picks"]]
    assert len(ids) == len(set(ids))


def test_recall_reports_shortfall_when_library_is_too_small(tmp_path) -> None:
    """素材真的不够时要如实报缺，而不是假装成功。"""
    app = make_app(tmp_path)
    _write_description(
        app.rag_root, "铸件", "阀体", "IMG_ONLY.jpg", ("阀体", "灰铁铸件"), "唯一一件"
    )
    result = app.recall(platform="facebook", top_k=1)
    assert result["count"] < result["requested"], "只有一张图，填不满四格"
    assert result["shortfall"] == result["requested"] - result["count"]
    assert any(slot["picks"] for slot in result["slots"]), "至少给出库里有的那张"


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


def _recalled_slots(app) -> list[dict]:
    """把 LinkedIn 召回结果压成页面回传的 slots 形状。"""
    recalled = app.recall(platform="linkedin", top_k=1)
    return [
        {"order": slot["order"], "asset_id": pick["asset_id"]}
        for slot in recalled["slots"]
        for pick in slot["picks"]
    ]


def test_export_puts_assets_into_cooldown(tmp_path) -> None:
    """点导出 = 这一帖的素材进入发布，素材必须立刻开始计冷却。"""
    app = _export_app(tmp_path)
    slots = _recalled_slots(app)
    assert slots, "先要有可导出的素材"

    result = app.export(platform="linkedin", slots=slots)

    exported = {item["asset_id"] for item in slots}
    assert set(result["used"]) == exported, "导出成功的素材都要记入台账"
    assert result["cooldown_days"] == 15
    cooling = {asset["asset_id"]: asset["cooldown_days_left"] for asset in app.state()["assets"]}
    for asset_id in exported:
        assert cooling[asset_id] == 15, "导出后该素材应显示冷却中"


def test_exported_asset_is_not_recalled_again(tmp_path) -> None:
    """冷却期内同一张素材不得再被召回，否则冷却等于没记。"""
    app = _export_app(tmp_path)
    target = next(slot for slot in app.recall(platform="linkedin", top_k=1)["slots"] if slot["picks"])
    asset_id = target["picks"][0]["asset_id"]

    app.export(platform="linkedin", slots=[{"order": target["order"], "asset_id": asset_id}])

    again = next(
        slot
        for slot in app.recall(platform="linkedin", top_k=1)["slots"]
        if slot["order"] == target["order"]
    )
    assert again["picks"] == [], "刚导出过的素材不应再被召回"
    assert again["blocked_by_cooldown"] is True, "空位次要说清是被冷却挡住"


def test_export_does_not_mark_files_that_failed(tmp_path) -> None:
    """没真的复制进包的素材不记冷却——否则白扣 15 天。"""
    app = _export_app(tmp_path)
    slots = _recalled_slots(app)
    (app.media_root / "加工件" / "机床件" / "IMG_BED.png").unlink()

    result = app.export(platform="linkedin", slots=slots)

    assert result["used"] == []
    assert all(asset["cooldown_days_left"] == 0 for asset in app.state()["assets"])


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


# ---------- 视觉失败要给出真因，而不是笼统指 .env ----------


def test_describe_reports_vision_error_on_failure(tmp_path) -> None:
    def broken(data, *, file_name, process, sub_process):
        raise RuntimeError("视觉接口返回 HTTP 401：API Key 无效或已过期")

    app = make_app(tmp_path, describer=broken)
    result = app.describe(file_name="a.jpg", data=b"\xff\xd8x", process="铸件", sub_process="阀体")
    assert result["source"] == "heuristic"
    assert result["vision_ticket"] == ""
    assert "401" in result["vision_error"], "真实原因必须回传，供页面直接显示"
    assert "Key 无效" in result["vision_error"]


def test_describe_reports_missing_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PULSE_VISION_API_KEY", raising=False)
    _seed_categories(tmp_path / "RAG知识库" / "图片描述")
    app = MediaConsoleApp(
        rag_root=tmp_path / "RAG知识库" / "图片描述",
        media_root=tmp_path / "菲美得产品图片",
        ledger_path=tmp_path / "ledger.sqlite3",
        env_root=tmp_path,  # 该目录没有 .env
    )
    assert app.vision_config.enabled is False
    result = app.describe(file_name="a.jpg", data=b"\xff\xd8x", process="铸件", sub_process="阀体")
    assert result["vision_ticket"] == ""
    assert "PULSE_VISION_API_KEY" in result["vision_error"]


def test_describe_has_no_error_on_success(tmp_path) -> None:
    app = make_app(tmp_path, describer=StubDescriber())
    result = app.describe(file_name="a.jpg", data=b"\xff\xd8x", process="铸件", sub_process="阀体")
    assert result["source"] == "vision"
    assert result["vision_error"] == ""
    assert result["vision_ticket"]


class _ProbeClient:
    """让 probe_vision 走到"能识图"分支的桩件。"""

    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self.payload = payload

    def post(self, url, headers=None, json=None):
        return httpx.Response(self.status, json=self.payload)


def test_vision_check_reports_config_and_stage(tmp_path) -> None:
    app = make_app(tmp_path, describer=StubDescriber())
    app.vision_config = app.vision_config.__class__(api_key="", model="deepseek-v4.1-flash")
    result = app.vision_check()
    assert result["ok"] is False
    assert result["stage"] == "config"
    assert result["key_configured"] is False
    assert result["model"] == "deepseek-v4.1-flash"
    assert "PULSE_VISION_API_KEY" in result["message"]
    assert result["detail"]["export_root"]


def test_vision_check_surfaces_http_error_detail(tmp_path) -> None:
    app = make_app(tmp_path, describer=StubDescriber())
    app.vision_config = app.vision_config.__class__(api_key="bad-key", model="deepseek-v4.1-flash")
    app.caption_client = _ProbeClient(
        401, {"type": "error", "error": {"type": "AuthError", "message": "invalid api key"}}
    )
    result = app.vision_check()
    assert result["ok"] is False
    assert result["stage"] == "request"
    assert "401" in result["message"]
    assert "invalid api key" in result["message"]
    assert result["key_configured"] is True


def test_vision_check_endpoint(live_server) -> None:
    app, host, port = live_server
    app.vision_config = app.vision_config.__class__(api_key="")
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request(
        "POST", "/api/vision-check", body=b"{}", headers={"Content-Type": "application/json"}
    )
    payload = json.loads(conn.getresponse().read().decode("utf-8"))
    assert payload["ok"] is False
    assert payload["stage"] == "config"
    conn.close()


def test_page_ships_vision_check_button(tmp_path) -> None:
    assert "id='vision-check-btn'" in PAGE_HTML
    assert "/api/vision-check" in PAGE_HTML
    assert "vision_error" in PAGE_HTML, "失败提示要带上真实原因"
    assert "视觉模型自检" in PAGE_HTML


# ---------- 视频识别：抽帧后当多图送模型 ----------


SAMPLE_VIDEO = (
    Path(__file__).resolve().parents[3]
    / "pulse"
    / "services"
    / "media"
    / "tests"
    / "assets"
    / "sample.mp4"
)


def _frames_capture_client(captured: dict, text: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured["payload"] = payload
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
                "usage": {
                    "input_tokens": 1569,
                    "output_tokens": 1481,
                    "total_tokens": 3050,
                    "output_tokens_details": {"reasoning_tokens": 1190},
                },
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _video_description() -> str:
    return json.dumps(
        {
            "summary": "车间内流水线的连续画面",
            "details": "同一段视频的连续几帧，镜头自左向右平移拍摄车间流水线。",
            "keywords": ["车间", "流水线", "连续拍摄"],
            "process": "生产流程",
            "sub_process": "发泡",
        },
        ensure_ascii=False,
    )


def test_describe_video_sends_extracted_frames(tmp_path, monkeypatch) -> None:
    """视频不能直接送接口（input_video 被拒），必须抽帧后当多图送过去。"""
    monkeypatch.setenv("PULSE_VISION_API_KEY", "test-key")
    _seed_categories(tmp_path / "RAG知识库" / "图片描述")
    captured: dict = {}
    app = MediaConsoleApp(
        rag_root=tmp_path / "RAG知识库" / "图片描述",
        media_root=tmp_path / "菲美得产品图片",
        ledger_path=tmp_path / "ledger.sqlite3",
        describer=StubDescriber(),
        require_vision=True,
        caption_client=_frames_capture_client(captured, _video_description()),
        env_root=tmp_path,
    )
    data = SAMPLE_VIDEO.read_bytes()
    result = app.describe(
        file_name="sample.mp4", data=data, process="生产流程", sub_process="发泡"
    )
    assert result["source"] == "vision"
    assert result["vision_ticket"], "视频识别成功同样要签发入库凭据"
    assert result["process"] == "生产流程"

    parts = captured["payload"]["input"][0]["content"]
    images = [part for part in parts if part.get("type") == "input_image"]
    assert len(images) == app.vision_config.video_frames, "应当送抽出的多张静帧"
    assert all(
        part["image_url"].startswith("data:image/jpeg;base64,") for part in images
    ), "送出去的必须是 JPEG 帧，不能是视频字节"
    assert "同一段视频" in parts[0]["text"], "要告诉模型这些帧来自同一段视频"
    assert "同一段视频" in captured["payload"]["instructions"]


def test_describe_video_reports_token_usage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PULSE_VISION_API_KEY", "test-key")
    _seed_categories(tmp_path / "RAG知识库" / "图片描述")
    app = MediaConsoleApp(
        rag_root=tmp_path / "RAG知识库" / "图片描述",
        media_root=tmp_path / "菲美得产品图片",
        ledger_path=tmp_path / "ledger.sqlite3",
        describer=StubDescriber(),
        require_vision=True,
        caption_client=_frames_capture_client({}, _video_description()),
        env_root=tmp_path,
    )
    result = app.describe(
        file_name="sample.mp4", data=SAMPLE_VIDEO.read_bytes(), process="生产流程",
        sub_process="发泡",
    )
    usage = result["vision_usage"]
    assert "输入 1569" in usage and "其中思考 1190" in usage, "成本要可见"


def test_describe_video_falls_back_when_frames_fail(tmp_path, monkeypatch) -> None:
    """抽帧失败（比如文件损坏）不能崩，退回基础描述并把原因说清楚。"""
    monkeypatch.setenv("PULSE_VISION_API_KEY", "test-key")
    _seed_categories(tmp_path / "RAG知识库" / "图片描述")
    app = MediaConsoleApp(
        rag_root=tmp_path / "RAG知识库" / "图片描述",
        media_root=tmp_path / "菲美得产品图片",
        ledger_path=tmp_path / "ledger.sqlite3",
        describer=StubDescriber(),
        require_vision=True,
        env_root=tmp_path,
    )
    result = app.describe(
        file_name="broken.mp4", data=b"definitely not a video", process="生产流程",
        sub_process="发泡",
    )
    assert result["source"] == "heuristic"
    assert result["vision_ticket"] == ""
    assert result["vision_error"], "抽帧失败的原因要带出来"


def test_upload_video_writes_mp4_md(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PULSE_VISION_API_KEY", "test-key")
    _seed_categories(tmp_path / "RAG知识库" / "图片描述")
    app = MediaConsoleApp(
        rag_root=tmp_path / "RAG知识库" / "图片描述",
        media_root=tmp_path / "菲美得产品图片",
        ledger_path=tmp_path / "ledger.sqlite3",
        describer=StubDescriber(),
        require_vision=True,
        caption_client=_frames_capture_client({}, _video_description()),
        env_root=tmp_path,
    )
    data = SAMPLE_VIDEO.read_bytes()
    described = app.describe(
        file_name="sample.mp4", data=data, process="生产流程", sub_process="发泡"
    )
    stored = app.upload(
        file_name="sample.mp4",
        data=data,
        process="生产流程",
        sub_process="发泡",
        summary=described["summary"],
        keywords=tuple(described["keywords"]),
        vision_ticket=described["vision_ticket"],
    )
    assert stored["ok"] is True
    description = (
        app.rag_root / "生产流程" / "发泡" / "sample.mp4.md"
    )
    assert description.is_file()
    assert 'content_type: "视频描述"' in description.read_text(encoding="utf-8")
    state = app.state()
    assert state["capacity"]["videos"] == 1
    assert state["capacity"]["total"] == 0, "视频不占图片容量"


def test_upload_accepts_video_files_in_the_page() -> None:
    """上传框要同时收图、视频与 Photoshop 文档（.psd 不在 image/* 里，必须显式列）。"""
    assert "accept='image/*,video/*,.psd'" in PAGE_HTML


# ---------- 先查重再识别（省识别费） ----------


def _counting_describer() -> StubDescriber:
    return StubDescriber()


def test_describe_skips_recognition_for_registry_duplicate(tmp_path) -> None:
    """同一份内容第二次上传，必须查重命中并**完全不调用模型**。"""
    describer = _counting_describer()
    app = make_app(tmp_path, describer=describer, require_vision=True)
    data = _png_bytes()
    first = app.upload(
        file_name="IMG_DUP.jpg",
        data=data,
        process="铸件",
        sub_process="阀体",
        summary="阀体铸件",
        vision_ticket=app.describe(
            file_name="IMG_DUP.jpg", data=data, process="铸件", sub_process="阀体"
        )["vision_ticket"],
    )
    assert first["ok"] is True
    calls_after_ingest = len(describer.calls)

    again = app.describe(
        file_name="IMG_DUP_2.jpg", data=data, process="铸件", sub_process="阀体"
    )
    assert again["duplicate"] is True
    assert again["vision_ticket"] == "", "重复内容不签发入库凭据"
    assert "没有产生任何模型费用" in again["message"]
    assert again["existing"]["file_name"] == "IMG_DUP.jpg"
    assert len(describer.calls) == calls_after_ingest, "重复内容不得再调用模型"


def test_describe_skips_recognition_for_existing_library_file(tmp_path) -> None:
    """既有素材库（老图，登记表里没有记录）同样要能查出来。"""
    describer = _counting_describer()
    app = make_app(tmp_path, describer=describer, require_vision=True)
    media_dir = app.media_root / "铸件" / "阀体"
    media_dir.mkdir(parents=True, exist_ok=True)
    data = _png_bytes()
    (media_dir / "IMG_OLD.jpg").write_bytes(data)

    result = app.describe(
        file_name="换个名字再传.jpg", data=data, process="铸件", sub_process="阀体"
    )
    assert result["duplicate"] is True, "内容相同就该认出来，跟文件名无关"
    assert result["existing"]["where"] == "library"
    assert result["existing"]["file_name"] == "IMG_OLD.jpg"
    assert describer.calls == [], "命中既有素材时也不该调用模型"


def test_describe_recognises_new_content(tmp_path) -> None:
    describer = _counting_describer()
    app = make_app(tmp_path, describer=describer, require_vision=True)
    result = app.describe(
        file_name="IMG_NEW.jpg", data=_png_bytes(), process="铸件", sub_process="阀体"
    )
    assert result["duplicate"] is False
    assert result["vision_ticket"], "新内容照常识别并签发凭据"
    assert len(describer.calls) == 1


def test_precheck_endpoint_reports_duplicate(tmp_path) -> None:
    app = make_app(tmp_path)
    data = _png_bytes()

    digest = hashlib.sha256(data).hexdigest()
    fresh = app.precheck(content_hash=digest, size=len(data))
    assert fresh == {"ok": True, "duplicate": False}

    media_dir = app.media_root / "铸件" / "阀体"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "IMG_P.jpg").write_bytes(data)
    hit = app.precheck(content_hash=digest, size=len(data))
    assert hit["duplicate"] is True
    assert hit["existing"]["file_name"] == "IMG_P.jpg"
    assert "零" in hit["message"] or "没有产生任何模型费用" in hit["message"]


def test_precheck_size_filter_avoids_hashing_everything(tmp_path, monkeypatch) -> None:
    """按字节数过滤后再算哈希：1.3 GB 素材库不该每次上传都重算。"""
    app = make_app(tmp_path)
    media_dir = app.media_root / "铸件" / "阀体"
    media_dir.mkdir(parents=True, exist_ok=True)
    for index in range(5):
        (media_dir / f"IMG_{index}.jpg").write_bytes(b"x" * (100 + index))

    hashed: list[str] = []
    real_sha256 = hashlib.sha256

    def counting_sha256(data=b"", **kwargs):
        if isinstance(data, (bytes, bytearray)):
            hashed.append("x")
        return real_sha256(data, **kwargs)

    monkeypatch.setattr("pulse.console.media_console.hashlib.sha256", counting_sha256)
    # 尺寸对不上任何文件 → 一个文件都不该被哈希（只算候选本身那一次）
    app.precheck(content_hash="a" * 64, size=999999)
    assert len(hashed) == 0, "尺寸不匹配时不该读取并哈希任何库内文件"
    # 尺寸刚好对上 1 个 → 只哈希那 1 个
    app.precheck(content_hash="a" * 64, size=101)
    assert len(hashed) == 1, "同尺寸的才需要算哈希"


def test_video_duplicate_also_skips_recognition(tmp_path) -> None:
    """视频更贵（一次约 3,000 token），重复的更不能送去识别。"""
    app = make_app(tmp_path, describer=_counting_describer(), require_vision=True)
    media_dir = app.media_root / "生产流程" / "发泡"
    media_dir.mkdir(parents=True, exist_ok=True)
    data = SAMPLE_VIDEO.read_bytes()
    (media_dir / "clip.mp4").write_bytes(data)

    result = app.describe(
        file_name="clip.mp4", data=data, process="生产流程", sub_process="发泡"
    )
    assert result["duplicate"] is True
    assert result["vision_ticket"] == ""
    assert "clip.mp4" in result["message"]


def test_page_ships_precheck_ui() -> None:
    assert "/api/precheck" in PAGE_HTML
    assert "sha256Hex" in PAGE_HTML
    assert "markDuplicate" in PAGE_HTML
    assert "crypto.subtle.digest('SHA-256'" in PAGE_HTML


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
