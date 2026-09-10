"""网关幂等、合规拦截与错误分级（派工单 §3 W1-A2-6/7）。"""

from __future__ import annotations

import pytest

from pulse.services.publish.adapters.fake import FakeAdapter, FakeBehaviour
from pulse.services.publish.errors import RetryPolicy
from pulse.services.publish.gateway import AdapterNotRegistered, PublishGateway
from pulse.services.publish.store import InMemoryPublishStore
from pulse.shared.enums import ErrorClass, PublishJobStatus, Platform


async def test_duplicate_dispatch_publishes_once(make_post, credential) -> None:
    """**幂等第一层**：重复投递只签发一次，publish_results 只落一条。"""

    adapter = FakeAdapter(behaviour=FakeBehaviour.PUBLISHED)
    store = InMemoryPublishStore()
    gateway = PublishGateway([adapter], store=store)
    post = make_post()

    first = await gateway.dispatch(post, credential)
    second = await gateway.dispatch(post, credential)

    assert first.status is PublishJobStatus.PUBLISHED
    assert second.duplicate is True
    assert adapter.publish_calls_count == 1
    assert store.result_writes(post.unified_post_id) == 1


async def test_adapter_find_existing_fallback(make_post, credential) -> None:
    """**幂等第二层**：本地无记录但平台上已有 → 不重复发布，转回执收敛。"""

    post = make_post()
    adapter = FakeAdapter()
    adapter.seed_existing(post.unified_post_id, "li_existing_1")
    gateway = PublishGateway([adapter])

    outcome = await gateway.dispatch(post, credential)

    assert outcome.duplicate is True
    assert outcome.status is PublishJobStatus.PENDING_FINALIZE
    assert outcome.needs_polling is True
    assert adapter.publish_calls_count == 0
    assert outcome.alert and "未重复发布" in outcome.alert


async def test_compliance_blocked_is_refused_without_touching_platform(
    make_post, credential
) -> None:
    """契约 §2：`compliance.blocked=True` 时网关必须拒绝发布。"""

    from pulse.shared.models import ComplianceInfo

    adapter = FakeAdapter()
    gateway = PublishGateway([adapter])
    post = make_post(compliance=ComplianceInfo(blocked=True, findings_ref=(7,)))

    outcome = await gateway.dispatch(post, credential)

    assert outcome.status is PublishJobStatus.REJECTED
    assert outcome.result.ok is False
    assert adapter.publish_calls_count == 0
    assert outcome.alert and "合规硬拦截" in outcome.alert


async def test_contract_invalid_payload_is_not_retried(make_post, credential) -> None:
    """契约校验失败 = 代码缺陷，转 failed 且不重试。"""

    adapter = FakeAdapter()
    gateway = PublishGateway([adapter])
    bad = make_post(Platform.YOUTUBE)  # 缺 title 且没有视频素材
    bad = make_post(Platform.YOUTUBE, title=None)

    outcome = await gateway.dispatch(bad, credential)

    assert outcome.status is PublishJobStatus.FAILED
    assert outcome.result.error_class == ErrorClass.VALIDATION_ERROR.value
    assert adapter.publish_calls_count == 0


async def test_adapter_validate_errors_stop_dispatch(make_post, credential) -> None:
    adapter = FakeAdapter(platform="reddit", validate_errors=["options.subreddit 缺失"])
    gateway = PublishGateway([adapter])

    outcome = await gateway.dispatch(make_post(Platform.REDDIT), credential)

    assert outcome.status is PublishJobStatus.FAILED
    assert outcome.result.error_class == ErrorClass.VALIDATION_ERROR.value
    assert adapter.publish_calls_count == 0
    assert "subreddit" in (outcome.result.error_message or "")


@pytest.mark.parametrize(
    "behaviour,expected_status,retryable",
    [
        (FakeBehaviour.RATE_LIMITED, PublishJobStatus.RETRYING, True),
        (FakeBehaviour.TRANSIENT, PublishJobStatus.RETRYING, True),
        (FakeBehaviour.AUTH_EXPIRED, PublishJobStatus.RETRYING, True),
        (FakeBehaviour.POLICY_REJECTED, PublishJobStatus.REJECTED, False),
        (FakeBehaviour.VALIDATION_ERROR, PublishJobStatus.FAILED, False),
    ],
)
async def test_error_class_maps_to_job_status(
    make_post, credential, behaviour, expected_status, retryable
) -> None:
    adapter = FakeAdapter(behaviour=behaviour)
    gateway = PublishGateway([adapter])

    outcome = await gateway.dispatch(make_post(), credential)

    assert outcome.status is expected_status
    if retryable:
        assert outcome.next_retry_at is not None
    else:
        assert outcome.next_retry_at is None
        assert outcome.alert


async def test_exhausted_retries_become_failed(make_post, credential) -> None:
    adapter = FakeAdapter(behaviour=FakeBehaviour.RATE_LIMITED)
    gateway = PublishGateway([adapter], retry_policy=RetryPolicy(max_attempts=1))

    outcome = await gateway.dispatch(make_post(), credential)

    assert outcome.status is PublishJobStatus.FAILED
    assert outcome.alert and "最大重试次数" in outcome.alert


async def test_unregistered_platform_raises(make_post, credential) -> None:
    """平台没接线 → 抛异常（部署缺陷），不要静默当成内容失败。"""

    gateway = PublishGateway([FakeAdapter(platform="linkedin")])
    with pytest.raises(AdapterNotRegistered):
        await gateway.dispatch(make_post(Platform.REDDIT), credential)


async def test_media_processing_goes_to_pending_finalize(make_post, credential) -> None:
    """平台异步处理中 → 轮询，而不是 failed。"""

    adapter = FakeAdapter(behaviour=FakeBehaviour.MEDIA_PROCESSING)
    gateway = PublishGateway([adapter])

    outcome = await gateway.dispatch(make_post(), credential)

    assert outcome.status is PublishJobStatus.PENDING_FINALIZE
    assert outcome.needs_polling is True
    assert outcome.finalize_deadline is not None
