"""W2-A6-2 · 集成测试：假 Adapter 端到端闭环（M1 验收标准）。

链路（契约 §8）::

    brief → variant → 合规 → 审批 → 排期 → 发布 → 回执

**哪些环节是真的、哪些是替身**（这条必须写清楚，否则等于虚假完成）：

* 真的：A1 内容域（brief/派生/variant 落库）、A3 调度与账号域（排期/配额/限流/熔断/
  Celery 任务名与载荷）、A2 发布网关域（网关/状态机/幂等/回执收敛/Fake Adapter）、
  identity 域（账号 + Vault 凭据 + 熔断联动）。
* 替身：**合规判定**（A4 未开工，``pulse/services/compliance/`` 不存在）与
  **审批控制台**（A5 未开工，``pulse/api/`` 不存在）。替身按契约 §3.1 / §3.5 / §6
  的语义实现（状态迁移合法 + 动作留痕），并且在用例里显式标注，不假装是真实现。

**已修缺陷（2026-09-22，v1.17.2）**：同步平台返回 ``published`` 时的
``dispatching → published`` 非法迁移（F-1）、以及 A1 与 A3 各自 mint 幂等键
导致两层防线不同键（F-2）。两个用例已从 ``xfail`` 转为正式断言。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from pulse.services.content import AccountBinding, ContentService
from pulse.services.publish import FakeAdapter, PublishGateway
from pulse.services.publish.adapters import FakeBehaviour
from pulse.services.scheduler import Dispatcher, EagerTaskQueue
from pulse.services.scheduler.celery_app import (
    TASK_PUBLISH_DISPATCH,
    TASK_PUBLISH_FINALIZE,
    TASK_SCHEDULE_ENQUEUE,
)
from pulse.services.scheduler.dispatcher import DEFAULT_FINALIZE_POLL_S
from pulse.services.scheduler.tasks import build_wired_app
from pulse.shared.enums import PublishJobStatus, ScheduleStatus, VariantStatus
from pulse.shared.models import ComplianceInfo, UnifiedPost

from pulse.tests.conftest import ACCOUNT_ID, GatewaySink

#: 契约 §3.1 的 variant 状态机（审批台替身据此校验，A5 开工后应改为调用真实现）
APPROVAL_EDGES: dict[VariantStatus, set[VariantStatus]] = {
    VariantStatus.DRAFT: {VariantStatus.PENDING_REVIEW},
    VariantStatus.PENDING_REVIEW: {
        VariantStatus.APPROVED,
        VariantStatus.REJECTED,
        VariantStatus.NEEDS_REVISION,
    },
    VariantStatus.NEEDS_REVISION: {VariantStatus.PENDING_REVIEW},
    VariantStatus.APPROVED: set(),
    VariantStatus.REJECTED: set(),
}


class ApprovalStub:
    """审批台的最小替身（A5 未开工）。

    只做两件契约要求的事：**状态迁移必须合法**、**每次动作必须留痕**
    （留痕字段与契约 §6 ``approvals`` 表一致：variant_id / actor / action / diff）。
    """

    def __init__(self) -> None:
        self.status: VariantStatus = VariantStatus.DRAFT
        self.rows: list[dict[str, Any]] = []

    def _move(self, target: VariantStatus, *, actor: str, action: str, diff: Any = None) -> None:
        if target not in APPROVAL_EDGES[self.status]:
            raise AssertionError(
                f"审批状态不允许 {self.status.value} → {target.value}（契约 §3.1）"
            )
        self.status = target
        self.rows.append({"actor": actor, "action": action, "diff": diff})

    def submit(self, variant_id: str, *, actor: str = "content_agent") -> VariantStatus:
        self.variant_id = variant_id
        self._move(VariantStatus.PENDING_REVIEW, actor=actor, action="submit")
        return self.status

    def approve(self, *, actor: str) -> VariantStatus:
        self._move(VariantStatus.APPROVED, actor=actor, action="approve")
        return self.status


class ComplianceStub:
    """合规判定替身（A4 未开工）：只产出契约 §2 的 ``ComplianceInfo`` 快照。"""

    def __init__(self, blocked: bool = False, findings: tuple[int, ...] = ()) -> None:
        self.blocked = blocked
        self.findings = findings
        self.checked: list[str] = []

    def check(self, variant_id: str, *, checked_at: Any) -> ComplianceInfo:
        self.checked.append(variant_id)
        return ComplianceInfo(
            blocked=self.blocked,
            checked_at=checked_at,
            findings_ref=self.findings if self.blocked else (),
        )


@dataclass
class Chain:
    """一次端到端跑链路的全部中间产物（便于逐段断言可追溯性）。"""

    content: ContentService
    source: Any
    brief_id: str
    derived: Any
    approvals: ApprovalStub
    compliance: ComplianceStub
    schedule: Any
    plan: Any
    post: UnifiedPost
    derived_post: UnifiedPost
    dispatcher: Dispatcher
    queue: EagerTaskQueue
    gateway: PublishGateway
    adapter: FakeAdapter
    sink: GatewaySink
    app: Any

    def run_task(self, task_name: str, *args: Any) -> dict[str, Any]:
        """用**真实 Celery app**（eager 模式）跑任务，而不是直接调处理器。"""
        result = self.app.tasks[task_name].apply(args=list(args))
        value = result.get()
        assert isinstance(value, dict), f"{task_name} 应返回字典，收到 {type(value).__name__}"
        return value


BRIEF = {
    "brief_id": "b_a6_e2e_0001",
    "topic": "Valve body castings with pre-machining for machine tool builders",
    "target_audience": "machine tool OEMs in India without an in-house foundry",
    "platforms": ["linkedin"],
    "cta": "Send us your drawing and we will quote casting plus machining",
}


def build_chain(
    *,
    content: ContentService,
    binding: AccountBinding,
    dispatcher: Dispatcher,
    queue: EagerTaskQueue,
    identity: Any,
    clock: Any,
    behaviour: FakeBehaviour | str = FakeBehaviour.PENDING_THEN_PUBLISHED,
    blocked_compliance: bool = False,
) -> Chain:
    """把 1→6 步跑完（生成 → 合规 → 审批 → 排期 → 入队），返回全部产物。"""
    # 1) A1：brief → 核心内容（一条 contents 行，幂等）
    source = content.submit_brief(BRIEF)
    assert content.submit_brief(BRIEF).id == source.id, "同一 brief 必须幂等"

    # 2) A1：派生单平台 variant（无素材库 → 纯文本；固定文案桩，不调真模型）
    derived = content.generate_variant(BRIEF["brief_id"], "linkedin", account=binding)
    post = derived.post

    # 3) A4（替身）：合规判定
    compliance = ComplianceStub(blocked=blocked_compliance, findings=(7001,) if blocked_compliance else ())
    info = compliance.check(derived.variant_id, checked_at=clock())
    post = dataclasses.replace(post, compliance=info)

    # 4) A5（替身）：审批留痕
    approvals = ApprovalStub()
    approvals.submit(derived.variant_id)
    approvals.approve(actor="ops_reviewer")

    # 5) A3：排期（带偏移时间；账号事实来自 identity）
    schedule = dispatcher.create_schedule(
        variant_id=derived.variant_id,
        account_id=ACCOUNT_ID,
        scheduled_at=clock() + timedelta(days=1),
    )

    # 6) A3：入队（经真实队列（记录式），载荷只含 ID）
    #    幂等键由上游（A1 派生时的 post）传入，A3 不再自己 mint 一个新的
    plan = dispatcher.enqueue_schedule(
        schedule.id, unified_post_id=derived.post.unified_post_id
    )

    # 7) 幂等键衔接（缺口 #2，2026-09-22 已修）：编排层把 A1 派生时 mint 的幂等键
    #    透传给 A3，``publish_jobs.unified_post_id`` 与 Adapter 的 ``find_existing()``
    #    从此守的是**同一个键**（契约 §6「幂等保证」）。
    job = dispatcher.jobs.get(plan.job_id)
    assert job is not None
    published_post = post

    # 8) A2：网关 + 假 Adapter；sink 把两个域扣在一起（等价于部署层的接线）
    adapter = FakeAdapter("linkedin", behaviour)
    gateway = PublishGateway(adapters=[adapter], now=clock)
    credential = identity.bind(ACCOUNT_ID)
    sink = GatewaySink(
        gateway,
        posts={published_post.unified_post_id: published_post},
        credentials={ACCOUNT_ID: credential},
    )
    app = build_wired_app(dispatcher=dispatcher, sink=sink, queue=queue)

    return Chain(
        content=content,
        source=source,
        brief_id=BRIEF["brief_id"],
        derived=derived,
        approvals=approvals,
        compliance=compliance,
        schedule=schedule,
        plan=plan,
        post=published_post,
        derived_post=derived.post,
        dispatcher=dispatcher,
        queue=queue,
        gateway=gateway,
        adapter=adapter,
        sink=sink,
        app=app,
    )


@pytest.fixture
def chain(content, binding, dispatcher, queue, identity, clock) -> Chain:
    """默认链路：Fake Adapter 先受理（pending_finalize）、轮询后 published。"""
    return build_chain(
        content=content,
        binding=binding,
        dispatcher=dispatcher,
        queue=queue,
        identity=identity,
        clock=clock,
    )


# --------------------------------------------------------------------------
# 闭环
# --------------------------------------------------------------------------


def test_end_to_end_closed_loop_reaches_published(chain):
    """M1 验收标准：单账号单平台跑通「生成 → 审核 → 发布 → 回执」。"""
    job_id = chain.plan.job_id
    assert job_id and job_id.startswith("job_")

    # 投递：平台受理 → **只能**进 pending_finalize（契约 §3.3）
    dispatched = chain.run_task(TASK_PUBLISH_DISPATCH, job_id, chain.post.unified_post_id)
    assert dispatched["status"] == PublishJobStatus.PENDING_FINALIZE.value
    job = chain.dispatcher.jobs.get(job_id)
    assert job.status_value is PublishJobStatus.PENDING_FINALIZE
    assert job.finalize_deadline is not None
    assert chain.dispatcher.schedules.get(chain.schedule.id).status_value is ScheduleStatus.PUBLISHING

    # 回执：收敛为 published
    settled = chain.run_task(TASK_PUBLISH_FINALIZE, job_id)
    assert settled["status"] == PublishJobStatus.PUBLISHED.value
    assert settled["schedule_status"] == ScheduleStatus.PUBLISHED.value
    assert chain.dispatcher.schedules.get(chain.schedule.id).status_value is ScheduleStatus.PUBLISHED

    # 发布结果可追溯（契约 §6 publish_results）
    record = chain.gateway.store.get(chain.post.unified_post_id)
    assert record is not None
    assert record.status is PublishJobStatus.PUBLISHED
    assert record.platform_post_id
    assert record.post_url and record.post_url.startswith("https://")
    assert record.attempts == 1


def test_every_hop_keeps_the_same_traceable_ids(chain):
    """FR-3：同一 ``source_id`` 可追溯全部 variant；三个域对同一条内容用同一组 ID。"""
    job = chain.dispatcher.jobs.get(chain.plan.job_id)

    assert chain.source.id == chain.post.source_id
    assert chain.derived.variant_id == chain.post.variant_id == chain.schedule.variant_id
    assert chain.post.unified_post_id == job.unified_post_id
    assert chain.derived_post.variant_id == chain.post.variant_id
    assert chain.derived_post.source_id == chain.post.source_id
    assert chain.post.account_id == chain.schedule.account_id == ACCOUNT_ID
    assert chain.post.platform == "linkedin" == job.platform
    assert chain.source.brief_id == chain.brief_id

    # 派生结果回写了 variants.fields（契约 §6），且字段名与契约一致
    stored = chain.content.store.get_variant(chain.derived.variant_id)
    assert set(stored.fields) == {"caption", "title", "hashtags", "content_type", "media_count"}
    assert stored.platform.value == "linkedin"


def test_queue_payload_carries_ids_only(chain):
    """契约 §5：队列只传 ID，不传业务对象（消息体版本漂移与重试幂等都依赖这条）。"""
    submissions = chain.queue.submissions_for(TASK_PUBLISH_DISPATCH)
    assert len(submissions) == 1
    submission = submissions[0]
    assert submission.task_name == TASK_PUBLISH_DISPATCH
    assert list(submission.args) == [chain.plan.job_id, chain.post.unified_post_id]
    for arg in submission.args:
        assert isinstance(arg, str)
        assert arg.startswith(("job_", "up_"))
    assert submission.kwargs == {}

    # 延迟投递用 eta（契约 §5 禁止 cron 硬排）
    assert submission.eta == chain.schedule.scheduled_at
    assert submission.countdown is None


def test_pending_finalize_schedules_the_polling_task(chain):
    """受理后必须自排一次 ``pulse.publish.finalize`` 轮询，间隔来自 dispatcher 配置。"""
    chain.run_task(TASK_PUBLISH_DISPATCH, chain.plan.job_id, chain.post.unified_post_id)
    polls = chain.queue.submissions_for(TASK_PUBLISH_FINALIZE)
    assert len(polls) == 1
    assert polls[0].args == (chain.plan.job_id,)
    assert polls[0].countdown == DEFAULT_FINALIZE_POLL_S


def test_schedule_enqueue_task_accepts_the_schedule_id_message(chain, clock):
    """`pulse.schedule.enqueue {schedule_id}` 载荷形态（契约 §5 的第一跳）。"""
    # 上一条投递已占用该账号的发布冷却（默认 1800s），推进时钟越过冷却再投第二条
    clock.advance(minutes=31)
    again = chain.dispatcher.create_schedule(
        variant_id=chain.derived.variant_id,
        account_id=ACCOUNT_ID,
        scheduled_at=chain.schedule.scheduled_at,
    )
    result = chain.run_task(TASK_SCHEDULE_ENQUEUE, again.id)
    assert result["task"] == TASK_SCHEDULE_ENQUEUE
    assert result["schedule_id"] == again.id
    assert result["status"] == PublishJobStatus.QUEUED.value
    # 第二条投递也必须是"只含 ID"的消息
    submissions = chain.queue.submissions_for(TASK_PUBLISH_DISPATCH)
    assert len(submissions) == 2
    assert all(isinstance(arg, str) and arg.startswith(("job_", "up_")) for arg in submissions[1].args)


def test_closed_loop_is_offline_by_construction(chain):
    """离线要求：链路里只有 Fake Adapter，没有任何真实平台 Adapter 被注册。"""
    assert isinstance(chain.gateway.adapter_for("linkedin"), FakeAdapter)
    assert chain.compliance.checked == [chain.derived.variant_id]
    # 审批替身留下了契约 §6 approvals 表形状的留痕（actor / action / diff）
    assert [row["action"] for row in chain.approvals.rows] == ["submit", "approve"]
    assert all(row["actor"] for row in chain.approvals.rows)


def test_compliance_block_stops_the_chain_before_the_platform(
    content, binding, dispatcher, queue, identity, clock
):
    """合规硬拦截（契约 §2 / §3.5）：blocked=True 时网关拒绝发布，且**不触达平台**。"""
    blocked = build_chain(
        content=content,
        binding=binding,
        dispatcher=dispatcher,
        queue=queue,
        identity=identity,
        clock=clock,
        blocked_compliance=True,
    )
    dispatched = blocked.run_task(
        TASK_PUBLISH_DISPATCH, blocked.plan.job_id, blocked.post.unified_post_id
    )

    # 网关拒绝发布 + 不触达平台；任务进 rejected 终态、排期进 failed
    assert dispatched["status"] == PublishJobStatus.REJECTED.value
    assert "compliance.blocked" in (dispatched["alert"] or "")
    assert blocked.adapter.publish_calls_count == 0
    assert blocked.dispatcher.schedules.get(blocked.schedule.id).status_value is ScheduleStatus.FAILED
    assert not blocked.queue.submissions_for(TASK_PUBLISH_FINALIZE)


def test_sync_platform_published_result_converges(
    content, binding, dispatcher, queue, identity, clock
):
    """同步平台（Adapter 直接返回 published）也必须收敛到终态。

    2026-09-22 修 F-1：原先会抛 ``InvalidTransition: dispatching → published``
    （契约 §3.3 没有这条边）。现在 ``apply_publish_result`` 在目标为 published
    且当前为 dispatching 时先落一次 ``publishing`` 再落 ``published``，
    排期侧同理（``scheduled`` 先经 ``publishing``），契约不变、链路不再断。
    """
    sync = build_chain(
        content=content,
        binding=binding,
        dispatcher=dispatcher,
        queue=queue,
        identity=identity,
        clock=clock,
        behaviour=FakeBehaviour.PUBLISHED,
    )

    dispatched = sync.run_task(TASK_PUBLISH_DISPATCH, sync.plan.job_id, sync.post.unified_post_id)

    job = sync.dispatcher.jobs.get(sync.plan.job_id)
    assert dispatched["status"] == PublishJobStatus.PUBLISHED.value
    assert job.status_value is PublishJobStatus.PUBLISHED
    assert sync.dispatcher.schedules.get(sync.schedule.id).status_value is ScheduleStatus.PUBLISHED


def test_a1_and_a3_agree_on_the_idempotency_key(chain):
    """A1 派生出的幂等键必须原样出现在 publish_jobs 与投递消息里。

    2026-09-22 修 F-2：``enqueue_schedule`` 新增可选 ``unified_post_id``，
    编排层把 A1 派生的键透传进来；不传才由 A3 兜底 mint 一个新的。
    """
    job = chain.dispatcher.jobs.get(chain.plan.job_id)
    assert chain.derived_post.unified_post_id == job.unified_post_id
    assert chain.queue.submissions_for(TASK_PUBLISH_DISPATCH)[0].args[1] == (
        chain.derived_post.unified_post_id
    )
