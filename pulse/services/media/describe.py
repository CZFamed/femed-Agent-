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

VIDEO_SUFFIXES = (".mp4", ".mov", ".avi", ".mkv", ".webm")

SYSTEM_PROMPT = (
    "你是工业铸件外贸企业的素材编目员。请只根据图片中**肉眼可见**的内容写中文描述。"
    "严禁推测或编造材质牌号、公差、单重、月产能、检测结果、客户名称等画面不可见的信息；"
    "看不见就写看不见。描述风格与既有素材库一致：先一句话概括，再补充细节与工艺阶段特征。"
    "只输出 JSON，字段为 summary（一句话摘要，40 字以内）、details（细节说明，120-260 字）、"
    "keywords（3-8 个中文关键词数组）。"
)

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


def _read_env_file(path: Path) -> dict[str, str]:
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
    )


@dataclass(frozen=True)
class VisionConfig:
    """视觉模型配置（OpenAI 兼容接口）。"""

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    timeout_s: float = 45.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip())


def vision_config_from_env(root: Path | None = None) -> VisionConfig:
    """从环境变量（或仓库根 .env）读取视觉模型配置。"""
    file_values = _read_env_file((root or Path.cwd()) / ".env")

    def pick(name: str, default: str) -> str:
        return os.environ.get(name) or file_values.get(name) or default

    raw_timeout = pick("PULSE_VISION_TIMEOUT", "45")
    try:
        timeout = float(raw_timeout)
    except ValueError:
        timeout = 45.0
    return VisionConfig(
        base_url=pick("PULSE_VISION_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        api_key=pick("PULSE_VISION_API_KEY", ""),
        model=pick("PULSE_VISION_MODEL", "gpt-4o-mini"),
        timeout_s=timeout,
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
    """调用多模态模型生成描述。"""

    def __init__(self, config: VisionConfig, *, client: Any | None = None) -> None:
        self.config = config
        self._client = client

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx  # 项目依赖，按需导入避免无网络环境下的额外开销

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.config.base_url}/chat/completions"
        if self._client is not None:
            response = self._client.post(url, headers=headers, json=payload)
        else:
            with httpx.Client(timeout=self.config.timeout_s) as client:
                response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

    def describe(
        self, data: bytes, *, file_name: str, process: str, sub_process: str
    ) -> Description:
        mime = _mime_of(file_name)
        encoded = base64.b64encode(data).decode("ascii")
        payload = {
            "model": self.config.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"品类：{process}/{sub_process}；源文件：{file_name}。"
                                "请按系统提示输出 JSON。"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                        },
                    ],
                },
            ],
        }
        body = self._post(payload)
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content) if isinstance(content, str) else dict(content)
        summary = str(parsed.get("summary") or "").strip()
        details = str(parsed.get("details") or "").strip()
        raw_keywords = parsed.get("keywords") or []
        keywords = tuple(str(item).strip() for item in raw_keywords if str(item).strip())
        if not summary and not details:
            raise ValueError("视觉模型返回内容为空")
        suspicious = find_unverified_claims(f"{summary} {details}")
        warnings: list[str] = []
        if suspicious:
            warnings.append(
                "模型输出含画面不可见参数（" + "、".join(suspicious) + "），请人工核对后再入库。"
            )
        return Description(
            summary=summary or details[:40],
            details=details or summary,
            keywords=keywords,
            source="vision",
            warnings=tuple(warnings),
        )


def build_describer(
    root: Path | None = None, *, client: Any | None = None
) -> Callable[..., Description]:
    """返回可用描述器：优先视觉模型，未配置时退化为基础信息描述。"""
    config = vision_config_from_env(root)
    if config.enabled:
        return VisionDescriber(config, client=client).describe

    def _fallback(
        data: bytes, *, file_name: str, process: str, sub_process: str
    ) -> Description:
        return heuristic_describe(
            data, file_name=file_name, process=process, sub_process=sub_process
        )

    return _fallback
