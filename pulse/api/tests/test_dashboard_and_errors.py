"""W2-A5-3 与错误映射：看板 KPI 口径、统一错误体、异常翻译。"""

from __future__ import annotations

from pulse.api.errors import ApiError, ApiResponse, map_exception
from pulse.services.compliance import FindingNotFoundError, WaiverNotAllowedError
from pulse.services.content import BriefValidationError, TokenBudgetExceeded
from pulse.services.scheduler import QuotaExceeded, ScheduleNotFound


def test_dashboard_uses_inquiry_basis_not_likes(app, approved):
    scheduled = app.handle(
        "POST",
        f"/api/v1/variants/{approved}/schedule",
        body={"scheduled_at": "2026-09-23T10:00:00+05:30"},
    )
    assert scheduled.status == 201

    payload = app.dashboard()

    assert "询盘与触达口径" in payload["kpi_basis"]
    assert "点赞" in payload["kpi_basis"]
    assert payload["publishing"]["jobs_total"] == 1
    assert payload["content"]["variants_total"] == 1
    assert set(payload["funnel"]) >= {"reached_companies", "inquiries", "shortlisted", "note"}
    # 三个漏斗指标目前必须是"待接数据源"，不能编数
    assert payload["funnel"]["inquiries"] is None


def test_api_error_uses_unified_body():
    response = ApiError(403, "forbidden", "不行", details={"why": "权限"}).response()

    assert isinstance(response, ApiResponse)
    assert response.status == 403
    assert response.body == {
        "error": {"code": "forbidden", "message": "不行", "details": {"why": "权限"}}
    }


def test_map_exception_translates_domain_errors():
    cases = [
        (BriefValidationError(["topic 不能为空"]), 400),
        (TokenBudgetExceeded("超预算"), 429),
        (WaiverNotAllowedError("不许豁免"), 403),
        (FindingNotFoundError("没有这条 finding"), 404),
        (ScheduleNotFound("没有这个排期"), 404),
        (QuotaExceeded("配额不足"), 429),
    ]

    for exc, expected_status in cases:
        response = map_exception(exc)
        assert response.status == expected_status, exc
        assert set(response.body["error"]) == {"code", "message", "details"}


def test_map_exception_hides_unknown_internals():
    response = map_exception(RuntimeError("数据库连接串 password=secret"))

    assert response.status == 500
    assert response.body["error"]["code"] == "internal_error"
    assert "password" not in str(response.body)
    assert response.body["error"]["details"]["exception"] == "RuntimeError"
