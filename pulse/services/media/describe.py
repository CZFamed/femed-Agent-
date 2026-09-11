"""素材描述自动生成（所有者：root）。

控制台不再要求用户手填描述：选定图片后自动生成"摘要 / 细节 / 关键词"。

两条路径：

1. **视觉模型**（首选）：配置 ``PULSE_VISION_API_KEY`` 后，调用 OpenAI 兼容的
   多模态接口识别画面内容，输出与既有 RAG 描述一致的 JSON。
2. **基础信息**（兜底）：未配置模型时，仅用文件名、拍摄时间、图片尺寸、
   品类等**可见事实**拼装描述，并明确提示"画面内容待补充"。

铁律（AGENTS.md §3.7 不伪造数据）：禁止推测材质牌号、公差、单重、月产能等
画面不可见参数；模型若输出了这类数值，会被 ``find_unverified_claims`` 标出并
作为警告返回，交由人工确认。
"""

from __future__ import annotations

import base64
import json
import os
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pulse.services.media.categories import (
    CategoryCatalog,
    resolve_category,
)

VIDEO_SUFFIXES = (".mp4", ".mov", ".avi", ".mkv", ".webm")

SYSTEM_PROMPT = (
    "你是工业铸件外贸企业的素材编目员。请只根据图片中**肉眼可见**的内容写中文描述。"
    "严禁推测或编造材质牌号、公差、单重、月产能、检测结果、客户名称等画面不可见的信息；"
    "看不见就写看不见。描述风格与既有素材库一致：先一句话概括，再补充细节与工艺阶段特征。"
    "只输出 JSON，字段为 summary（一句话摘要，40 字以内）、details（细节说明，120-260 字）、"
    "keywords（3-8 个中文关键词数组）。"
)

#: 让模型顺带判定品类；具体清单在运行时拼进去
CATEGORY_PROMPT_SUFFIX = (
    "\nJSON 里还要有 process 与 sub_process 两个字段，记录该素材的品类。"
    "**只能从下面这份既有品类清单里选，不要自创**：{catalog}。"
    "判断依据是画面里实际拍到的东西（工件形态、所处工序、拍摄场景），不是文件名。"
    "确实判断不了时，两个字段都填空字符串。"
)


def build_system_prompt(catalog: CategoryCatalog | None = None) -> str:
    """拼出系统提示词；没有品类目录时就是基础提示词。"""
    if not catalog:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + CATEGORY_PROMPT_SUFFIX.format(catalog=catalog.prompt_text())

#: 画面不可见、但常被模型"猜"出来的参数类表述
_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(HT|QT|GG|ZG)\s?\d{3,4}"),
    re.compile(r"\d+(\.\d+)?\s?(吨|t|kg|公斤|mm|毫米|MPa|bar|m³|立方米)"),
    re.compile(r"(公差|精度|产能|月产|单重|牌号|探伤|合格证|ISO\s?\d{4,5})"),
)


@dataclass(frozen=True)
class ImageInfo:
    """图片基础信息（只解析文件头，不依赖第三方图像库）。"""

    fmt: str
    width: int | None = None
    height: int | None = None

    @property
    def orientation(self) -> str:
        if not self.width or not self.height:
            return "未知构图"
        if self.width > self.height:
            return "横向构图"
        if self.width < self.height:
            return "竖向构图"
        return "方图"

    @property
    def size_text(self) -> str:
        if not self.width or not self.height:
            return "分辨率未知"
        return f"{self.width}×{self.height} {self.orientation}"


@dataclass(frozen=True)
class Description:
    """自动生成的素材描述。"""

    summary: str
    details: str
    keywords: tuple[str, ...]
    source: str  # vision | heuristic
    warnings: tuple[str, ...] = field(default=())
    process: str = ""
    sub_process: str = ""
    category_source: str = ""  # vision | keyword | manual | heuristic | none


