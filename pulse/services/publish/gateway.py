"""发布网关：把 `UnifiedPost` 可靠送到平台，并保证重复投递不重复发布。

幂等是**两层**的（契约 §6「幂等保证」）：

* 第一层：`publish_jobs.unified_post_id` 唯一索引语义（本模块 `store.claim()`）
* 第二层：Adapter 的 `find_existing()` 兜底

异步回执：平台返回"已受理"只能进 `pending_finalize`（契约 §3.3），
由 `finalize()` 收敛；超时**只告警**，既不自动判成功也不自动判失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from pulse.services.publish.base import Credential, PlatformAdapter
from pulse.services.publish.errors import ErrorClass, RetryPolicy
from pulse.services.publish.state import (
    DEFAULT_FINALIZE_TIMEOUT_S,
    PendingWindow,
    job_status_for,
)
from pulse.services.publish.store import (
    InMemoryPublishStore,
    PublishRecord,
    PublishStore,
)
from pulse.shared.enums import PublishJobStatus
from pulse.shared.ids import new_job_id
from pulse.shared.models import PublishResult, UnifiedPost


class AdapterNotRegistered(LookupError):
    """平台没有注册 Adapter（部署接线问题，不是内容问题）。"""


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """一次 dispatch / finalize 的结果与后续动作。"""

    job_id: str
    unified_post_id: str
    status: PublishJobStatus
    result: PublishResult
    attempts: int = 1
    next_retry_at: datetime | None = None
    finalize_deadline: datetime | None = None
    duplicate: bool = False
    alert: str | None = None

    @property
    def needs_polling(self) -> bool:
        """是否还需要 `pulse.publish.finalize` 继续收敛。"""

        return self.status is PublishJobStatus.PENDING_FINALIZE


class PublishGateway:
    """发布网关（A2 的唯一入口）。

    Args:
        adapters: 已注册的平台 Adapter。
        store: 落库实现；缺省用内存实现（单测用）。
        finalize_timeout_s: `pending_finalize` 超时，默认 30 分钟（契约 §3.3）。
        retry_policy: 可重试错误的退避策略（契约 §3.4）。
        now: 时间源，便于测试注入固定时钟。
    """

    def __init__(
        self,
        adapters: Iterable[PlatformAdapter] = (),
        *,
        store: PublishStore | None = None,
        finalize_timeout_s: int = DEFAULT_FINALIZE_TIMEOUT_S,
        retry_policy: RetryPolicy | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapters: dict[str, PlatformAdapter] = {}
        for adapter in adapters:
            self.register(adapter)
        self._store: PublishStore = store or InMemoryPublishStore()
        self._timeout_s = finalize_timeout_s
        self._retry = retry_policy or RetryPolicy()
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(timezone.utc))

    # -- 注册与查询 --------------------------------------------------------

    def register(self, adapter: PlatformAdapter) -> None:
        """注册/覆盖某平台的 Adapter。"""

        self._adapters[str(adapter.platform)] = adapter

    def adapter_for(self, platform: str) -> PlatformAdapter:
        """取平台 Adapter；未注册则抛 `AdapterNotRegistered`。"""

        try:
            return self._adapters[str(platform)]
        except KeyError as exc:  # pragma: no cover - 消息内容由测试断言
            raise AdapterNotRegistered(f"平台未注册 Adapter：{platform}") from exc

    @property
    def store(self) -> PublishStore:
        """当前存储实现（测试与 A3 接线用）。"""

        return self._store

    # -- 发布 --------------------------------------------------------------

    async def dispatch(
        self,
        post: UnifiedPost,
        credential: Credential,
        *,
        job_id: str | None = None,
    ) -> DispatchOutcome:
        """入口消息 `pulse.publish.dispatch {job_id, unified_post_id}` 的执行体。

        订单不依赖消息体里的业务对象——`job_id` 只用于落库与追踪，
        幂等键一律取 `post.unified_post_id`。
        """

        now = self._now()
        record = PublishRecord(
            unified_post_id=post.unified_post_id,
            job_id=job_id or new_job_id(),
            platform=str(post.platform),
            attempts=1,
        )

        # 第一层幂等：唯一索引语义占位。抢不到说明这条已投递过。
        if not self._store.claim(record):
            return self._duplicate_outcome(post.unified_post_id)

        # 合规硬拦截：网关必须拒绝发布（契约 §2），且不触达平台。
        if post.compliance.blocked:
            return self._settle(
                record,
                status=PublishJobStatus.REJECTED,
                result=PublishResult(
                    ok=False,
                    status="rejected",
                    error_message="compliance.blocked=True，发布网关拒绝发布",
                ),
                alert="合规硬拦截：compliance.blocked=True，需管理员权限豁免后重新生成 UnifiedPost",
            )

        # payload 合法性：契约校验 + 平台校验，两者都不允许发请求。
        contract_errors = post.validate()
        if contract_errors:
            return self._settle(
                record,
                status=PublishJobStatus.FAILED,
                result=PublishResult(
                    ok=False,
                    status="failed",
                    error_class=ErrorClass.VALIDATION_ERROR.value,
                    error_message="；".join(contract_errors),
                ),
                alert="payload 未通过契约校验，属代码缺陷，需修复后重投（不重试）",
            )

        adapter = self.adapter_for(str(post.platform))  # 未注册 → 部署接线错误
        adapter_errors = await adapter.validate(post)
        if adapter_errors:
            return self._settle(
                record,
                status=PublishJobStatus.FAILED,
                result=PublishResult(
                    ok=False,
                    status="failed",
                    error_class=ErrorClass.VALIDATION_ERROR.value,
                    error_message="；".join(adapter_errors),
                ),
                alert="payload 未通过平台校验，需修复后重投（不重试）",
            )

        # 第二层幂等：平台侧兜底。命中则不再发布，但仍要收敛回执。
        existing_id = await adapter.find_existing(post)
        if existing_id:
            deadline = now + timedelta(seconds=self._timeout_s)
            result = PublishResult(
                ok=True,
                status="pending_finalize",
                platform_post_id=existing_id,
            )
            return self._settle(
                record,
                status=PublishJobStatus.PENDING_FINALIZE,
                result=result,
                alert="平台已存在该 unified_post_id，未重复发布，待回执收敛核实",
                duplicate=True,
                platform_post_id=existing_id,
                finalize_deadline=deadline,
            )

        result = await adapter.publish(post, credential)
        status = job_status_for(result)

        if status is PublishJobStatus.PENDING_FINALIZE:
            record = self._store.save(
                record.evolve(
                    status=status,
                    result=result,
                    platform_post_id=result.platform_post_id,
                    post_url=result.post_url,
                    finalize_deadline=now + timedelta(seconds=self._timeout_s),
                )
            )
            return self._outcome(record)

        if status is PublishJobStatus.RETRYING:
            plan = self._retry.plan(result.error_class, attempt=record.attempts)
            next_retry = (
                now + timedelta(seconds=plan.delay_s)
                if plan.retry and plan.delay_s is not None
                else None
            )
            record = self._store.save(
                record.evolve(
                    status=PublishJobStatus.RETRYING if plan.retry else PublishJobStatus.FAILED,
                    result=result,
                    error_class=result.error_class,
                    error_message=result.error_message,
                    next_retry_at=next_retry,
                )
            )
            return self._outcome(record, alert=None if plan.retry else plan.reason)

        return self._settle(
            record,
            status=status,
            result=result,
            alert=self._alert_for(status, result),
        )

    # -- 回执收敛 ----------------------------------------------------------

    async def finalize(
        self,
        unified_post_id: str,
        credential: Credential,
        *,
        job_id: str | None = None,
        force: bool = False,
    ) -> DispatchOutcome:
        """入口消息 `pulse.publish.finalize {job_id}` 的执行体。

        Args:
            unified_post_id: 幂等键（生产由 A3 从 `publish_jobs` 反查）。
            credential: 凭据句柄。
            job_id: 仅用于追踪。
            force: 超时后仍允许人工触发一次核对（默认拒绝继续自动轮询）。
        """

        record = self._store.get(unified_post_id)
        if record is None:
            return DispatchOutcome(
                job_id=job_id or "",
                unified_post_id=unified_post_id,
                status=PublishJobStatus.FAILED,
                result=PublishResult(
                    ok=False,
                    status="failed",
                    error_message=f"无本地发布记录：{unified_post_id}",
                ),
                alert="收到 finalize 但没有对应的 dispatch 记录，请核对投递链路",
            )

        if record.status is not PublishJobStatus.PENDING_FINALIZE:
            # 终态或尚未受理：幂等返回，不再调平台
            return self._outcome(record, duplicate=True)

        now = self._now()
        window = PendingWindow(
            deadline=record.finalize_deadline or now,
            timeout_s=self._timeout_s,
        )
        if window.expired(now) and not force:
            # 超时只告警：**不**自动置 published，也**不**自动判 failed
            return self._outcome(record, alert=window.alert_message(now))

        if not record.platform_post_id:
            return self._outcome(
                record,
                alert="pending_finalize 缺少 platform_post_id，无法轮询收敛，需人工核对",
            )

        adapter = self.adapter_for(record.platform)
        result = await adapter.poll_finalize(record.platform_post_id, credential)
        status = job_status_for(result)

        if status is PublishJobStatus.PENDING_FINALIZE:
            # 不重置 deadline：否则 30 分钟上限可以被无限续期
            record = self._store.save(
                record.evolve(
                    result=result,
                    platform_post_id=result.platform_post_id or record.platform_post_id,
                    post_url=result.post_url or record.post_url,
                )
            )
            alert = window.alert_message(now) if window.expired(now) else None
            return self._outcome(record, alert=alert)

        record = self._store.save(
            record.evolve(
                status=status,
                result=result,
                platform_post_id=result.platform_post_id or record.platform_post_id,
                post_url=result.post_url or record.post_url,
                error_class=result.error_class,
                error_message=result.error_message,
            )
        )
        return self._outcome(record, alert=self._alert_for(status, result))

    async def fetch_metrics(
        self, unified_post_id: str, credential: Credential
    ) -> dict:
        """FR-8 数据回捞（P1）。无记录或未拿到平台 ID 时返回 {}。"""

        record = self._store.get(unified_post_id)
        if record is None or not record.platform_post_id:
            return {}
        adapter = self.adapter_for(record.platform)
        return await adapter.fetch_metrics(record.platform_post_id, credential)

    # -- 内部工具 ----------------------------------------------------------

    @staticmethod
    def _alert_for(status: PublishJobStatus, result: PublishResult) -> str | None:
        if status is PublishJobStatus.REJECTED:
            return f"平台政策拒绝，终态不重试：{result.error_message or '（无消息）'}"
        if status is PublishJobStatus.FAILED:
            return f"发布失败且不可重试，需人工介入：{result.error_message or '（无消息）'}"
        return None

    def _outcome(
        self, record: PublishRecord, *, duplicate: bool = False, alert: str | None = None
    ) -> DispatchOutcome:
        return DispatchOutcome(
            job_id=record.job_id,
            unified_post_id=record.unified_post_id,
            status=record.status,
            result=record.result
            or PublishResult(ok=False, status="failed", error_message="无结果记录"),
            attempts=record.attempts,
            next_retry_at=record.next_retry_at,
            finalize_deadline=record.finalize_deadline,
            duplicate=duplicate,
            alert=alert,
        )

    def _duplicate_outcome(self, unified_post_id: str) -> DispatchOutcome:
        record = self._store.get(unified_post_id)
        if record is None:  # pragma: no cover - claim 失败必然有记录
            raise AdapterNotRegistered(f"claim 失败但记录缺失：{unified_post_id}")
        return self._outcome(record, duplicate=True)

    def _settle(
        self,
        record: PublishRecord,
        *,
        status: PublishJobStatus,
        result: PublishResult,
        alert: str | None = None,
        duplicate: bool = False,
        platform_post_id: str | None = None,
        finalize_deadline: datetime | None = None,
    ) -> DispatchOutcome:
        """落库并返回结果。用于除"已受理待轮询 / 可重试"之外的全部分支。"""

        saved = self._store.save(
            record.evolve(
                status=status,
                result=result,
                platform_post_id=platform_post_id or result.platform_post_id,
                post_url=result.post_url,
                error_class=result.error_class,
                error_message=result.error_message,
                finalize_deadline=finalize_deadline,
            )
        )
        return self._outcome(saved, duplicate=duplicate, alert=alert)
