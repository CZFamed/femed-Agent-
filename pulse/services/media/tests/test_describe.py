"""素材描述自动生成测试：图片信息解析、兜底描述、视觉模型、参数防编造。"""

from __future__ import annotations

import json
import struct
from datetime import datetime

import httpx
import pytest

from pulse.services.media.describe import (
    SYSTEM_PROMPT,
    TextOnlyModelError,
    VisionAPIError,
    VisionConfig,
    VisionDescriber,
    build_describer,
    find_unverified_claims,
    heuristic_describe,
    parse_capture_time,
    probe_png,
    probe_image,
    probe_vision,
    vision_config_from_env,
    vision_model_supports_images,
)


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def _jpeg(width: int, height: int) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width)
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"\x00\x00" + sof + b"\xff\xd9"


def test_probe_image_reads_png_and_jpeg_sizes() -> None:
    png = probe_image(_png(4032, 3024))
    assert (png.fmt, png.width, png.height) == ("png", 4032, 3024)
    assert png.orientation == "横向构图"
    assert png.size_text == "4032×3024 横向构图"
    jpeg = probe_image(_jpeg(1080, 1920))
    assert (jpeg.fmt, jpeg.width, jpeg.height) == ("jpeg", 1080, 1920)
    assert jpeg.orientation == "竖向构图"


def test_probe_unknown_format_is_tolerated() -> None:
    info = probe_image(b"not-an-image")
    assert info.fmt == "unknown"
    assert info.size_text == "分辨率未知"


def test_parse_capture_time_from_common_names() -> None:
    assert parse_capture_time("IMG_20250914_091930.jpg") == datetime(2025, 9, 14)
    assert parse_capture_time("mmexport1654645028396.jpg") is not None
    assert parse_capture_time("无时间文件名.jpg") is None


def test_heuristic_description_uses_only_visible_facts() -> None:
    description = heuristic_describe(
        _png(800, 600), file_name="IMG_20250914_091930.png", process="加工件", sub_process="机床件"
    )
    assert description.source == "heuristic"
    assert "加工件-机床件" in description.summary
    assert "800×600" in description.summary
    assert "2025-09-14" in description.details
    assert "加工件" in description.keywords
    assert description.warnings
    # 兜底描述不得编造材质 / 公差 / 产能等画面不可见参数
    assert find_unverified_claims(description.details) == ()


def test_find_unverified_claims_flags_specs() -> None:
    text = "可见 HT300 材质，单重 2.5 吨，公差 ±0.05mm，月产能 300 件。"
    hits = find_unverified_claims(text)
    assert any("HT300" in item for item in hits)
    assert any("吨" in item for item in hits)
    assert any("公差" in item for item in hits)
    assert any("月产" in item for item in hits)


def test_vision_describer_parses_json_and_warns_on_specs() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "多件灰色机床床身铸件整齐堆放",
                                    "details": "画面为户外场地堆放的大型铸件，表面喷灰色底漆，材质 HT300。",
                                    "keywords": ["机床床身", "灰色底漆"],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    describer = VisionDescriber(
        VisionConfig(api_key="k", model="vision-test", api_style="chat"), client=client
    )
    description = describer.describe(
        _png(800, 600), file_name="IMG_1.png", process="铸件", sub_process="机床件"
    )
    assert description.source == "vision"
    assert description.keywords == ("机床床身", "灰色底漆")
    assert any("HT300" in item for item in description.warnings)
    payload = captured["payload"]
    assert payload["model"] == "vision-test"
    assert payload["messages"][0]["content"] == SYSTEM_PROMPT
    image_url = payload["messages"][1]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")


def test_vision_describer_rejects_empty_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps({"summary": "", "details": ""})}}]}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    describer = VisionDescriber(VisionConfig(api_key="k"), client=client)
    with pytest.raises(ValueError):
        describer.describe(b"x", file_name="a.png", process="铸件", sub_process="阀体")


