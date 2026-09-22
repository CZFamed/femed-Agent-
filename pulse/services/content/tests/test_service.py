"""端到端：brief → 素材 → 文案（桩）→ variant，全链路不调真实模型。"""

from __future__ import annotations

import pytest

from pulse.services.content.errors import (
    ContentError,
    ContentNotFoundError,
    TokenBudgetExceeded,
)
from pulse.services.content.generator import (
    TASK_GENERATE_VARIANT,
    ContentService,
    StubCopywriter,
)
from pulse.services.content.prompts import prompt_template
from pulse.services.content.variants import AccountBinding
from pulse.shared.enums import Platform, VariantStatus


def _payload(**overrides) -> dict:
    base = {
        "topic": "消失模铸造 + 粗加工一体化",
        "target_audience": "印度机床整机厂采购",
        "platforms": ["linkedin"],
    }
    base.update(overrides)
    return base


def _accounts() -> dict[str, AccountBinding]:
    return {
        "linkedin": AccountBinding(
            account_id="acct_li_1",
            platform=Platform.LINKEDIN,
            options={"author_urn": "urn:li:organization:12345", "linkedin_visibility": "PUBLIC"},
        )
    }


def _service(**kwargs) -> ContentService:
    return ContentService(accounts=_accounts(), **kwargs)


def test_submit_brief_creates_content_record():
    service = _service()

    content = service.submit_brief(_payload())

    assert content.id.startswith("src_")
    assert content.brief_id.startswith("b_")
    assert content.title == "消失模铸造 + 粗加工一体化"
    assert "印度机床整机厂采购" in content.body_seed


def test_submit_brief_is_idempotent_per_brief_id():
    service = _service()
    payload = _payload(brief_id="b_fixed")

    first = service.submit_brief(payload)
    second = service.submit_brief(payload)

    assert first == second
    assert len(service.store.list_contents()) == 1


def test_generate_variant_without_media_is_text_only():
    service = _service()
    service.submit_brief(_payload())
    brief_id = service.store.list_contents()[0].brief_id

    derived = service.generate_variant(brief_id, "linkedin")

    assert derived.post.content_type.value == "text"
    assert derived.post.media == ()
    assert derived.post.validate() == []
    assert derived.post.caption.text.startswith("Most machine tool builders")


def test_generate_variant_uses_selected_media_and_reports_gaps(asset_pool, policy):
    service = _service(assets=asset_pool, policy=policy, media_size_of=lambda _n: (1200, 1500))
    service.submit_brief(_payload())
    brief_id = service.store.list_contents()[0].brief_id

    derived = service.generate_variant(brief_id, "linkedin")

    assert derived.post.content_type.value == "image"
    assert len(derived.post.media) >= 1
    assert derived.post.validate() == []
    # 素材文件名应当出现在文案里（固定桩引用的是真实选中的素材）
    assert "IMG_0001.jpg" in derived.post.caption.text or "TODO(need-real-data)" in derived.post.caption.text


def test_youtube_without_slot_profile_degrades_to_manual_media(asset_pool, policy):
    """YouTube 还没有位次口径：不猜画面，由调用方人工提供视频素材。"""
    from pulse.shared.models import MediaItem

    service = ContentService(
        accounts={
            "youtube": AccountBinding(account_id="acct_yt_1", platform=Platform.YOUTUBE)
        },
        assets=asset_pool,
        policy=policy,
        media_size_of=lambda _n: (1200, 1500),
    )
    service.submit_brief(_payload(platforms=["youtube"]))
    brief_id = service.store.list_contents()[0].brief_id

    derived = service.generate_variant(
        brief_id,
        "youtube",
        media=[
            MediaItem(
                kind="video",
                url="s3://pulse-media/clip.mp4",
                mime="video/mp4",
                license_status="owned",
                duration_s=42.0,
            )
        ],
    )

    assert derived.post.title
    assert derived.post.content_type.value == "video"
    assert derived.post.validate() == []


def test_generated_variant_is_persisted_with_fields_and_media(asset_pool, policy):
    service = _service(assets=asset_pool, policy=policy, media_size_of=lambda _n: (1200, 1500))
    service.submit_brief(_payload())
    brief_id = service.store.list_contents()[0].brief_id

    derived = service.generate_variant(brief_id, "linkedin")

    record = service.store.get_variant(derived.variant_id)
    assert record.status is VariantStatus.DRAFT
    assert record.fields["caption"]["text"]
    assert record.fields["hashtags"]
    assert record.fields["content_type"] == "image"
    assert service.store.media_for_variant(derived.variant_id)
    assert service.store.variants_for_source(derived.source_id) == (record,)


def test_token_budget_circuit_breaker_stops_generation():
    """brief 预算不够时，生成直接熔断，不产出半截文案。"""
    service = _service()
    service.submit_brief(_payload(token_budget=1_000))  # 桩要 2,400 token
    brief_id = service.store.list_contents()[0].brief_id

    with pytest.raises(TokenBudgetExceeded):
        service.generate_variant(brief_id, "linkedin")

    assert service.store.list_variants() == ()


def test_missing_account_binding_blocks_generation():
    service = ContentService()  # 没有账号绑定
    service.submit_brief(_payload())
    brief_id = service.store.list_contents()[0].brief_id

    with pytest.raises(ContentError) as exc:
        service.generate_variant(brief_id, "linkedin")

    assert "账号绑定" in str(exc.value)


def test_queue_handler_accepts_id_only_payload():
    service = _service()
    service.submit_brief(_payload())
    brief_id = service.store.list_contents()[0].brief_id

    result = service.handle_generate_variant({"brief_id": brief_id, "platform": "linkedin"})

    assert TASK_GENERATE_VARIANT in result
    payload = result[TASK_GENERATE_VARIANT]
    assert payload["platform"] == "linkedin"
    assert payload["post"]["variant_id"] == payload["variant_id"]


def test_queue_handler_rejects_incomplete_payload():
    service = _service()

    with pytest.raises(ContentError) as exc:
        service.handle_generate_variant({"brief_id": "b_x"})

    assert "brief_id, platform" in str(exc.value)


def test_unknown_brief_raises():
    service = _service()

    with pytest.raises(ContentNotFoundError):
        service.generate_variant("b_missing", "linkedin")


def test_self_review_only_runs_one_round_and_charges_budget():
    service = _service()
    service.submit_brief(_payload())
    brief_id = service.store.list_contents()[0].brief_id
    service.generate_variant(brief_id, "linkedin")

    review = service.self_review(brief_id)

    assert review["rounds"] == 1
    assert review["model"] == "light-reviewer"
    assert service.router_for(brief_id).budget.used == 2_400 + 400


def test_stub_copywriter_respects_platform_hashtag_bounds():
    service = _service()
    brief = service.submit_brief(_payload()).brief_id
    template = prompt_template("linkedin")

    derived = service.generate_variant(brief, "linkedin")

    assert template.hashtag_min <= len(derived.post.hashtags) <= template.hashtag_max
    assert all(tag.startswith("#") and " " not in tag for tag in derived.post.hashtags)


def test_no_real_model_is_used_by_default():
    service = _service()

    assert isinstance(service.copywriter, StubCopywriter)