def find_unverified_claims(text: str) -> tuple[str, ...]:
    """找出疑似"画面不可见参数"的表述，供人工确认。"""
    hits: list[str] = []
    for pattern in _CLAIM_PATTERNS:
        for match in pattern.finditer(text or ""):
            token = match.group(0).strip()
            if token and token not in hits:
                hits.append(token)
    return tuple(hits)


def probe_image(data: bytes) -> ImageInfo:
    """解析 PNG / JPEG / GIF / BMP / WebP 的尺寸。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return ImageInfo("png", width, height)
    if data.startswith(b"GIF8") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return ImageInfo("gif", width, height)
    if data.startswith(b"BM") and len(data) >= 26:
        width, height = struct.unpack("<ii", data[18:26])
        return ImageInfo("bmp", abs(width), abs(height))
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and data[12:16] == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return ImageInfo("webp", width, height)
    if data.startswith(b"\xff\xd8"):
        return _probe_jpeg(data)
    return ImageInfo("unknown")


def _probe_jpeg(data: bytes) -> ImageInfo:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment_length = int.from_bytes(data[index + 2 : index + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            return ImageInfo("jpeg", width, height)
        index += 2 + max(segment_length, 2)
    return ImageInfo("jpeg")


def parse_capture_time(file_name: str) -> datetime | None:
    """从常见命名中提取拍摄时间：IMG_20250914_091930 / mmexport1654645028396。"""
    match = re.search(r"(20\d{2})(\d{2})(\d{2})[_\-]?(\d{2})(\d{2})(\d{2})?", file_name)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        try:
            return datetime(year, month, day)
        except ValueError:
            return None
    epoch = re.search(r"(1[0-9]{12})", file_name)
    if epoch:
        try:
            return datetime.fromtimestamp(int(epoch.group(1)) / 1000)
        except (OSError, ValueError, OverflowError):
            return None
    return None


def read_env_file(path: Path) -> dict[str, str]:
    """读取 ``.env`` 形式的键值对（``KEY=VALUE``，支持 ``#`` 注释）。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def heuristic_describe(
    data: bytes, *, file_name: str, process: str, sub_process: str
) -> Description:
    """兜底描述：只用可见事实，不编造画面内容。"""
    info = probe_image(data)
    captured = parse_capture_time(file_name)
    category = f"{process}-{sub_process}" if sub_process else process
    keywords: list[str] = [item for item in (process, sub_process) if item]
    if captured:
        keywords.append(f"{captured.year}年")
    summary = f"{category}类实拍素材（{info.size_text}）"
    detail_parts = [
        f"本图由素材库控制台自动登记：品类 {process}/{sub_process}，源文件 {file_name}，"
        f"图片格式 {info.fmt.upper()}，{info.size_text}。",
    ]
    if captured:
        detail_parts.append(f"按文件名推断拍摄日期为 {captured.date().isoformat()}。")
    detail_parts.append("画面内容描述尚未生成：当前未配置视觉识别模型，请人工补充或配置后重新生成。")
    warnings = ["未配置视觉识别模型：描述仅含文件与规格信息，画面内容待补充。"]
    return Description(
        summary=summary,
        details="".join(detail_parts),
        keywords=tuple(dict.fromkeys(keywords)),
        source="heuristic",
        warnings=tuple(warnings),
        process=process,
        sub_process=sub_process,
        category_source="heuristic",
    )


@dataclass(frozen=True)
class VisionConfig:
    """视觉模型配置。

    默认对接 OpenCode Go（Responses API）；任何 OpenAI 兼容端点都可通过
    ``PULSE_VISION_BASE_URL`` + ``PULSE_VISION_API_STYLE`` 覆盖。
    """

    base_url: str = "https://opencode.ai/zen/go/v1"
    api_key: str = ""
    model: str = "deepseek-v4.1-flash"
    api_style: str = "responses"  # responses | chat
    timeout_s: float = 45.0
    session: str = "pulse-media-library"
    max_output_tokens: int = 2000

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip())

    @property
    def endpoint(self) -> str:
        suffix = "/responses" if self.api_style == "responses" else "/chat/completions"
        return f"{self.base_url.rstrip('/')}{suffix}"


