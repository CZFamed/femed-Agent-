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


class PulseRequestHandler(BaseHTTPRequestHandler):
    """把 HTTP 请求转成 `ApiApp.handle()` 调用。"""

    #: 由 server 实例注入
    app: ApiApp
    default_actor: str = "http"
    server_version = "PulseAPI/1.0"

    # ---- 日志：只记方法/路径/状态，不记头部与 body（可能含凭据） ----
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 标准库签名
        message = format % args
        # BaseHTTPRequestHandler 默认会把首行打进 stderr；这里保留但去掉客户端地址里的端口噪音
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

    # ---- 内部 ----
    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
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
            self._write(ApiResponse(400, {"error": {"code": "bad_request", "message": str(exc), "details": {}}}))
            return

        actor = self.headers.get("X-Actor") or self.default_actor
        response = self.app.handle(
            method, parsed.path, body=body, query=query, actor=str(actor)
        )
        self._write(response)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
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
            data = self.rfile.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)

    def _write(self, response: ApiResponse, *, close: bool = False) -> None:
        payload = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        # 本地服务，允许控制台页面直连；生产部署应在网关层收敛 CORS
        self.send_header("Access-Control-Allow-Origin", "*")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)

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
