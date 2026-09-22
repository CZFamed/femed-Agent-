"""合规服务（所有者：A4）：判定、豁免留痕、制裁筛查、发布前闸门。

对外只有四件事：

1. `check(view)` 对一个 variant 跑规则，返回结构化判定并**落库（内存）**，拿到 finding ID；
2. `waive(...)` 豁免 `warn`（留痕：谁、什么时候、为什么）；`block` 默认不可绕过；
3. `screen_subject(...)` 制裁与出口管制筛查（薄封装，记录见 `sanctions.py`）；
4. `compliance_info(...)` 产出契约 §2 的 `ComplianceInfo`（含 `blocked` 与 `findings_ref`），
   交给 A3/A2 下传——`blocked=True` 时发布网关必须拒绝发布。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from pulse.services.compliance.errors import (
    FindingNotFoundError,
    WaiverNotAllowedError,
)
from pulse.services.compliance.models import Finding, VariantView
from pulse.services.compliance.rules import RuleEngine
from pulse.services.compliance.sanctions import ScreeningRecord, screen
from pulse.shared.enums import ComplianceSeverity
from pulse.shared.models import ComplianceInfo

#: FR-4 里"必须逐条复核、不可整批通过"的三个触发条件（契约与需求 US-3）
INDIVIDUAL_REVIEW_TRIGGERS: tuple[str, ...] = (
    "first_time_account",
    "compliance_warning",
    "brand_guide_changed",
)


@dataclass(frozen=True, slots=True)
class ComplianceOutcome:
    """一次判定的结果视图。"""

    variant_id: str
    findings: tuple[Finding, ...] = ()

    @property
    def blocking(self) -> tuple[Finding, ...]:
        """仍然生效的硬拦截项（已豁免的不算）。"""
        return tuple(
            item
            for item in self.findings
            if item.severity is ComplianceSeverity.BLOCK and not item.waived_by
        )

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity is ComplianceSeverity.WARN)

    @property
    def waived(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.waived_by)

    @property
    def blocked(self) -> bool:
        return bool(self.blocking)

    @property
    def findings_ref(self) -> tuple[int, ...]:
        return tuple(int(item.id) for item in self.findings if item.id is not None)

    def as_payload(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "blocked": self.blocked,
            "findings_ref": list(self.findings_ref),
            "blocking": [item.as_payload() for item in self.blocking],
            "warnings": [item.as_payload() for item in self.warnings],
            "waived": [item.as_payload() for item in self.waived],
        }


@dataclass
class ComplianceService:
    """合规域门面。finding 的存储是内存版（跨进程由上层换真实库）。"""

    engine: RuleEngine = field(default_factory=RuleEngine)
    _findings: dict[int, Finding] = field(default_factory=dict, init=False, repr=False)
    _by_variant: dict[str, list[int]] = field(default_factory=dict, init=False, repr=False)
    _next_id: int = field(default=1, init=False, repr=False)

    # ---------- 判定 ----------

    def check(self, view: VariantView, *, checked_at: datetime | None = None) -> ComplianceOutcome:
        """跑一遍规则集并把 finding 落库（拿 ID），返回判定结果。

        `checked_at` 目前只用于留痕（finding 本身不带时间列，时间在库里那一行上）。
        """
        moment = checked_at or datetime.now(timezone.utc)
        raw = self.engine.evaluate(view)
        stored: list[Finding] = []
        for item in raw:
            maybe_id = self._store(item)
            stored.append(maybe_id)
        self._by_variant[view.variant_id] = [int(item.id) for item in stored if item.id]
        del moment  # 时间由调用方的库表负责；这里不做假时间字段
        return ComplianceOutcome(variant_id=view.variant_id, findings=tuple(stored))

    def _store(self, item: Finding) -> Finding:
        finding_id = self._next_id
        self._next_id += 1
        stored = Finding(
            rule=item.rule,
            severity=item.severity,
            message=item.message,
            position=item.position,
            id=finding_id,
            waived_by=item.waived_by,
        )
        self._findings[finding_id] = stored
        return stored

    def get_finding(self, finding_id: int) -> Finding:
        found = self._findings.get(int(finding_id))
        if found is None:
            raise FindingNotFoundError(
                f"找不到 finding {finding_id}", details={"finding_id": finding_id}
            )
        return found

    def findings_for(self, variant_id: str) -> tuple[Finding, ...]:
        return tuple(self.get_finding(item) for item in self._by_variant.get(variant_id, ()))

    def outcome_for(self, variant_id: str) -> ComplianceOutcome:
        """按当前库中状态重建判定（豁免之后调用它拿最新结论）。"""
        return ComplianceOutcome(variant_id=variant_id, findings=self.findings_for(variant_id))

    # ---------- 豁免留痕 ----------

    def waive(
        self,
        finding_id: int,
        *,
        actor: str,
        reason: str = "",
        is_admin: bool = False,
    ) -> Finding:
        """豁免一条 finding 并留痕。

        Raises:
            FindingNotFoundError: finding 不存在。
            WaiverNotAllowedError: 没写豁免人；或试图豁免 `block` 但不是管理员。
        """
        name = str(actor or "").strip()
        if not name:
            raise WaiverNotAllowedError("豁免必须写明豁免人（留痕要求：谁、什么时候、为什么）")
        current = self.get_finding(finding_id)
        if current.severity is ComplianceSeverity.BLOCK and not is_admin:
            raise WaiverNotAllowedError(
                f"finding {finding_id} 是 block 级硬拦截（{current.rule}），"
                "默认不可绕过；确需豁免必须由管理员操作并留痕",
                details={"finding_id": finding_id, "rule": current.rule},
            )
        updated = Finding(
            rule=current.rule,
            severity=current.severity,
            message=current.message,
            position=current.position,
            id=current.id,
            waived_by=name if not reason.strip() else f"{name}（{reason.strip()}）",
        )
        self._findings[int(finding_id)] = updated
        return updated

    # ---------- 制裁与出口管制筛查 ----------

    def screen_subject(
        self,
        subject: str,
        *,
        checked_by: str | None = None,
        lists: Any = None,
        review_hints: Any = None,
        now: datetime | None = None,
    ) -> ScreeningRecord:
        """薄封装：筛查逻辑与数据在 `sanctions.py`，这里只做入口与留痕参数透传。"""
        return screen(
            subject,
            lists=lists,
            review_hints=review_hints,
            checked_by=checked_by,
            now=now,
        )

    # ---------- 发布前闸门 ----------

    def compliance_info(
        self, variant_id: str, *, checked_at: datetime | None = None
    ) -> ComplianceInfo:
        """产出契约 §2 的 `ComplianceInfo`（含 `blocked` 与 `findings_ref`）。

        契约要求：`blocked=True` 时必须带 `findings_ref`，否则无法追溯拦截原因——
        这里保证两者同源，不会出现"拦了但说不出为什么"。
        """
        outcome = self.outcome_for(variant_id)
        return ComplianceInfo(
            ai_generated_disclosure=False,
            checked_at=checked_at or datetime.now(timezone.utc),
            blocked=outcome.blocked,
            findings_ref=outcome.findings_ref,
        )

    def must_review_individually(
        self,
        view: VariantView,
        outcome: ComplianceOutcome,
        *,
        brand_guide_changed: bool = False,
    ) -> tuple[str, ...]:
        """FR-4：返回命中的"必须逐条复核、不可整批通过"触发条件。

        三个条件是需求原文写死的：首次接入某账号、命中合规告警、更换 Brand Guide。
        """
        hits: list[str] = []
        if view.first_time_account:
            hits.append("first_time_account")
        if outcome.warnings or outcome.blocking:
            hits.append("compliance_warning")
        if brand_guide_changed:
            hits.append("brand_guide_changed")
        return tuple(hits)

    # ---------- 队列入口 ----------

    def handle_check(self, payload: Mapping[str, Any], view: VariantView) -> dict[str, Any]:
        """处理 `pulse.compliance.check {variant_id}`。

        载荷里只有 ID（契约 §5），所以业务对象由调用方按 ID 取好后以 `view` 传入；
        两者 ID 必须一致，否则说明接线错了。
        """
        variant_id = str(payload.get("variant_id") or "").strip()
        if not variant_id:
            raise FindingNotFoundError(
                "pulse.compliance.check 的载荷必须是 {variant_id}", details={"payload": dict(payload)}
            )
        if view.variant_id != variant_id:
            raise FindingNotFoundError(
                f"载荷里的 variant_id（{variant_id}）与传入的视图（{view.variant_id}）不一致",
                details={"payload_variant_id": variant_id, "view_variant_id": view.variant_id},
            )
        outcome = self.check(view)
        return {
            "pulse.compliance.check": outcome.as_payload(),
            "must_review_individually": list(self.must_review_individually(view, outcome)),
        }
