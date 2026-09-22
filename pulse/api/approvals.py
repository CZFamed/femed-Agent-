"""审批工作流（所有者：A5，任务包 W2-A5-2；FR-4 是 P0）。

契约 §3.1 的状态机：`draft → pending_review → approved | rejected | needs_revision`。
三条不可妥协的规则：

1. **未通过的内容不得进入发布池**（需求 US-3）——排期端点会拒绝非 `approved` 的变体；
2. **三个触发条件下必须逐条复核、不得整批通过**：首次接入某账号、命中合规告警、更换 Brand Guide；
3. **审批留痕含 diff**：谁、什么时候、改了什么，都要能查（契约 §6 `approvals` 表）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from pulse.api.errors import conflict, forbidden, not_found
from pulse.shared.enums import VariantStatus

#: 审批动作（契约 §6 `approvals.action`）
APPROVE = "approve"
REJECT = "reject"
NEEDS_REVISION = "needs_revision"
BATCH_APPROVE = "batch_approve"

ACTION_TO_STATUS: Mapping[str, VariantStatus] = {
    APPROVE: VariantStatus.APPROVED,
    REJECT: VariantStatus.REJECTED,
    NEEDS_REVISION: VariantStatus.NEEDS_REVISION,
}

#: 只有这些动作既写 `approvals` 又改 `variants.status`
ACTIONS: tuple[str, ...] = (APPROVE, REJECT, NEEDS_REVISION, BATCH_APPROVE)

#: 允许被审批的当前状态（草稿要先提交待审）
REVIEWABLE_STATUSES: frozenset[VariantStatus] = frozenset(
    {VariantStatus.PENDING_REVIEW}
)


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """`approvals` 表的一行。"""

    id: int
    variant_id: str
    actor: str
    action: str
    diff: Mapping[str, Any] | None = None
    created_at: datetime | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "variant_id": self.variant_id,
            "actor": self.actor,
            "action": self.action,
            "diff": dict(self.diff) if self.diff else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """整批通过的结果：谁通过、谁被拒以及为什么（不静默跳过）。"""

    approved: tuple[str, ...] = ()
    refused: tuple[tuple[str, str], ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "approved": list(self.approved),
            "refused": [{"variant_id": item, "reason": reason} for item, reason in self.refused],
        }


@dataclass
class ApprovalService:
    """审批状态的唯一裁决者（状态机只有这一份实现）。"""

    content: Any
    compliance: Any | None = None
    _records: list[ApprovalRecord] = field(default_factory=list, init=False, repr=False)
    _next_id: int = field(default=1, init=False, repr=False)

    # ---------- 提交待审 ----------

    def submit(
        self, variant_id: str, *, actor: str = "system", now: datetime | None = None
    ) -> dict[str, Any]:
        """`draft → pending_review`。"""
        variant = self._variant(variant_id)
        if variant.status is not VariantStatus.DRAFT:
            raise conflict(
                f"变体 {variant_id} 当前状态为 {variant.status.value}，只有 draft 可以提交待审",
                variant_id=variant_id,
                status=variant.status.value,
            )
        updated = self.content.set_variant_status(variant_id, VariantStatus.PENDING_REVIEW)
        return {"variant_id": variant_id, "status": updated.status.value, "submitted_by": actor}

    # ---------- 单条审批 ----------

    def decide(
        self,
        variant_id: str,
        *,
        action: str,
        actor: str,
        diff: Mapping[str, Any] | None = None,
        brand_guide_changed: bool = False,
        is_admin: bool = False,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        """单条审批：通过 / 驳回 / 改稿（整批通过走 `batch_approve`）。"""
        act = str(action or "").strip().lower()
        if act not in ACTIONS:
            raise conflict(
                f"未知审批动作 {action!r}（允许：{'、'.join(ACTIONS)}）", action=action
            )
        if act == BATCH_APPROVE:
            raise conflict("整批通过请用 batch_approve（它需要逐个检查强制复核条件）")
        name = str(actor or "").strip()
        if not name:
            raise forbidden("审批必须写明操作人（留痕要求：谁、什么时候、改了什么）")

        variant = self._reviewable(variant_id)
        if act == APPROVE:
            self._assert_approvable(variant_id, is_admin=is_admin)

        assert variant is not None
        status = ACTION_TO_STATUS[act]
        self.content.set_variant_status(variant_id, status)
        record = self._record(variant_id, name, act, diff, now)
        return record

    def batch_approve(
        self,
        variant_ids: Iterable[str],
        *,
        actor: str,
        diff: Mapping[str, Any] | None = None,
        brand_guide_changed: bool = False,
        is_admin: bool = False,
        now: datetime | None = None,
    ) -> BatchOutcome:
        """整批通过：**命中强制复核条件的会被拒绝并说明原因**，不静默跳过。"""
        approved: list[str] = []
        refused: list[tuple[str, str]] = []
        for variant_id in variant_ids:
            variant_id = str(variant_id)
            try:
                variant = self._reviewable(variant_id)
                del variant
                self._assert_approvable(
                    variant_id,
                    is_admin=is_admin,
                    brand_guide_changed=brand_guide_changed,
                    batch=True,
                )
            except Exception as exc:  # noqa: BLE001 - 逐条降级，不中断整批
                refused.append((variant_id, getattr(exc, "message", str(exc))))
                continue
            self.content.set_variant_status(variant_id, VariantStatus.APPROVED)
            self._record(variant_id, str(actor).strip() or "system", BATCH_APPROVE, diff, now)
            approved.append(variant_id)
        return BatchOutcome(approved=tuple(approved), refused=tuple(refused))

    # ---------- 查询 ----------

    def history(self, variant_id: str | None = None) -> tuple[ApprovalRecord, ...]:
        if variant_id is None:
            return tuple(self._records)
        return tuple(item for item in self._records if item.variant_id == variant_id)

    def must_review_individually(
        self, variant_id: str, *, brand_guide_changed: bool = False
    ) -> tuple[str, ...]:
        """转发到内容/合规口径的强制复核判定（不在本模块重写一套规则）。"""
        if self.compliance is None:
            return ("brand_guide_changed",) if brand_guide_changed else ()
        from pulse.services.compliance import VariantView

        variant = self._variant(variant_id)
        view = VariantView.from_variant_record(variant, platform=variant.platform.value)
        outcome = self.compliance.outcome_for(variant_id)
        return self.compliance.must_review_individually(
            view, outcome, brand_guide_changed=brand_guide_changed
        )

    # ---------- 内部 ----------

    def _variant(self, variant_id: str) -> Any:
        try:
            return self.content.get_variant(str(variant_id))
        except Exception as exc:  # noqa: BLE001 - 统一转成 404
            raise not_found(f"找不到变体 {variant_id}", variant_id=variant_id) from exc

    def _reviewable(self, variant_id: str) -> Any:
        """要求变体处于 `pending_review`（草稿必须先提交待审）。"""
        variant = self._variant(variant_id)
        if variant.status not in REVIEWABLE_STATUSES:
            raise conflict(
                f"变体 {variant_id} 当前状态为 {variant.status.value}，"
                "只有 pending_review 可以被审批（草稿请先提交待审）",
                variant_id=variant_id,
                status=variant.status.value,
            )
        return variant

    def _assert_approvable(
        self,
        variant_id: str,
        *,
        is_admin: bool,
        brand_guide_changed: bool = False,
        batch: bool = False,
    ) -> None:
        """通过前的两道闸。

        1. 合规硬拦截：`block` 未豁免就不许通过（管理员豁免例外，且豁免本身留痕）；
        2. **整批通过**时额外检查"必须逐条复核"的三个条件（首次接入账号 / 命中合规告警 /
           更换 Brand Guide）——命中任意一条，这一条就不许进整批，必须单条走。
           单条审批（`decide`）本身就是逐条复核，所以不触发这条拦截。
        """
        if self.compliance is not None:
            outcome = self.compliance.outcome_for(variant_id)
            if outcome.blocked and not is_admin:
                raise forbidden(
                    f"变体 {variant_id} 命中合规硬拦截（{outcome.blocking[0].rule}），不得通过；"
                    "确需放行必须由管理员豁免并留痕",
                    variant_id=variant_id,
                    blocking=[item.rule for item in outcome.blocking],
                )
        if batch:
            triggers = self.must_review_individually(
                variant_id, brand_guide_changed=brand_guide_changed
            )
            if triggers:
                raise conflict(
                    f"变体 {variant_id} 命中必须逐条复核的条件（{'、'.join(triggers)}），"
                    "不得整批通过；请单条审批",
                    variant_id=variant_id,
                    triggers=list(triggers),
                )

    def _record(
        self,
        variant_id: str,
        actor: str,
        action: str,
        diff: Mapping[str, Any] | None,
        now: datetime | None,
    ) -> ApprovalRecord:
        record = ApprovalRecord(
            id=self._next_id,
            variant_id=variant_id,
            actor=actor,
            action=action,
            diff=dict(diff) if diff else None,
            created_at=now or datetime.now(timezone.utc),
        )
        self._next_id += 1
        self._records.append(record)
        return record
