"""接口层的 HTTP 适配（所有者：A5）。

把框架无关的 `ApiApp` 挂到标准库 `http.server` 上——**不引入新依赖**，
`python -m pulse.api.http_server` 就能把这 11 个端点跑起来。

三个刻意的设计：

1. **只做协议翻译**：解析 HTTP → 调 `ApiApp.handle()` → 把 `ApiResponse` 原样写回。
   业务逻辑一行都不在这里，所以换到 Cloudflare Worker 只需再写一层同样的薄适配。
2. **不泄漏敏感信息**：请求日志只记方法、路径、状态码；`Authorization` 等头部不落日志，
   错误响应由 `ApiApp` 的统一错误体产生（未知异常不吐类型与栈）。
3. **可测**：`serve_in_background()` 用 0 号端口起临时服务，测试直接拿 `base_url` 打真实 HTTP。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from pulse.api.app import ApiApp
from pulse.api.errors import ApiResponse

#: 一次请求体最大字节数（防止误发的巨大 body 把内存吃满）
MAX_BODY_BYTES = 2 * 1024 * 1024

#: 超限时最多再排空多少字节（超过就直接断连，避免被无限流拖住）
MAX_DRAIN_BYTES = 32 * 1024 * 1024


class ApiRouteMixin:
    """把 `/api/v1/*` 交给 `ApiApp.handle()`（供多个 HTTP 适配复用）。

    混合进任何 `BaseHTTPRequestHandler` 子类即可：在 `do_GET` / `do_POST` 的开头调用
    `if self.try_handle_pipeline_api("GET"): return`。素材库控制台就是用它把
    契约 §7 的 11 个端点直接暴露在同一进程里，页面因此可以用真实 HTTP 调后端。

    注入方式二选一：类属性 `pipeline_app`（推荐）或 `app`（纯 API 服务用这个名字）。
    """

    #: ApiApp 实例；由 create_server / PulseHttpServer 注入
    pipeline_app: Any = None
    default_actor: str = "http"
    API_PREFIX: str = "/api/v1/"

    def _pipeline_app(self) -> Any:
        return getattr(self, "pipeline_app", None) or getattr(self, "app", None)

    def try_handle_pipeline_api(self, method: str) -> bool:
        """路径属于 `/api/v1/*` 时处理并返回 True；否则返回 False（交给宿主路由）。"""
        app = self._pipeline_app()
        if app is None:
            return False
        parsed = urlparse(self.path)  # type: ignore[attr-defined]
        if not parsed.path.startswith(self.API_PREFIX):
            return False
        self._handle_api_request(app, method, parsed)
        return True

    # ---- 内部：一次 API 请求 ----

    def _handle_api_request(self, app: Any, method: str, parsed: Any) -> None:
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length") or 0)  # type: ignore[attr-defined]
        if length > MAX_BODY_BYTES:
            # 关键顺序：**先把客户端发来的 body 排空，再回响应**。
            # 否则客户端还在发数据、服务端已经写了响应并关连接，客户端会看到
            # "连接被中止"（WinError 10053）而不是一个干净的 413。
            self._drain(length)
            self._write(
                ApiResponse(
                    413,
                    {
                        "error": {
                            "code": "payload_too_large",
                            "message": f"请求体过大（{length} 字节，上限 {MAX_BODY_BYTES}）",
                            "details": {"limit": MAX_BODY_BYTES},
                        }
                    },
                ),
                close=True,
            )
            return
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._write(
                ApiResponse(
                    400,
                    {"error": {"code": "bad_request", "message": str(exc), "details": {}}},
                )
            )
            return

        actor = self.headers.get("X-Actor") or self.default_actor  # type: ignore[attr-defined]
        response = app.handle(method, parsed.path, body=body, query=query, actor=str(actor))
        self._write(response)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)  # type: ignore[attr-defined]
        if length <= 0:
            return {}
        raw = self.rfile.read(length)  # type: ignore[attr-defined]
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return parsed

    def _drain(self, length: int) -> None:
        """把请求体读掉并丢弃（封顶 `MAX_DRAIN_BYTES`），让客户端能正常收到响应。"""
        remaining = min(length, MAX_DRAIN_BYTES)
        chunk = 64 * 1024
        while remaining > 0:
            data = self.rfile.read(min(chunk, remaining))  # type: ignore[attr-defined]
            if not data:
                break
            remaining -= len(data)

    def _write(self, response: ApiResponse, *, close: bool = False) -> None:
        payload = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
        self.send_response(response.status)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "application/json; charset=utf-8")  # type: ignore[attr-defined]
        self.send_header("Content-Length", str(len(payload)))  # type: ignore[attr-defined]
        # 本地服务，允许控制台页面直连；生产部署应在网关层收敛 CORS
        self.send_header("Access-Control-Allow-Origin", "*")  # type: ignore[attr-defined]
        if close:
            self.send_header("Connection", "close")  # type: ignore[attr-defined]
            self.close_connection = True  # type: ignore[attr-defined]
        self.end_headers()  # type: ignore[attr-defined]
        self.wfile.write(payload)  # type: ignore[attr-defined]


class PulseRequestHandler(ApiRouteMixin, BaseHTTPRequestHandler):
    """纯 API 服务的处理器：所有路径都走 `/api/v1/*`。"""

    app: ApiApp
    default_actor: str = "http"
    server_version = "PulseAPI/1.0"

    # ---- 日志：只记方法/路径/状态，不记头部与 body（可能含凭据） ----
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 标准库签名
        message = format % args
        self.server.log(f"{self.command} {self.path} -> {message}")  # type: ignore[attr-defined]

    # ---- 各 HTTP 方法 ----
    def do_GET(self) -> None:  # noqa: N802 - 标准库命名
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        if self.try_handle_pipeline_api(method):
            return
        self._write(
            ApiResponse(
                404,
                {
                    "error": {
                        "code": "not_found",
                        "message": f"没有这个端点：{method} {urlparse(self.path).path}（API 前缀 {self.API_PREFIX}）",
                        "details": {},
                    }
                },
            )
        )

    def do_OPTIONS(self) -> None:  # noqa: N802 - 预检请求
        self.send_response(204)
        self.send_header("Allow", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Actor")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.end_headers()


class PulseHttpServer:
    """可嵌入的 HTTP 服务（测试与本地联调都用它）。"""

    def __init__(
        self,
        app: ApiApp,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        actor: str = "http",
    ) -> None:
        handler = type(
            "BoundPulseHandler",
            (PulseRequestHandler,),
            {"app": app, "default_actor": actor},
        )
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.log = lambda message: _log(message)  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    # ---- 生命周期 ----
    @property
    def host(self) -> str:
        return str(self._server.server_address[0])

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def serve_in_background(self) -> "PulseHttpServer":
        """在后台线程里服务（测试用；调用方负责 `stop()`）。"""
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def serve_forever(self) -> None:
        _log(f"Pulse API 已启动：{self.base_url}（Ctrl+C 停止）")
        self._server.serve_forever()

    def __enter__(self) -> "PulseHttpServer":
        return self.serve_in_background()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _log(message: str) -> None:
    print(message, flush=True)


def main(argv: list[str] | None = None) -> int:
    """`python -m pulse.api.http_server [--host 127.0.0.1] [--port 8787]`。

    默认起**演示接线**（内存存储 + 假队列 + 演示账号）——真实接线需要平台凭据与账号配置，
    由部署层调用 `build_service_app()` 注入，不在命令行里假装已经配好。
    """
    import argparse

    from pulse.api.wiring import build_demo_app

    parser = argparse.ArgumentParser(description="Pulse 接口层 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8787, help="监听端口（默认 8787）")
    parser.add_argument("--actor", default="http", help="未带 X-Actor 头部时的默认操作人")
    parser.add_argument(
        "--no-demo",
        action="store_true",
        help="不起演示接线（仅用于嵌入部署时检查参数，会直接退出）",
    )
    args = parser.parse_args(argv)

    if args.no_demo:
        _log(
            "未启用演示接线：生产接线请在部署脚本里调用 pulse.api.wiring.build_service_app() "
            "并注入各域服务与真实账号凭据"
        )
        return 0

    app = build_demo_app()
    server = PulseHttpServer(app, host=args.host, port=args.port, actor=args.actor)
    _log(f"演示接线：LinkedIn 演示账号已就绪（不会真的发到平台）；{len(app.ROUTES)} 个端点")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("已停止")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