#: 推荐模型（2026-09-11 实测可识图）
RECOMMENDED_VISION_MODEL = "deepseek-v4.1-flash"

#: 备选模型（同为 OpenCode Go 上可识图的模型）
FALLBACK_VISION_MODEL = "deepseek-v4-flash-vision-exp"

#: 拿它们识图必然失败的模型：或只有文本输入能力，或只在中国区托管且需显式开通
TEXT_ONLY_MODELS: frozenset[str] = frozenset(
    {"deepseek-v4-flash", "deepseek-v4-pro", "deepseek-flash"}
)


class TextOnlyModelError(ValueError):
    """当前配置的模型不支持图片输入（或未开通）。"""


class VisionAPIError(RuntimeError):
    """视觉接口返回错误，已解析服务端原文并附上中文处理建议。"""

    def __init__(
        self,
        status: int,
        provider_type: str = "",
        provider_message: str = "",
        hint: str = "",
    ) -> None:
        self.status = status
        self.provider_type = provider_type
        self.provider_message = provider_message
        self.hint = hint
        parts = [f"视觉接口返回 HTTP {status}"]
        if provider_type:
            parts.append(f"错误类型 {provider_type}")
        if provider_message:
            parts.append(f"服务端原文：{provider_message}")
        if hint:
            parts.append(f"处理建议：{hint}")
        super().__init__("；".join(parts))


#: OpenCode Go 的已知错误类型 → 中文处理建议
_PROVIDER_HINTS: dict[str, str] = {
    "MissingSessionID": (
        "本项目已自动附带 x-opencode-session 请求头；若仍报此错，"
        "请在 .env 里手动设置 PULSE_VISION_SESSION 为任意非空字符串。"
    ),
    "RegionError": (
        "该模型只在中国区托管且需要显式开通，请改用 "
        f"{RECOMMENDED_VISION_MODEL}。"
    ),
}

#: HTTP 状态码 → 中文处理建议（未被错误类型覆盖时使用）
_STATUS_HINTS: dict[int, str] = {
    401: "API Key 无效或已过期，请重新复制 PULSE_VISION_API_KEY。",
    403: "密钥无该模型的调用权限，或模型需要先在 OpenCode 后台开通。",
    404: "接口地址或模型名不对，请核对 PULSE_VISION_BASE_URL 与 PULSE_VISION_MODEL。",
    429: "调用过于频繁或额度用尽，请稍后再试。",
}


def _provider_error_of(response: Any) -> tuple[str, str]:
    """从错误响应里取出 ``(错误类型, 服务端原文)``。"""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - 非 JSON 响应退回纯文本
        return "", (getattr(response, "text", "") or "").strip()[:300]
    if not isinstance(body, dict):
        return "", str(body)[:300]
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("type") or ""), str(error.get("message") or "")
    if isinstance(error, str):
        return "", error
    return "", (body.get("message") or json.dumps(body, ensure_ascii=False))[:300]


def _hint_for(status: int, provider_type: str) -> str:
    return _PROVIDER_HINTS.get(provider_type) or _STATUS_HINTS.get(status, "")


def vision_model_supports_images(model: str) -> bool:
    return model.strip() not in TEXT_ONLY_MODELS


def vision_config_from_env(root: Path | None = None) -> VisionConfig:
    """从环境变量（或仓库根 .env）读取视觉模型配置。"""
    file_values = read_env_file((root or Path.cwd()) / ".env")

    def pick(name: str, default: str) -> str:
        return os.environ.get(name) or file_values.get(name) or default

    raw_timeout = pick("PULSE_VISION_TIMEOUT", "45")
    try:
        timeout = float(raw_timeout)
    except ValueError:
        timeout = 45.0
    raw_tokens = pick("PULSE_VISION_MAX_TOKENS", "2000")
    try:
        max_tokens = int(raw_tokens)
    except ValueError:
        max_tokens = 2000
    return VisionConfig(
        base_url=pick("PULSE_VISION_BASE_URL", "https://opencode.ai/zen/go/v1").rstrip("/"),
        api_key=pick("PULSE_VISION_API_KEY", ""),
        model=pick("PULSE_VISION_MODEL", RECOMMENDED_VISION_MODEL),
        api_style=pick("PULSE_VISION_API_STYLE", "responses"),
        timeout_s=timeout,
        session=pick("PULSE_VISION_SESSION", "pulse-media-library"),
        max_output_tokens=max_tokens,
    )


