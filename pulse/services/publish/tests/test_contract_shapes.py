"""契约形状测试：ABC 签名、类型复用、默认不发起真实请求（派工单 §3 W1-A2-1/2）。"""

from __future__ import annotations

import inspect
import json

import pytest
import pulse.services.publish.base as base
from pulse.services.publish import PlatformAdapter
from pulse.services.publish.transport import HttpRequest, no_transport
from pulse.shared.models import PublishResult, SemiAutoBundle, UnifiedPost


def test_platform_adapter_is_abstract() -> None:
    """ABC 不可直接实例化（契约 §4）。"""

    with pytest.raises(TypeError):
        PlatformAdapter()  # type: ignore[abstract]


def test_subclass_missing_methods_cannot_instantiate() -> None:
    """子类缺方法必须报错，避免"漏实现一个方法还能跑"。"""

    class Partial(PlatformAdapter):
        platform = "linkedin"

        async def validate(self, post: UnifiedPost) -> list[str]:
            return []

    with pytest.raises(TypeError):
        Partial()  # type: ignore[abstract]


def test_abstract_method_set_matches_contract() -> None:
    """五个抽象方法名与契约 §4 完全一致。"""

    assert PlatformAdapter.__abstractmethods__ == frozenset(
        {"validate", "find_existing", "publish", "poll_finalize", "fetch_metrics"}
    )


def test_method_signatures_are_verbatim() -> None:
    """方法参数逐字对齐契约 §4。"""

    assert list(inspect.signature(PlatformAdapter.publish).parameters) == [
        "self",
        "post",
        "credential",
    ]
    assert list(inspect.signature(PlatformAdapter.poll_finalize).parameters) == [
        "self",
        "platform_post_id",
        "credential",
    ]
    assert list(inspect.signature(base.SemiAutoExporter.export).parameters) == [
        "self",
        "post",
    ]


def test_shared_types_are_reused_not_redefined() -> None:
    """`UnifiedPost` / `PublishResult` / `SemiAutoBundle` 必须复用 shared 契约类型。"""

    assert base.PublishResult is PublishResult
    assert base.SemiAutoBundle is SemiAutoBundle
    assert base.UnifiedPost is UnifiedPost


@pytest.mark.asyncio
async def test_default_transport_refuses_real_requests() -> None:
    """默认传输层直接报错——防止忘记注入假传输时真的发请求。"""

    with pytest.raises(RuntimeError, match="拒绝发起真实请求"):
        await no_transport(HttpRequest(method="POST", url="https://api.linkedin.com/rest/posts"))


def test_publish_result_guard_still_enforced() -> None:
    """受理 ≠ 发布：`ok=True + publishing` 必须带 platform_post_id 才能轮询。"""

    with pytest.raises(Exception):
        PublishResult(ok=True, status="publishing")


def test_token_never_leaks_into_result() -> None:
    """Adapter 的 `raw` 只放状态码，不得夹带 Authorization 头。"""

    result = PublishResult(ok=False, status="failed", raw={"status_code": 429})
    assert json.dumps(result.raw) == '{"status_code": 429}'
    assert "token" not in json.dumps(result.raw).lower()
