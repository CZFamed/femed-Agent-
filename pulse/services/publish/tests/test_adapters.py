"""LinkedIn / YouTube / Fake 三个 Adapter 的行为测试（派工单 §3 W1-A2-3/4/5）。"""

from __future__ import annotations

import pytest

from pulse.services.publish.adapters.fake import FakeAdapter, FakeBehaviour
from pulse.services.publish.adapters.linkedin import (
    LINKEDIN_API_POSTS,
    MAX_COMMENTARY_LEN,
    LinkedInAdapter,
)
from pulse.services.publish.adapters.youtube import YouTubeAdapter
from pulse.services.publish.transport import HttpResponse, RecordingTransport
from pulse.shared.enums import ErrorClass, Platform


async def _fake_reader(item) -> bytes:  # noqa: ANN001 - 测试替身
    return b"\x00" * 16


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------


async def test_linkedin_validate_accepts_valid_post(make_post) -> None:
    adapter = LinkedInAdapter()
    assert await adapter.validate(make_post()) == []


@pytest.mark.parametrize(
    "options,needle",
    [
        ({"linkedin_visibility": "PUBLIC"}, "author_urn"),
        ({"author_urn": "urn:li:organization:1"}, "linkedin_visibility"),
        ({"author_urn": "company:1", "linkedin_visibility": "PUBLIC"}, "urn:li:"),
        ({"author_urn": "urn:li:organization:1", "linkedin_visibility": "PRIVATE"}, "非法"),
    ],
)
async def test_linkedin_validate_rejects_bad_options(make_post, options, needle) -> None:
    errors = await LinkedInAdapter().validate(make_post(options=options))
    assert any(needle in message for message in errors), errors


async def test_linkedin_validate_rejects_overlong_text(make_post) -> None:
    post = make_post(caption=make_post().caption.__class__(text="字" * (MAX_COMMENTARY_LEN + 1)))
    errors = await LinkedInAdapter().validate(post)
    assert any("上限" in message for message in errors)


async def test_linkedin_publish_success_is_synchronous(make_post, credential) -> None:
    transport = RecordingTransport(
        [HttpResponse(status_code=201, headers={"X-Restli-Id": "urn:li:share:987"})]
    )
    result = await LinkedInAdapter(transport=transport).publish(make_post(), credential)

    assert result.ok is True
    assert result.status == "published"  # 同步平台
    assert result.platform_post_id == "urn:li:share:987"
    assert result.post_url and result.post_url.endswith("urn:li:share:987")

    sent = transport.requests[0]
    assert sent.url == LINKEDIN_API_POSTS
    assert sent.headers["X-Restli-Idempotency-Key"]  # 幂等键必须透传
    assert sent.headers["Authorization"].startswith("Bearer ")
    assert "#casting" in str(sent.json["commentary"])  # hashtags 随正文下发


@pytest.mark.parametrize(
    "status_code,body,expected",
    [
        (429, {}, ErrorClass.RATE_LIMITED),
        (401, {}, ErrorClass.AUTH_EXPIRED),
        (403, {"serviceErrorCode": "QUOTA_EXCEEDED"}, ErrorClass.RATE_LIMITED),
        (403, {"serviceErrorCode": "POLICY_VIOLATION"}, ErrorClass.POLICY_REJECTED),
        (500, {}, ErrorClass.TRANSIENT),
        (400, {"message": "bad payload"}, ErrorClass.VALIDATION_ERROR),
    ],
)
async def test_linkedin_error_classification(
    make_post, credential, status_code, body, expected
) -> None:
    transport = RecordingTransport([HttpResponse(status_code=status_code, body=body)])
    result = await LinkedInAdapter(transport=transport).publish(make_post(), credential)

    assert result.ok is False
    assert result.error_class == expected.value
    assert "token" not in str(result.raw).lower()


async def test_linkedin_policy_error_maps_to_rejected(make_post, credential) -> None:
    transport = RecordingTransport(
        [HttpResponse(status_code=403, body={"serviceErrorCode": "POLICY_VIOLATION"})]
    )
    result = await LinkedInAdapter(transport=transport).publish(make_post(), credential)
    assert result.status == "rejected"


async def test_linkedin_find_existing_and_poll_finalize(make_post, credential) -> None:
    post = make_post()
    adapter = LinkedInAdapter(published={post.unified_post_id: "urn:li:share:1"})
    assert await adapter.find_existing(post) == "urn:li:share:1"

    finalized = await adapter.poll_finalize("urn:li:share:1", credential)
    assert finalized.ok is True and finalized.status == "published"
    assert await adapter.fetch_metrics("urn:li:share:1", credential) == {}


async def test_linkedin_without_transport_refuses(make_post, credential) -> None:
    with pytest.raises(RuntimeError, match="拒绝发起真实请求"):
        await LinkedInAdapter().publish(make_post(), credential)


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------


async def test_youtube_validate_accepts_valid_post(make_post) -> None:
    post = make_post(Platform.YOUTUBE)
    assert await YouTubeAdapter().validate(post) == []


@pytest.mark.parametrize(
    "overrides,needle",
    [
        ({"title": None}, "title"),
        ({"options": {"category_id": "28", "made_for_kids": False}}, "privacy_status"),
        ({"options": {"privacy_status": "secret", "category_id": "28", "made_for_kids": False}}, "非法"),
        ({"options": {"privacy_status": "public", "made_for_kids": False}}, "category_id"),
        ({"options": {"privacy_status": "public", "category_id": "28"}}, "made_for_kids"),
        ({"options": {"privacy_status": "public", "category_id": "28", "made_for_kids": "no"}}, "布尔值"),
    ],
)
async def test_youtube_validate_rejects_bad_options(make_post, overrides, needle) -> None:
    errors = await YouTubeAdapter().validate(make_post(Platform.YOUTUBE, **overrides))
    assert any(needle in message for message in errors), errors


