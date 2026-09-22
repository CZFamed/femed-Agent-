"""A5 接口层的契约一致性：**解析 INTERFACES.md §7 当外部真源**，再对代码。

与 `test_contract_conformance.py` 同一思路：真源是契约文本，不是代码里抄的一份常量。
抄常量只能证明"代码没变"，解析契约才能证明"代码没跑偏"。
"""

from __future__ import annotations

import re
from pathlib import Path

from pulse.api import ApiApp

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "INTERFACES.md"


def _contract_routes() -> set[tuple[str, str]]:
    """从契约 §7 的 Markdown 表格里抠出 (方法, 路径)。"""
    text = CONTRACT.read_text(encoding="utf-8")
    section = text.split("## 7. REST API", 1)[1].split("## 8.", 1)[0]
    rows = re.findall(r"^\|\s*(GET|POST|PATCH|DELETE|PUT)\s*\|\s*`([^`]+)`", section, re.M)
    return {(method, path) for method, path in rows}


def test_routes_match_contract_section_7_verbatim():
    contract = _contract_routes()

    assert len(contract) == 11, f"契约 §7 应恰好 11 条端点，解析到 {len(contract)} 条"
    actual = {(item["method"], item["path"]) for item in ApiApp.route_table()}
    assert actual == contract, {
        "只在代码里": sorted(actual - contract),
        "只在契约里": sorted(contract - actual),
    }


def test_every_contract_route_is_implemented_and_routed():
    """每条契约路径都要真的能路由到处理器（不是只在表里登记）。"""
    app_routes = {(item["method"], item["path"]) for item in ApiApp.route_table()}

    for method, path in _contract_routes():
        assert (method, path) in app_routes, f"契约端点未实现：{method} {path}"


def test_error_body_shape_is_documented_and_implemented():
    """契约 §7 写的统一错误体，与代码返回的形状一致。"""
    text = CONTRACT.read_text(encoding="utf-8")
    assert '{"error": {"code": "...", "message": "...", "details": {}}}' in text

    from pulse.api.errors import ApiError

    body = ApiError(409, "conflict", "x").response().body
    assert set(body["error"]) == {"code", "message", "details"}
