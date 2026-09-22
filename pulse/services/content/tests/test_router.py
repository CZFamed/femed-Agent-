"""W1-A1-6：模型路由与 token 预算熔断。"""

from __future__ import annotations

import pytest

from pulse.services.content.errors import TokenBudgetExceeded, UnknownTaskError
from pulse.services.content.router import (
    TASK_LONG_FORM,
    TASK_REWRITE,
    TASK_SELF_REVIEW,
    ModelRouter,
    TokenBudget,
)


def test_routes_use_heavy_model_for_long_form_and_light_for_rewrite():
    router = ModelRouter()

    assert router.route(TASK_LONG_FORM).model == "heavy-writer"
    assert router.route(TASK_REWRITE).model == "light-rewriter"
    assert router.route(TASK_SELF_REVIEW).model == "light-reviewer"


def test_unknown_task_raises_instead_of_silent_fallback():
    router = ModelRouter()

    with pytest.raises(UnknownTaskError) as exc:
        router.route("translate")

    assert "translate" in str(exc.value)


def test_budget_accumulates_and_reports_remaining():
    budget = TokenBudget(limit=1_000)

    budget.consume(400, task=TASK_LONG_FORM)
    budget.consume(200, task=TASK_REWRITE)

    assert budget.used == 600
    assert budget.remaining == 400
    assert budget.as_payload() == {"used": 600, "limit": 1_000, "remaining": 400}


def test_budget_circuit_breaks_instead_of_truncating():
    """契约要求熔断：超预算必须抛错，而不是悄悄截断文案。"""
    budget = TokenBudget(limit=1_000)
    budget.consume(900, task=TASK_LONG_FORM)

    with pytest.raises(TokenBudgetExceeded) as exc:
        budget.consume(200, task=TASK_LONG_FORM)

    details = exc.value.details
    assert details["used"] == 900
    assert details["requested"] == 200
    assert details["limit"] == 1_000
    # 失败后账本不变：不产生"扣了一半"的中间状态
    assert budget.used == 900


def test_charge_routes_then_charges_and_unknown_task_does_not_spend_budget():
    router = ModelRouter(budget=TokenBudget(limit=1_000))

    route = router.charge(TASK_LONG_FORM, 300)
    assert route.model == "heavy-writer"
    assert router.budget.used == 300

    with pytest.raises(UnknownTaskError):
        router.charge("nope", 100)
    assert router.budget.used == 300


def test_budget_rejects_non_positive_limit():
    with pytest.raises(ValueError):
        TokenBudget(limit=0)


def test_budget_rejects_negative_usage():
    budget = TokenBudget(limit=100)

    with pytest.raises(ValueError):
        budget.consume(-1)


def test_can_afford_matches_consume_boundary():
    budget = TokenBudget(limit=100)

    assert budget.can_afford(100) is True
    assert budget.can_afford(101) is False
    budget.consume(100)
    assert budget.can_afford(0) is True
    assert budget.can_afford(1) is False
