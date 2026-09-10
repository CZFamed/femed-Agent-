"""HTTP 传输抽象。

W1 阶段**不得发起真实网络请求**（派工单 §5 / 协同手册 §3.8），
所以 Adapter 不直接建连，而是接受一个注入的 `Transport`：
测试注入 `RecordingTransport`（假传输），生产由部署层注入真实实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """一次平台 API 调用。`json` 与 `content` 二选一。"""

    method: str
    url: str
    json: Mapping[str, Any] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    content: bytes | None = None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """平台响应。`headers` 按大小写不敏感取值。"""

    status_code: int
    body: Mapping[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        """大小写不敏感地取响应头（如 `Location`、`X-Restli-Id`）。"""

        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


class Transport(Protocol):
    """异步传输层协议（可调用对象）。"""

    def __call__(self, request: HttpRequest) -> Awaitable[HttpResponse]:  # pragma: no cover
        """执行一次请求。"""


async def no_transport(request: HttpRequest) -> HttpResponse:
    """默认传输：直接报错。

    这是有意的失败——防止忘记注入假传输时真的发出请求。
    """

    raise RuntimeError(
        f"未注入 Transport，拒绝发起真实请求：{request.method} {request.url}"
        "（W1 阶段平台调用必须走假传输）"
    )


class RecordingTransport:
    """测试用假传输：按脚本返回响应，并记录收到的请求。"""

    def __init__(self, responses: list[HttpResponse] | None = None) -> None:
        self._responses = list(responses or [])
        self.requests: list[HttpRequest] = []
        self.default = HttpResponse(status_code=200, body={})

    def push(self, response: HttpResponse) -> "RecordingTransport":
        """追加一个待消费的响应，返回 self 便于链式调用。"""

        self._responses.append(response)
        return self

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return self.default


#: 便于注入的传输层类型别名
TransportCallable = Callable[[HttpRequest], Awaitable[HttpResponse]]