def _mime_of(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".gif": "image/gif",
    }.get(suffix, "image/jpeg")


class VisionDescriber:
    """调用多模态模型生成描述（支持 Responses API 与 Chat Completions）。"""

    def __init__(
        self,
        config: VisionConfig,
        *,
        client: Any | None = None,
        catalog: CategoryCatalog | None = None,
    ) -> None:
        self.config = config
        self._client = client
        #: 既有品类清单：让模型从库里真实存在的品类中选，而不是自己编
        self.catalog = catalog or CategoryCatalog()

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx  # 项目依赖，按需导入避免无网络环境下的额外开销

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            # OpenCode Go 强制要求：缺这个头会直接返回 400 MissingSessionID
            "x-opencode-session": self.config.session or "pulse-media-library",
        }
        url = self.config.endpoint
        if self._client is not None:
            response = self._client.post(url, headers=headers, json=payload)
        else:
            with httpx.Client(timeout=self.config.timeout_s) as client:
                response = client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            provider_type, provider_message = _provider_error_of(response)
            raise VisionAPIError(
                response.status_code,
                provider_type,
                provider_message,
                _hint_for(response.status_code, provider_type),
            )
        return response.json()

    def _build_payload(self, data_url: str, *, file_name: str, process: str, sub_process: str):
        prompt = (
            f"品类：{process}/{sub_process}；源文件：{file_name}。请按系统提示输出 JSON。"
        )
        limit = self.config.max_output_tokens
        instructions = build_system_prompt(self.catalog)
        if self.config.api_style == "responses":
            payload: dict[str, Any] = {
                "model": self.config.model,
                "instructions": instructions,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": data_url},
                        ],
                    }
                ],
            }
            if limit > 0:
                payload["max_output_tokens"] = limit
            return payload
        payload = {
            "model": self.config.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": instructions},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
        }
        if limit > 0:
            payload["max_tokens"] = limit
        return payload

    @staticmethod
    def _extract_text(body: dict[str, Any], api_style: str) -> str:
        """从两种 API 形态的响应里取出模型文本。"""
        if api_style == "responses":
            chunks: list[str] = []
            for item in body.get("output") or []:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                        chunks.append(str(part.get("text") or ""))
            if not chunks and isinstance(body.get("output_text"), str):
                chunks.append(body["output_text"])
            return "".join(chunks)
        content = body["choices"][0]["message"]["content"]
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)

    def describe(
        self, data: bytes, *, file_name: str, process: str, sub_process: str
    ) -> Description:
        if not vision_model_supports_images(self.config.model):
            raise TextOnlyModelError(
                f"模型 {self.config.model} 不能识图（只有文本能力，或需在中国区单独开通）。"
                f"请改用 {RECOMMENDED_VISION_MODEL}（备选 {FALLBACK_VISION_MODEL}）。"
            )
        mime = _mime_of(file_name)
        encoded = base64.b64encode(data).decode("ascii")
        payload = self._build_payload(
            f"data:{mime};base64,{encoded}",
            file_name=file_name,
            process=process,
            sub_process=sub_process,
        )
        body = self._post(payload)
        parsed = _loads_json_object(self._extract_text(body, self.config.api_style))
        summary = str(parsed.get("summary") or "").strip()
        details = str(parsed.get("details") or "").strip()
        raw_keywords = parsed.get("keywords") or []
        keywords = tuple(str(item).strip() for item in raw_keywords if str(item).strip())
        if not summary and not details:
            raise ValueError("视觉模型返回内容为空")
        # 品类：优先采信模型的判断，但必须是既有品类；否则用关键词兜底
        resolved_process, resolved_sub, category_source = resolve_category(
            str(parsed.get("process") or ""),
            str(parsed.get("sub_process") or ""),
            text=f"{summary} {details}",
            keywords=keywords,
            catalog=self.catalog,
        )
        if category_source == "none" and self.catalog.has(process, sub_process):
            # 模型没判断出来：保留调用方（页面下拉框/人工）已经选好的品类
            resolved_process, resolved_sub, category_source = process, sub_process, "manual"

        suspicious = find_unverified_claims(f"{summary} {details}")
        warnings: list[str] = []
        if suspicious:
            warnings.append(
                "描述里出现数字/规格类表述（" + "、".join(suspicious) + "）："
                "若来自图中铭牌或标牌则可用，否则请删掉后再入库。"
            )
        if self.catalog and category_source == "none":
            warnings.append("没能判断出品类，请在上方手动选择后再入库。")
        return Description(
            summary=summary or details[:40],
            details=details or summary,
            keywords=keywords,
            source="vision",
            warnings=tuple(warnings),
            process=resolved_process,
            sub_process=resolved_sub,
            category_source=category_source,
        )