async def test_youtube_requires_video_media(make_post) -> None:
    """契约 §2：YouTube 必须有视频素材（只有图片会被拦）。"""

    post = make_post(Platform.YOUTUBE)
    text_only = make_post(Platform.YOUTUBE, media=(), content_type="video")
    errors = await YouTubeAdapter().validate(text_only)
    assert any("视频素材" in message for message in errors)
    assert await YouTubeAdapter().validate(post) == []


async def test_youtube_publish_never_returns_published(make_post, credential) -> None:
    """**核心约束**：上传成功只能进 pending_finalize（契约 §3.3）。"""

    post = make_post(Platform.YOUTUBE)
    transport = RecordingTransport(
        [
            HttpResponse(status_code=200, headers={"Location": "https://upload.test/session/1"}),
            HttpResponse(status_code=200, body={"id": "vid_123", "status": {"uploadStatus": "uploaded"}}),
        ]
    )
    result = await YouTubeAdapter(transport=transport, read_media=_fake_reader).publish(
        post, credential
    )

    assert result.ok is True
    assert result.status == "pending_finalize"
    assert result.status != "published"
    assert result.platform_post_id == "vid_123"
    assert transport.requests[1].content == b"\x00" * 16  # 真的走了 resumable 上传


async def test_youtube_publish_without_session_uri_fails(make_post, credential) -> None:
    transport = RecordingTransport([HttpResponse(status_code=200)])
    result = await YouTubeAdapter(transport=transport, read_media=_fake_reader).publish(
        make_post(Platform.YOUTUBE), credential
    )
    assert result.ok is False and result.status == "failed"


async def test_youtube_publish_without_media_reader_fails_loudly(make_post, credential) -> None:
    transport = RecordingTransport(
        [HttpResponse(status_code=200, headers={"Location": "https://upload.test/session/1"})]
    )
    result = await YouTubeAdapter(transport=transport).publish(
        make_post(Platform.YOUTUBE), credential
    )
    assert result.ok is False
    assert result.error_class == ErrorClass.VALIDATION_ERROR.value


@pytest.mark.parametrize(
    "upload_status,expected_status,expected_error",
    [
        ("processed", "published", None),
        ("uploaded", "pending_finalize", None),
        ("processing", "pending_finalize", None),
        ("rejected", "rejected", ErrorClass.POLICY_REJECTED.value),
        ("failed", "pending_finalize", ErrorClass.MEDIA_PROCESSING.value),
    ],
)
async def test_youtube_poll_finalize_converges(
    credential, upload_status, expected_status, expected_error
) -> None:
    transport = RecordingTransport(
        [HttpResponse(status_code=200, body={"items": [{"status": {"uploadStatus": upload_status}}]})]
    )
    result = await YouTubeAdapter(transport=transport).poll_finalize("vid_1", credential)
    assert result.status == expected_status
    assert result.error_class == expected_error


async def test_youtube_quota_403_is_rate_limited(credential) -> None:
    """403 + quotaExceeded 是限流，不是政策拒绝——只看状态码必然误判。"""

    body = {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}}
    transport = RecordingTransport([HttpResponse(status_code=403, body=body)])
    result = await YouTubeAdapter(transport=transport).poll_finalize("vid_1", credential)
    assert result.error_class == ErrorClass.RATE_LIMITED.value


# ---------------------------------------------------------------------------
# Fake Adapter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "behaviour,expected_status,expected_error",
    [
        (FakeBehaviour.PUBLISHED, "published", None),
        (FakeBehaviour.PENDING_THEN_PUBLISHED, "pending_finalize", None),
        (FakeBehaviour.POLICY_REJECTED, "rejected", ErrorClass.POLICY_REJECTED.value),
        (FakeBehaviour.RATE_LIMITED, "failed", ErrorClass.RATE_LIMITED.value),
        (FakeBehaviour.AUTH_EXPIRED, "failed", ErrorClass.AUTH_EXPIRED.value),
        (FakeBehaviour.TRANSIENT, "failed", ErrorClass.TRANSIENT.value),
        (FakeBehaviour.MEDIA_PROCESSING, "pending_finalize", ErrorClass.MEDIA_PROCESSING.value),
        (FakeBehaviour.VALIDATION_ERROR, "failed", ErrorClass.VALIDATION_ERROR.value),
    ],
)
async def test_fake_adapter_behaviours(
    make_post, credential, behaviour, expected_status, expected_error
) -> None:
    adapter = FakeAdapter(behaviour=behaviour)
    result = await adapter.publish(make_post(), credential)
    assert result.ok is (expected_error is None)
    assert result.status == expected_status
    assert result.error_class == expected_error


async def test_fake_adapter_records_calls(make_post, credential) -> None:
    adapter = FakeAdapter()
    post = make_post()
    await adapter.publish(post, credential)
    assert adapter.publish_calls == [post.unified_post_id]
    assert await adapter.find_existing(post) is not None  # 发布后本地已有映射


async def test_fake_adapter_seed_existing(make_post) -> None:
    post = make_post()
    adapter = FakeAdapter()
    adapter.seed_existing(post.unified_post_id, "li_999")
    assert await adapter.find_existing(post) == "li_999"