def test_responses_style_payload_and_parsing() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        text = "```json\n" + json.dumps(
            {"summary": "阀体铸件整齐堆放", "details": "可见灰色阀体铸件。", "keywords": ["阀体"]},
            ensure_ascii=False,
        ) + "\n```"
        return httpx.Response(
            200,
            json={"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    describer = VisionDescriber(
        VisionConfig(api_key="k", model="deepseek-v4-flash-vision-exp"), client=client
    )
    description = describer.describe(
        probe_png(), file_name="a.png", process="铸件", sub_process="阀体"
    )
    assert captured["url"] == "https://opencode.ai/zen/go/v1/responses"
    payload = captured["payload"]
    assert payload["model"] == "deepseek-v4-flash-vision-exp"
    assert payload["instructions"] == SYSTEM_PROMPT
    assert payload["input"][0]["content"][1]["type"] == "input_image"
    assert description.summary == "阀体铸件整齐堆放"
    assert description.keywords == ("阀体",)


def test_text_only_model_is_rejected() -> None:
    assert vision_model_supports_images("deepseek-v4-flash-vision-exp") is True
    assert vision_model_supports_images("deepseek-v4-flash") is False
    describer = VisionDescriber(VisionConfig(api_key="k", model="deepseek-v4-flash"))
    with pytest.raises(TextOnlyModelError) as excinfo:
        describer.describe(b"x", file_name="a.png", process="铸件", sub_process="阀体")
    assert "不能识图" in str(excinfo.value)


def test_probe_png_is_valid() -> None:
    info = probe_image(probe_png())
    assert (info.fmt, info.width, info.height) == ("png", 8, 8)


def test_probe_vision_reports_config_and_model_problems() -> None:
    assert probe_vision(VisionConfig())["stage"] == "config"
    wrong_model = probe_vision(VisionConfig(api_key="k", model="deepseek-v4-flash"))
    assert wrong_model["stage"] == "model"
    assert "deepseek-v4-flash-vision-exp" in wrong_model["message"]


def test_probe_vision_ok_with_mock_client() -> None:
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
                                "text": json.dumps(
                                    {"summary": "纯色探针图", "details": "红色方块。", "keywords": []},
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = probe_vision(VisionConfig(api_key="k"), client=client)
    assert result["ok"] is True
    assert result["stage"] == "done"
    assert result["endpoint"].endswith("/responses")


def test_vision_config_reads_env_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PULSE_VISION_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "PULSE_VISION_API_KEY=from-file\nPULSE_VISION_MODEL=my-model\n", encoding="utf-8"
    )
    config = vision_config_from_env(tmp_path)
    assert config.enabled is True
    assert config.api_key == "from-file"
    assert config.model == "my-model"


def test_build_describer_falls_back_without_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PULSE_VISION_API_KEY", raising=False)
    describe = build_describer(tmp_path)
    description = describe(b"x", file_name="a.jpg", process="铸件", sub_process="阀体")
    assert description.source == "heuristic"


def test_request_carries_opencode_session_header() -> None:
    """OpenCode Go 缺 x-opencode-session 会直接 400，必须默认带上。"""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["session"] = request.headers.get("x-opencode-session", "")
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {"summary": "灰色阀体铸件", "details": "可见灰色铸件。", "keywords": ["阀体"]},
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    VisionDescriber(VisionConfig(api_key="secret-key"), client=client).describe(
        probe_png(), file_name="a.png", process="铸件", sub_process="阀体"
    )
    assert captured["session"] == "pulse-media-library"
    assert captured["auth"] == "Bearer secret-key"


def test_api_error_surfaces_provider_message_and_hint() -> None:
    """服务端错误必须把原文带出来——只报 "400 Bad Request" 无法排障。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "MissingSessionID",
                    "message": "Request is missing x-opencode-session",
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    describer = VisionDescriber(VisionConfig(api_key="k"), client=client)
    with pytest.raises(VisionAPIError) as excinfo:
        describer.describe(probe_png(), file_name="a.png", process="铸件", sub_process="阀体")
    message = str(excinfo.value)
    assert excinfo.value.status == 400
    assert excinfo.value.provider_type == "MissingSessionID"
    assert "x-opencode-session" in message
    assert "PULSE_VISION_SESSION" in message  # 带回中文处理建议


def test_api_error_handles_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"<html>bad gateway</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(VisionAPIError) as excinfo:
        VisionDescriber(VisionConfig(api_key="k"), client=client).describe(
            probe_png(), file_name="a.png", process="铸件", sub_process="阀体"
        )
    assert excinfo.value.status == 502
    assert "bad gateway" in str(excinfo.value)


def test_probe_vision_reports_session_and_model_on_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "type": "error",
                "error": {"type": "RegionError", "message": "only available hosted in China"},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = probe_vision(VisionConfig(api_key="k"), client=client)
    assert result["ok"] is False
    assert result["stage"] == "request"
    assert result["status"] == 403
    assert result["session"] == "pulse-media-library"
    assert "deepseek-v4.1-flash" in result["hint"]


def test_recommended_model_can_read_images() -> None:
    assert vision_model_supports_images("deepseek-v4.1-flash") is True
    assert vision_model_supports_images("deepseek-v4-flash-vision-exp") is True
    assert VisionConfig().model == "deepseek-v4.1-flash"
    # 中文提示要同时给出推荐与备选
    describer = VisionDescriber(VisionConfig(api_key="k", model="deepseek-v4-pro"))
    with pytest.raises(TextOnlyModelError) as excinfo:
        describer.describe(b"x", file_name="a.png", process="铸件", sub_process="阀体")
    assert "deepseek-v4.1-flash" in str(excinfo.value)
    assert "deepseek-v4-flash-vision-exp" in str(excinfo.value)


def test_vision_config_reads_session_and_token_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PULSE_VISION_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "PULSE_VISION_API_KEY=k\nPULSE_VISION_SESSION=my-session\n"
        "PULSE_VISION_MAX_TOKENS=800\n",
        encoding="utf-8",
    )
    config = vision_config_from_env(tmp_path)
    assert config.session == "my-session"
    assert config.max_output_tokens == 800
