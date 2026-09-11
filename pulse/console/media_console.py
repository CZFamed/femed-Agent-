"""素材库图形化控制台（所有者：root）。

纯标准库实现（http.server + 内嵌 HTML），不引入任何新依赖：

- ``GET  /``             素材库页面（上传、容量预警、冷却状态）
- ``GET  /api/state``    素材清单 + 容量报告
- ``POST /api/assets``   上传实拍图入库（multipart/form-data）
- ``POST /api/usage``    标记素材已进入内容 / 发布（触发 15 天冷却）
- ``POST /api/recall``   按策略召回候选（新图优先 + 冷却过滤 + 加权随机）
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pulse.services.media.catalog import MediaAsset, load_catalog
from pulse.services.media.config import BRAND_NAME, RecallConfig
from pulse.services.media.ingest import (
    DuplicateMediaError,
    MediaIngestor,
    UnsupportedMediaError,
)
from pulse.services.media.ledger import RecallLedger
from pulse.services.media.policy import MediaCandidate, RecallPolicy
from pulse.services.media.registry import DEFAULT_REGISTRY_NAME, MediaRegistry

PAGE_HTML = (
    "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'/>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
    "<title>Pulse 素材库控制台</title><style>"
    "body{font-family:'Microsoft YaHei',system-ui,sans-serif;margin:0;background:#f5f6f8}"
    "header{background:#1f4e79;color:#fff;padding:18px 28px}"
    "header h1{margin:0;font-size:20px}header p{margin:6px 0 0;font-size:13px;opacity:.85}"
    "main{padding:20px 28px 60px;max-width:1180px;margin:0 auto}"
    ".banner{padding:12px 16px;border-radius:6px;font-weight:600;margin-bottom:18px}"
    ".banner.red{background:#fdecea;color:#b3261e;border:1px solid #f3b8b2}"
    ".banner.ok{background:#e8f5e9;color:#1b5e20;border:1px solid #b7dfb9}"
    ".cards{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:20px}"
    ".card{background:#fff;border:1px solid #dcdfe4;border-radius:8px;padding:14px 18px;min-width:120px}"
    ".card b{display:block;font-size:24px;margin-top:4px}"
    "section{background:#fff;border:1px solid #dcdfe4;border-radius:8px;padding:18px;margin-bottom:20px}"
    "h2{font-size:16px;margin:0 0 12px}"
    "form{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}"
    "label{font-size:13px;color:#444;display:block;margin-bottom:4px}"
    "input,textarea{width:100%;padding:7px 9px;border:1px solid #c9ced6;border-radius:5px;font:inherit}"
    "button{background:#1f4e79;color:#fff;border:0;border-radius:5px;padding:9px 16px;cursor:pointer}"
    ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}"
    ".asset{border:1px solid #e1e4e8;border-radius:6px;padding:12px;background:#fcfcfd}"
    ".asset h3{margin:0 0 6px;font-size:14px}"
    ".tag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:10px;"
    "background:#eef2f7;color:#33475b}"
    ".tag.new{background:#e3f2fd;color:#0d47a1}"
    ".tag.cooling{background:#fff4e5;color:#9a5b00}"
    ".muted{color:#6b7280;font-size:12px}"
    "</style></head><body>"
    "<header><h1>Pulse 素材库控制台</h1>"
    "<p>品牌级素材池：沧州菲美得 ｜ 冷却 15 天 ｜ 新图优先召回 ｜ 容量低于 112 张红色预警</p>"
    "</header><main>"
    "<div id='banner' class='banner ok'>加载中…</div>"
    "<div class='cards' id='cards'></div>"
    "<section><h2>上传实拍图入库</h2><form id='upload'>"
    "<div><label>图片文件</label><input type='file' name='file' accept='image/*' required/></div>"
    "<div><label>品类 process</label><input name='process' value='加工件' required/></div>"
    "<div><label>子类 sub_process</label><input name='sub_process' value='机床件' required/></div>"
    "<div><label>关键词（逗号分隔）</label><input name='keywords' placeholder='机床床身, 灰口铸铁'/></div>"
    "<div><label>一句话摘要</label><input name='summary' placeholder='大型机床床身批量堆放'/></div>"
    "<div><label>细节说明</label><textarea name='details' rows='2'></textarea></div>"
    "<div><button type='submit'>上传并入库</button></div></form>"
    "<p class='muted' id='upload-msg'></p></section>"
    "<section><h2>模拟召回</h2><form id='recall'>"
    "<div><label>查询词</label><input name='query' placeholder='机床 床身'/></div>"
    "<div><label>返回条数</label><input name='top_k' type='number' value='3' min='1'/></div>"
    "<div><button type='submit'>执行召回</button></div></form>"
    "<div class='grid' id='recall-result'></div></section>"
    "<section><h2>素材清单</h2><div class='grid' id='assets'></div></section>"
    "</main><script>"
    "function esc(v){var d=document.createElement('div');d.textContent=v==null?'':String(v);"
    "return d.innerHTML;}"
    "function assetCard(a,extra){var tag='tag'+(a.status==='new'?' new':(a.status==='cooling'?' cooling':''));"
    "return \"<div class='asset'><h3>\"+esc(a.file_name)+\"</h3><span class='\"+tag+\"'>\"+esc(a.status_label)"
    "+\"</span><p class='muted'>\"+esc(a.category)+\"</p><p>\"+esc(a.summary)+\"</p>\"+(extra||'')+\"</div>\";}"
    "function refresh(){fetch('/api/state').then(function(r){return r.json();}).then(function(d){"
    "var c=d.capacity;var b=document.getElementById('banner');"
    "b.className='banner '+(c.alert==='red'?'red':'ok');"
    "b.textContent=(c.alert==='red'?('红色预警：可召回容量仅 '+c.available+' 张，低于阈值 '"
    "+c.threshold+' 张（约一周用量），请立即补充实拍素材。'):('容量正常：可召回 '+c.available+' 张 / 共 '"
    "+c.total+' 张（冷却中 '+c.cooling+' 张，新图 '+c.new+' 张）。'));"
    "var cards=[['可召回',c.available],['冷却中',c.cooling],['新图',c.new],['老图',c.legacy],"
    "['总量',c.total],['阈值',c.threshold]];"
    "document.getElementById('cards').innerHTML=cards.map(function(p){"
    "return \"<div class='card'>\"+p[0]+\"<b>\"+p[1]+\"</b></div>\";}).join('');"
    "document.getElementById('assets').innerHTML=d.assets.map(function(a){"
    "return assetCard(a,\"<button onclick=\\\"markUsed('\"+a.asset_id+\"')\\\">标记已用于内容</button>\");"
    "}).join('');});}"
    "function markUsed(id){fetch('/api/usage',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({asset_id:id})}).then(refresh);}"
    "document.getElementById('upload').addEventListener('submit',function(e){e.preventDefault();"
    "fetch('/api/assets',{method:'POST',body:new FormData(e.target)}).then(function(r){return r.json();})"
    ".then(function(d){document.getElementById('upload-msg').textContent="
    "d.ok?('已入库：'+d.file_name+'（'+d.asset_id+'）'):('失败：'+d.error);if(d.ok){refresh();}});});"
    "document.getElementById('recall').addEventListener('submit',function(e){e.preventDefault();"
    "var f=new FormData(e.target);"
    "fetch('/api/recall',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({query:f.get('query'),top_k:Number(f.get('top_k'))})})"
    ".then(function(r){return r.json();}).then(function(d){"
    "document.getElementById('recall-result').innerHTML=d.picks.map(function(p){"
    "return \"<div class='asset'><h3>\"+esc(p.file_name)+\"</h3><span class='tag'>\""
    "+(p.is_new?'新图加权':'老图')+\"</span><p class='muted'>权重 \"+p.weight+\" ｜ 相似度 \"+p.similarity"
    "+\"</p><p>\"+esc(p.summary)+\"</p></div>\";}).join('')"
    "||\"<p class='muted'>没有可召回素材（可能全部处于冷却期）。</p>\";});});"
    "refresh();</script></body></html>"
)


class MediaConsoleApp:
    """控制台的后端用例层（可脱离 HTTP 单独测试）。"""

    def __init__(
        self,
        *,
        rag_root: str | Path,
        media_root: str | Path,
        ledger_path: str | Path,
        registry_path: str | Path | None = None,
        config: RecallConfig | None = None,
    ) -> None:
        self.config = config or RecallConfig()
        self.rag_root = Path(rag_root)
        self.media_root = Path(media_root)
        self.registry = MediaRegistry(registry_path or self.rag_root / DEFAULT_REGISTRY_NAME)
        self.ledger = RecallLedger(ledger_path)
        self.policy = RecallPolicy(self.ledger, self.config)
        self.ingestor = MediaIngestor(
            rag_root=self.rag_root,
            media_root=self.media_root,
            registry=self.registry,
            brand=self.config.brand,
        )

    def assets(self) -> list[MediaAsset]:
        return load_catalog(self.rag_root, self.registry, brand=self.config.brand)

    def state(self, *, now: datetime | None = None) -> dict[str, Any]:
        """素材清单 + 容量报告（含每张图的冷却状态）。"""
        reference = now or datetime.now(timezone.utc)
        assets = self.assets()
        capacity = self.policy.capacity(assets, now=reference)
        items: list[dict[str, Any]] = []
        for asset in assets:
            remaining = self.policy.cooldown_remaining(asset, now=reference)
            days_left = 0
            if remaining.total_seconds() > 0:
                days_left = max(1, remaining.days + (1 if remaining.seconds else 0))
                status, label = "cooling", f"冷却中 · 剩 {days_left} 天"
            elif asset.is_legacy:
                status, label = "legacy", "可用（老图）"
            else:
                status, label = "new", "可用（新图，优先召回）"
            items.append(
                {
                    "asset_id": asset.asset_id,
                    "file_name": asset.file_name,
                    "category": asset.category,
                    "summary": asset.summary,
                    "keywords": list(asset.keywords),
                    "status": status,
                    "status_label": label,
                    "is_new": not asset.is_legacy,
                    "added_at": asset.added_at.isoformat() if asset.added_at else None,
                    "cooldown_days_left": days_left,
                }
            )
        return {
            "brand": capacity.brand,
            "config": {
                "cooldown_days": self.config.cooldown_days,
                "capacity_red_threshold": self.config.capacity_red_threshold,
                "freshness_window_days": self.config.freshness_window_days,
                "freshness_boost": self.config.freshness_boost,
            },
            "capacity": {
                "total": capacity.total,
                "available": capacity.available,
                "cooling": capacity.cooling,
                "new": capacity.new,
                "legacy": capacity.legacy,
                "videos": capacity.videos,
                "threshold": capacity.threshold,
                "alert": capacity.alert,
            },
            "assets": items,
        }

    def upload(
        self,
        *,
        file_name: str,
        data: bytes,
        process: str,
        sub_process: str,
        keywords: tuple[str, ...] = (),
        summary: str = "",
        details: str = "",
    ) -> dict[str, Any]:
        """上传一张实拍图并入库。"""
        asset = self.ingestor.add_image(
            file_name=file_name,
            process=process,
            sub_process=sub_process,
            data=data,
            keywords=keywords,
            summary=summary,
            details=details,
        )
        return {
            "ok": True,
            "asset_id": asset.asset_id,
            "file_name": asset.file_name,
            "category": asset.category,
        }

    def mark_used(self, *, asset_id: str, content_id: str | None = None) -> dict[str, Any]:
        """标记素材已实际进入内容 / 发布（触发冷却）。"""
        inserted = self.ledger.mark_used(
            asset_id, brand=self.config.brand, content_id=content_id, event="used"
        )
        return {
            "ok": True,
            "duplicate": not inserted,
            "asset_id": asset_id,
            "cooldown_days": self.config.cooldown_days,
        }

    def recall(
        self, *, query: str = "", top_k: int | None = None, now: datetime | None = None
    ) -> dict[str, Any]:
        """按策略召回（控制台用关键词相似度替代向量检索）。"""
        assets = self.assets()
        candidates = [
            MediaCandidate(asset=asset, similarity=RecallPolicy.keyword_similarity(query, asset))
            for asset in assets
        ]
        picks = self.policy.recall(candidates, top_k=top_k, now=now)
        return {
            "query": query,
            "count": len(picks),
            "picks": [
                {
                    "asset_id": pick.asset_id,
                    "file_name": pick.file_name,
                    "category": pick.category,
                    "summary": pick.summary,
                    "similarity": pick.similarity,
                    "weight": pick.weight,
                    "is_new": pick.is_new,
                }
                for pick in picks
            ],
        }


def _disposition_value(disposition: str, key: str) -> str:
    marker = f'{key}="'
    start = disposition.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = disposition.find('"', start)
    return disposition[start:end] if end > start else ""


def parse_multipart(
    body: bytes, boundary: str
) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """解析 multipart/form-data（标准库实现，不依赖 cgi 与三方库）。"""
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    delimiter = ("--" + boundary).encode("utf-8")
    for chunk in body.split(delimiter):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        header_blob, separator, raw = chunk.partition(b"\r\n\r\n")
        if not separator:
            continue
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        headers: dict[str, str] = {}
        for line in header_blob.decode("utf-8", "replace").splitlines():
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        name = _disposition_value(headers.get("content-disposition", ""), "name")
        if not name:
            continue
        file_name = _disposition_value(headers.get("content-disposition", ""), "filename")
        if file_name:
            files[name] = (file_name, raw)
        else:
            fields[name] = raw.decode("utf-8", "replace")
    return fields, files


class _Handler(BaseHTTPRequestHandler):
    app: MediaConsoleApp
    server_version = "PulseMediaConsole/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 标准库签名
        """静默访问日志。"""

    def do_GET(self) -> None:  # noqa: N802 - 标准库命名
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_bytes(PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send_json(self.app.state())
        else:
            self._send_json({"ok": False, "error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - 标准库命名
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            if path == "/api/assets":
                self._handle_upload(body)
            elif path == "/api/usage":
                payload = json.loads(body or b"{}")
                self._send_json(
                    self.app.mark_used(
                        asset_id=str(payload.get("asset_id", "")),
                        content_id=payload.get("content_id"),
                    )
                )
            elif path == "/api/recall":
                payload = json.loads(body or b"{}")
                top_k = payload.get("top_k")
                self._send_json(
                    self.app.recall(
                        query=str(payload.get("query", "")),
                        top_k=int(top_k) if top_k else None,
                    )
                )
            else:
                self._send_json({"ok": False, "error": "not found"}, status=404)
        except (UnsupportedMediaError, DuplicateMediaError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"ok": False, "error": f"请求不合法：{exc}"}, status=400)

    def _handle_upload(self, body: bytes) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type or "boundary=" not in content_type:
            raise ValueError("需要 multipart/form-data 上传")
        boundary = content_type.split("boundary=", 1)[1].strip().strip('"')
        fields, files = parse_multipart(body, boundary)
        if "file" not in files:
            raise ValueError("缺少 file 字段")
        file_name, data = files["file"]
        keywords = tuple(
            item.strip() for item in (fields.get("keywords") or "").split(",") if item.strip()
        )
        self._send_json(
            self.app.upload(
                file_name=file_name,
                data=data,
                process=fields.get("process", "").strip(),
                sub_process=fields.get("sub_process", "").strip(),
                keywords=keywords,
                summary=fields.get("summary", "").strip(),
                details=fields.get("details", "").strip(),
            )
        )

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        self._send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
        )

    def _send_bytes(self, data: bytes, content_type: str, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def create_server(
    app: MediaConsoleApp, *, host: str = "127.0.0.1", port: int = 0
) -> ThreadingHTTPServer:
    """创建控制台 HTTP 服务（port=0 时由系统分配空闲端口）。"""
    handler = type("BoundHandler", (_Handler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:  # pragma: no cover - 手工启动入口
    import argparse

    parser = argparse.ArgumentParser(description="Pulse 素材库控制台")
    parser.add_argument("--rag-root", default="RAG知识库/图片描述")
    parser.add_argument("--media-root", default="../菲美得产品图片")
    parser.add_argument("--ledger", default=".pulse/media_ledger.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app = MediaConsoleApp(
        rag_root=args.rag_root,
        media_root=args.media_root,
        ledger_path=args.ledger,
        config=RecallConfig(brand=BRAND_NAME),
    )
    server = create_server(app, host=args.host, port=args.port)
    print(f"素材库控制台已启动：http://{args.host}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        server.shutdown()


if __name__ == "__main__":  # pragma: no cover
    main()