def build_describer(
    root: Path | None = None,
    *,
    client: Any | None = None,
    catalog: CategoryCatalog | None = None,
) -> Callable[..., Description]:
    """返回可用描述器：优先视觉模型，未配置时退化为基础信息描述。"""
    config = vision_config_from_env(root)
    if config.enabled:
        return VisionDescriber(config, client=client, catalog=catalog).describe

    def _fallback(
        data: bytes, *, file_name: str, process: str, sub_process: str
    ) -> Description:
        return heuristic_describe(
            data, file_name=file_name, process=process, sub_process=sub_process
        )

    return _fallback


def _loads_json_object(text: str) -> dict[str, Any]:
    """容错解析模型输出的 JSON（允许 ```json 代码块与前后散文）。"""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {"summary": str(parsed)}
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(cleaned[start : end + 1])
        return parsed if isinstance(parsed, dict) else {}
    raise ValueError("视觉模型返回内容不是 JSON")


def probe_png(width: int = 8, height: int = 8) -> bytes:
    """生成一张纯色探针图（标准库实现，用于视觉模型自检）。"""
    import zlib

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def probe_vision(config: VisionConfig, *, client: Any | None = None) -> dict[str, Any]:
    """自检：验证地址、密钥与模型是否真的能识图。

    返回 ``{"ok": bool, "stage": str, "message": str, ...}``，供命令行与页面提示使用。
    """
    if not config.enabled:
        return {
            "ok": False,
            "stage": "config",
            "message": "未配置 PULSE_VISION_API_KEY，无法调用视觉模型。",
        }
    if not vision_model_supports_images(config.model):
        return {
            "ok": False,
            "stage": "model",
            "message": (
                f"模型 {config.model} 不能识图（只有文本能力，或需在中国区单独开通）；"
                f"请改用 {RECOMMENDED_VISION_MODEL}（备选 {FALLBACK_VISION_MODEL}）。"
            ),
            "model": config.model,
            "endpoint": config.endpoint,
        }
    try:
        description = VisionDescriber(config, client=client).describe(
            probe_png(), file_name="probe.png", process="自检", sub_process="探针"
        )
    except Exception as exc:  # noqa: BLE001 - 自检需要把任何失败原因回报给用户
        result: dict[str, Any] = {
            "ok": False,
            "stage": "request",
            "message": str(exc),
            "endpoint": config.endpoint,
            "model": config.model,
            "session": config.session,
        }
        if isinstance(exc, VisionAPIError):
            result["status"] = exc.status
            result["provider_type"] = exc.provider_type
            result["hint"] = exc.hint
        return result
    return {
        "ok": True,
        "stage": "done",
        "message": "视觉模型可用。",
        "endpoint": config.endpoint,
        "model": config.model,
        "session": config.session,
        "summary": description.summary,
        "warnings": list(description.warnings),
    }
