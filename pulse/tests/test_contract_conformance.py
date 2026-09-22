"""W2-A6-1 · 契约一致性校验（独立复核 + 补盲区）。

与 root 的基线测试（``pulse/shared/tests/test_contract_baseline.py``，21 项）的分工：

* 那份测的是**契约语义**（枚举白名单、必填项、时区偏移……）；
* 这份**不照抄**它的断言，而是把 ``contracts/INTERFACES.md`` 当作**外部真源解析**，
  再拿代码去对：
  1. 枚举取值集合 ← 解析 §2 注释、§3.1–§3.5 的代码块与表格；
  2. 状态机迁移表 ← §3.2 / §3.3 的状态图（**基线未覆盖**）；
  3. 必填 options ← §2.1 表格，且逐字段做**行为验证**（真的会报错才算数）；
  4. DDL 字段名 ← §6 的 ``CREATE TABLE``（**跨域字段一致性**）；
  5. ``to_dict()`` 往返 ← §2 的 jsonc 字段集合（**基线未覆盖**）；
  6. ``SemiAutoBundle`` 四字段 ← §4 的 docstring（**基线未覆盖**）。

解析失败（解析到空集合）会被显式断言拦住：宁可直接红，也不要"解析不到 → 全过"的假绿。
"""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pulse.services.content import ContentRecord, MediaAssetRecord, VariantRecord
from pulse.services.identity import Account, CredentialRecord
from pulse.services.publish import (
    FakeAdapter,
    PublishGateway,
    SemiAutoBundleExporter,
)
from pulse.services.publish.adapters import FakeBehaviour
from pulse.services.publish import state as publish_state
from pulse.services.scheduler import state as scheduler_state
from pulse.services.scheduler.celery_app import ALL_TASK_NAMES
from pulse.services.scheduler.store import PublishJobRecord, ScheduleRecord
from pulse.shared.enums import (
    NON_RETRYABLE_ERRORS,
    RETRYABLE_ERRORS,
    TERMINAL_JOB_STATUSES,
    ComplianceSeverity,
    ErrorClass,
    LicenseStatus,
    MediaKind,
    Platform,
    PublishJobStatus,
    ScheduleStatus,
    VariantStatus,
)
from pulse.shared.ids import (
    new_account_id,
    new_brief_id,
    new_job_id,
    new_schedule_id,
    new_source_id,
    new_unified_post_id,
    new_variant_id,
)
from pulse.shared.models import SemiAutoBundle, UnifiedPost

from pulse.services.content.generator import TASK_GENERATE_VARIANT
from pulse.tests.conftest import CredentialStub, make_media_item, make_post, run_async

#: 契约文件（外部真源，只读）
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "INTERFACES.md"

#: §7 冻结的 11 个 REST 端点（A5 未开工，这里只守护"契约本身没被悄悄改"）
FROZEN_REST_ENDPOINTS = frozenset(
    {
        ("POST", "/api/v1/briefs"),
        ("GET", "/api/v1/contents/{id}/variants"),
        ("PATCH", "/api/v1/variants/{id}/status"),
        ("POST", "/api/v1/variants/{id}/schedule"),
        ("PATCH", "/api/v1/schedules/{id}"),
        ("POST", "/api/v1/schedules/{id}/publish"),
        ("GET", "/api/v1/schedules/{id}/semi-auto"),
        ("GET", "/api/v1/accounts"),
        ("POST", "/api/v1/accounts/{id}/oauth"),
        ("DELETE", "/api/v1/accounts/{id}/credential"),
        ("POST", "/api/v1/compliance/screen"),
    }
)

#: 契约 §6 里**尚无代码实现**的表（A4/A5 未开工），与代码现状必须一致
CONTRACT_TABLES_WITHOUT_IMPLEMENTATION = frozenset(
    {"brand_guides", "approvals", "compliance_findings", "sanctions_screenings"}
)

#: 契约 §6 里**只被部分建模**的表：A2 把 ``publish_results`` 合并进 ``PublishRecord``
#: （见 ``publish/services/store.py`` 的模块说明），但 ``metrics`` / ``fetched_at`` 两列
#: 尚未落库——FR-8 数据回捞是 P1，M1 只保留占位。
CONTRACT_TABLES_PARTIALLY_IMPLEMENTED = {"publish_results": {"metrics", "fetched_at"}}


# --------------------------------------------------------------------------
# 契约解析工具
# --------------------------------------------------------------------------


def _doc() -> str:
    return CONTRACT_PATH.read_text(encoding="utf-8")


def _subsection(fragment: str) -> str:
    """取包含 ``fragment`` 的标题到下一个标题之间的正文。"""
    doc = _doc()
    match = re.search(rf"^#{{2,4}} .*{re.escape(fragment)}.*$", doc, re.M)
    assert match is not None, f"契约里找不到小节：{fragment!r}（契约被改动？）"
    start = match.end()
    following = re.compile(r"^#{2,4} ", re.M).search(doc, start)
    return doc[start : following.start() if following else len(doc)]


def _code_block(text: str) -> str:
    """取该节的第一个 fenced code block。"""
    match = re.search(r"```[a-zA-Z]*\n(.*?)```", text, re.S)
    assert match is not None, "该节没有 fenced code block（解析失败）"
    return match.group(1)


def _table_rows(section: str) -> list[list[str]]:
    """解析 markdown 表格（正确处理单元格里的转义竖线 ``\\|``）。"""
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped)[1:-1]]
        if cells and set("".join(cells)) <= set("-: "):
            continue  # 分隔行
        rows.append(cells)
    return rows


def _identifiers(block: str) -> set[str]:
    """取代码块里的小写标识符（状态值都是这种形态）。"""
    return set(re.findall(r"\b[a-z][a-z_0-9]{2,}\b", block))


def contract_option_requirements() -> dict[str, dict[str, bool]]:
    """§2.1 表 → ``{platform: {field: 是否必填}}``。"""
    requirements: dict[str, dict[str, bool]] = {}
    for cells in _table_rows(_subsection("2.1 `options` 平台字段")):
        if len(cells) < 3 or cells[0] == "平台":
            continue
        platform = cells[0].strip().strip("`")
        field = cells[1].strip().strip("`")
        requirements.setdefault(platform, {})[field] = cells[2].strip() == "是"
    return requirements


def contract_tables() -> dict[str, list[str]]:
    """§6 DDL → ``{table: [column, ...]}``。"""
    section = _subsection("6. 数据模型")
    tables: dict[str, list[str]] = {}
    for match in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", section, re.S):
        table, body = match.group(1), match.group(2)
        columns: list[str] = []
        for raw in body.splitlines():
            line = raw.strip()
            if not line or line.startswith("--"):
                continue
            keyword = line.split()[0].upper()
            if keyword in {"CREATE", "PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN", "CHECK"}:
                continue
            columns.append(line.split()[0])
        tables[table] = columns
    return tables


# --------------------------------------------------------------------------
# §1 / §2：平台白名单与统一对象形状
# --------------------------------------------------------------------------


def test_platform_enum_matches_contract_whitelist():
    """§2 的 jsonc 注释就是平台白名单，§1 规定 TikTok 暂不投入。"""
    block = _code_block(_subsection("统一发布对象"))
    comment = re.search(r"//\s*(linkedin \| youtube[^\n]*)", block)
    assert comment is not None, "§2 的平台注释解析失败"
    documented = [item.strip() for item in comment.group(1).split("|")]

    assert [p.value for p in Platform] == documented
    assert "tiktok" not in documented
    assert not any("tiktok" in p.value for p in Platform)


def test_unified_post_to_dict_round_trip_matches_contract_shape():
    """``to_dict()`` 的键集合必须与 §2 字段集合逐字一致，且可 JSON 往返。"""
    block = _code_block(_subsection("统一发布对象"))
    documented = set(re.findall(r'^  "([a-z_]+)":', block, re.M))
    assert documented, "§2 字段集合解析失败"

    post = make_post(
        media=(make_media_item(),),
        content_type="image",
        scheduled_at=datetime(2026, 9, 23, 9, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    )
    payload = post.to_dict()
    assert set(payload) == documented

    # 枚举必须落成字面值，时间必须带偏移，嵌套键与 §2 一致
    assert payload["platform"] == "linkedin"
    assert payload["content_type"] == "image"
    assert payload["scheduled_at"] == "2026-09-23T09:30:00+05:30"
    assert payload["media"][0]["kind"] == "image"
    assert payload["media"][0]["license_status"] == "owned"
    assert set(payload["media"][0]) == {
        "kind",
        "url",
        "mime",
        "width",
        "height",
        "duration_s",
        "license_status",
    }
    assert set(payload["compliance"]) == {
        "ai_generated_disclosure",
        "checked_at",
        "blocked",
        "findings_ref",
    }
    assert set(payload["caption"]) == {"text", "text_zh", "lang"}

    # JSON 往返（消息体一旦不可序列化，队列就会静默丢消息）
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_contract_literal_rules_are_enforced():
    """§2 注释里写死的几条规则，逐条做行为验证。"""
    base = make_post()
    assert base.validate() == []

    # scheduled_at 必须带 offset，否则多时区排期会漂移
    naive = dataclasses.replace(base, scheduled_at=datetime(2026, 9, 23, 9, 30))
    assert any("时区偏移" in msg for msg in naive.validate())

    # hashtag 已含 #、不含空格
    assert any("必须以 # 开头" in msg for msg in dataclasses.replace(base, hashtags=("casting",)).validate())
    assert any("不能含空格" in msg for msg in dataclasses.replace(base, hashtags=("#cast ing",)).validate())

    # 素材 license_status 只允许 owned / licensed
    pending = dataclasses.replace(
        base,
        content_type="image",
        media=(make_media_item(license_status=LicenseStatus.PENDING),),
    )
    assert any("不得发布" in msg for msg in pending.validate())
    bogus = dataclasses.replace(
        base,
        content_type="image",
        media=(make_media_item(license_status="borrowed"),),
    )
    assert any("license_status 非法" in msg for msg in bogus.validate())

    # 图片必须给尺寸，视频必须给时长
    no_size = dataclasses.replace(
        base,
        content_type="image",
        media=(make_media_item(width=None, height=None),),
    )
    assert any("width" in msg for msg in no_size.validate())
    no_duration = dataclasses.replace(
        base,
        content_type="video",
        media=(make_media_item(kind=MediaKind.VIDEO, width=None, height=None, duration_s=None),),
    )
    assert any("duration_s" in msg for msg in no_duration.validate())

    # compliance.blocked=True 时网关必须拒绝发布（契约 §2）
    assert any("拒绝发布" in msg for msg in make_post(blocked=True).validate())


# --------------------------------------------------------------------------
# §2.1：必填 options（表格 → 行为）
# --------------------------------------------------------------------------


def _valid_post_for(platform: str) -> UnifiedPost:
    """构造该平台的最小合法帖（用于 options 必填项的行为验证）。"""
    if platform == "youtube":
        return make_post(
            "youtube",
            title="Inside the foundry: casting to pre-machining",
            content_type="video",
            media=(
                make_media_item(
                    kind=MediaKind.VIDEO,
                    url="s3://pulse-media/a6/clip.mp4",
                    width=None,
                    height=None,
                    duration_s=96.0,
                ),
            ),
        )
    if platform == "reddit":
        return make_post("reddit", title="Casting plus pre-machining under one roof")
    if platform == "vk":
        return make_post("vk", text="Мы производим чугунное литьё.")
    return make_post(platform)


def test_options_required_flags_are_enforced_behaviorally():
    """§2.1 的每一行都要能被代码认出来：必填的缺了就报错，可选的缺了不报错。"""
    requirements = contract_option_requirements()
    assert requirements, "§2.1 表解析失败（解析不到内容等于没有校验）"
    assert set(requirements) == {
        "linkedin",
        "youtube",
        "facebook",
        "reddit",
        "vk",
    }, "§2.1 覆盖的平台集合变了，需同步复核实现"

    for platform, fields in requirements.items():
        assert any(fields.values()), f"{platform} 在契约里应当至少有一个必填字段"
        for field, required in fields.items():
            post = _valid_post_for(platform)
            assert post.validate() == [], f"{platform} 的基准帖不合法，后续断言无意义"

            options = dict(post.options)
            options.pop(field, None)
            candidate = dataclasses.replace(post, options=options)
            errors = candidate.validate()
            mentioned = [msg for msg in errors if f"options.{field}" in msg]
            if required:
                assert mentioned, f"契约 §2.1 要求 {platform}.{field} 必填，但缺失时未报错"
            else:
                assert not mentioned, f"{platform}.{field} 在契约里是可选，却报了必填错误"


def test_platform_without_contract_row_needs_no_options():
    """§2.1 没有 instagram 行——它不应有任何必填 options。"""
    assert "instagram" not in contract_option_requirements()
    post = make_post("instagram", options={})
    assert post.validate() == []


def test_vk_owner_id_must_be_negative_community_id():
    """§2.1 的 vk 行写明：owner_id 必须为负数社区 ID。"""
    positive = make_post("vk", options={"owner_id": 123456})
    assert any("负数" in msg for msg in positive.validate())
    zero = make_post("vk", options={"owner_id": 0})
    assert any("不能为 0" in msg for msg in zero.validate())
    good = make_post("vk", options={"owner_id": -123456, "from_group": True})
    assert good.validate() == []


def test_youtube_and_reddit_require_title():
    """§2 注释：title 对 YouTube / Reddit 必填，其他平台可空。"""
    youtube = make_post(
        "youtube",
        content_type="video",
        media=(make_media_item(kind=MediaKind.VIDEO, width=None, height=None, duration_s=60.0),),
        title=None,
    )
    assert any("必须提供 title" in msg for msg in youtube.validate())
    reddit = make_post("reddit", title=None)
    assert any("必须提供 title" in msg for msg in reddit.validate())
    assert make_post("linkedin", title=None).validate() == []


# --------------------------------------------------------------------------
# §3：枚举与状态机
# --------------------------------------------------------------------------


def test_status_enums_match_contract_diagrams():
    """§3.1–3.3 的状态图里的标识符集合 == 枚举取值集合（双向，多一个少一个都红）。"""
    pairs = [
        ("3.1 `VariantStatus`", VariantStatus),
        ("3.2 `ScheduleStatus`", ScheduleStatus),
        ("3.3 `PublishJobStatus`", PublishJobStatus),
    ]
    for fragment, enum in pairs:
        documented = _identifiers(_code_block(_subsection(fragment)))
        implemented = {member.value for member in enum}
        assert documented, f"{fragment} 没解析到状态标识符"
        assert documented == implemented, (
            f"{fragment} 的文档与代码不一致："
            f"仅文档有 {sorted(documented - implemented)}，"
            f"仅代码有 {sorted(implemented - documented)}"
        )


def test_error_class_enum_and_retry_split_match_contract_table():
    """§3.4 表格：取值集合 + 「是否重试」三分法（可重试 / 轮询 / 不可重试）。"""
    rows = _table_rows(_subsection("3.4 `ErrorClass`"))
    documented = {
        cells[0].strip().strip("`"): cells[3].replace("*", "").strip()
        for cells in rows
        if len(cells) >= 4 and cells[0].startswith("`")
    }
    assert documented, "§3.4 表解析失败"
    assert set(documented) == {member.value for member in ErrorClass}

    retryable = {name for name, policy in documented.items() if "是" in policy}
    polling = {name for name, policy in documented.items() if "轮询" in policy}
    not_retryable = {name for name, policy in documented.items() if "否" in policy}

    assert {member.value for member in RETRYABLE_ERRORS} == retryable
    assert {member.value for member in NON_RETRYABLE_ERRORS} == not_retryable
    assert polling == {ErrorClass.MEDIA_PROCESSING.value}

    # 三分法必须是一个划分：既穷尽、又不重叠
    assert retryable | polling | not_retryable == set(documented)
    assert not (retryable & polling) and not (retryable & not_retryable)
    assert not (polling & not_retryable)


def test_compliance_severity_matches_contract_table():
    """§3.5：``block`` / ``warn`` 两个取值。"""
    rows = _table_rows(_subsection("3.5 `Severity`"))
    documented = {cells[0].strip().strip("`") for cells in rows if cells and cells[0].startswith("`")}
    assert documented == {member.value for member in ComplianceSeverity}


def test_terminal_job_statuses_match_contract():
    """§3.3：published / failed / rejected / cancelled 为终态。

    ``failed`` 是**终态但有出边**：契约图里写着 ``failed → retrying → publishing``，
    那是人工重投（``Dispatcher.retry_job``）的入口，不是自动收敛。
    因此这里只要求 ``published`` / ``rejected`` / ``cancelled`` 无出边。
    """
    assert {s.value for s in TERMINAL_JOB_STATUSES} == {
        "published",
        "failed",
        "rejected",
        "cancelled",
    }
    for status in (
        PublishJobStatus.PUBLISHED,
        PublishJobStatus.REJECTED,
        PublishJobStatus.CANCELLED,
    ):
        assert scheduler_state.ALLOWED_JOB_TRANSITIONS[status] == frozenset(), (
            f"{status.value} 是终态，不应有出边"
        )
    assert scheduler_state.ALLOWED_JOB_TRANSITIONS[PublishJobStatus.FAILED] == frozenset(
        {PublishJobStatus.RETRYING, PublishJobStatus.CANCELLED}
    ), "failed 的出边只允许人工重投与取消（契约 §3.3）"


#: §3.2 状态图的人工转写：
#: ``pending → scheduled → publishing → published | failed``，``cancelled`` 从任意非终态进入。
#: **published 只能从 publishing 进入**（这是契约图的硬约束，也是同步平台缺陷的判定依据）。
EXPECTED_SCHEDULE_EDGES: dict[ScheduleStatus, frozenset[ScheduleStatus]] = {
    ScheduleStatus.PENDING: frozenset({ScheduleStatus.SCHEDULED, ScheduleStatus.CANCELLED}),
    ScheduleStatus.SCHEDULED: frozenset(
        {
            ScheduleStatus.PUBLISHING,
            ScheduleStatus.FAILED,
            ScheduleStatus.CANCELLED,
        }
    ),
    ScheduleStatus.PUBLISHING: frozenset(
        {
            ScheduleStatus.PUBLISHED,
            ScheduleStatus.FAILED,
            ScheduleStatus.CANCELLED,
        }
    ),
    ScheduleStatus.PUBLISHED: frozenset(),
    ScheduleStatus.FAILED: frozenset(),
    ScheduleStatus.CANCELLED: frozenset(),
}


def test_schedule_transition_table_matches_contract():
    """§3.2 迁移表（基线未覆盖）：逐条比对，多一条边也要红。"""
    assert dict(scheduler_state.ALLOWED_SCHEDULE_TRANSITIONS) == dict(EXPECTED_SCHEDULE_EDGES)
    # published 只能从 publishing 进入：scheduled 与 pending 都没有直连 published 的边
    for source in (ScheduleStatus.PENDING, ScheduleStatus.SCHEDULED):
        assert ScheduleStatus.PUBLISHED not in EXPECTED_SCHEDULE_EDGES[source]
    assert ScheduleStatus.CANCELLED in EXPECTED_SCHEDULE_EDGES[ScheduleStatus.PENDING]


#: §3.3 状态图的人工转写（含 failed → retrying → publishing 与 pending_finalize 收敛）
EXPECTED_JOB_EDGES: dict[PublishJobStatus, frozenset[PublishJobStatus]] = {
    PublishJobStatus.QUEUED: frozenset(
        {
            PublishJobStatus.DISPATCHING,
            PublishJobStatus.CANCELLED,
            PublishJobStatus.FAILED,
        }
    ),
    PublishJobStatus.DISPATCHING: frozenset(
        {
            PublishJobStatus.PUBLISHING,
            PublishJobStatus.PENDING_FINALIZE,
            PublishJobStatus.RETRYING,
            PublishJobStatus.FAILED,
            PublishJobStatus.REJECTED,
            PublishJobStatus.CANCELLED,
        }
    ),
    PublishJobStatus.PUBLISHING: frozenset(
        {
            PublishJobStatus.PUBLISHED,
            PublishJobStatus.PENDING_FINALIZE,
            PublishJobStatus.RETRYING,
            PublishJobStatus.FAILED,
            PublishJobStatus.REJECTED,
            PublishJobStatus.CANCELLED,
        }
    ),
    PublishJobStatus.PENDING_FINALIZE: frozenset(
        {
            PublishJobStatus.PUBLISHED,
            PublishJobStatus.FAILED,
            PublishJobStatus.CANCELLED,
        }
    ),
    PublishJobStatus.FAILED: frozenset(
        {PublishJobStatus.RETRYING, PublishJobStatus.CANCELLED}
    ),
    PublishJobStatus.RETRYING: frozenset(
        {
            PublishJobStatus.DISPATCHING,
            PublishJobStatus.PUBLISHING,
            PublishJobStatus.FAILED,
            PublishJobStatus.CANCELLED,
        }
    ),
    PublishJobStatus.PUBLISHED: frozenset(),
    PublishJobStatus.REJECTED: frozenset(),
    PublishJobStatus.CANCELLED: frozenset(),
}


def test_job_transition_table_matches_contract():
    """§3.3 迁移表（基线未覆盖）。"""
    assert dict(scheduler_state.ALLOWED_JOB_TRANSITIONS) == dict(EXPECTED_JOB_EDGES)
    for source, targets in EXPECTED_JOB_EDGES.items():
        for target in targets:
            assert scheduler_state.assert_job_transition(source, target) is target


def test_illegal_transitions_are_rejected_not_silently_allowed():
    """终态与跨状态跳跃必须显式报错（契约 §3.2 / §3.3）。"""
    from pulse.services.scheduler.errors import InvalidTransition

    illegal = [
        (ScheduleStatus.PUBLISHED, ScheduleStatus.PENDING),
        (ScheduleStatus.CANCELLED, ScheduleStatus.SCHEDULED),
        (ScheduleStatus.PENDING, ScheduleStatus.PUBLISHED),
    ]
    for source, target in illegal:
        with pytest.raises(InvalidTransition):
            scheduler_state.assert_schedule_transition(source, target)

    for source, target in [
        (PublishJobStatus.PUBLISHED, PublishJobStatus.RETRYING),
        (PublishJobStatus.REJECTED, PublishJobStatus.PUBLISHING),
        (PublishJobStatus.QUEUED, PublishJobStatus.PUBLISHED),
    ]:
        with pytest.raises(InvalidTransition):
            scheduler_state.assert_job_transition(source, target)


# --------------------------------------------------------------------------
# 跨域一致性：A2 与 A3 各自实现了一份「结果 → 状态」，必须逐格一致
# --------------------------------------------------------------------------


RESULT_MATRIX: tuple[tuple[dict[str, Any], PublishJobStatus], ...] = (
    ({"ok": True, "status": "published"}, PublishJobStatus.PUBLISHED),
    ({"ok": True, "status": "pending_finalize"}, PublishJobStatus.PENDING_FINALIZE),
    ({"ok": True, "status": "publishing"}, PublishJobStatus.PENDING_FINALIZE),
    ({"ok": True, "status": "something_new"}, PublishJobStatus.FAILED),
    (
        {"ok": False, "status": "failed", "error_class": "rate_limited"},
        PublishJobStatus.RETRYING,
    ),
    (
        {"ok": False, "status": "failed", "error_class": "transient"},
        PublishJobStatus.RETRYING,
    ),
    (
        {"ok": False, "status": "failed", "error_class": "auth_expired"},
        PublishJobStatus.RETRYING,
    ),
    (
        {"ok": False, "status": "pending_finalize", "error_class": "media_processing"},
        PublishJobStatus.PENDING_FINALIZE,
    ),
    (
        {"ok": False, "status": "failed", "error_class": "media_processing"},
        PublishJobStatus.PENDING_FINALIZE,
    ),
    (
        {"ok": False, "status": "rejected", "error_class": "policy_rejected"},
        PublishJobStatus.REJECTED,
    ),
    (
        {"ok": False, "status": "failed", "error_class": "validation_error"},
        PublishJobStatus.FAILED,
    ),
    ({"ok": False, "status": "failed"}, PublishJobStatus.FAILED),
)


def test_result_to_job_status_matrix_is_identical_in_both_domains():
    """A2 ``publish.state.job_status_for`` 与 A3 ``scheduler.state.job_status_for_result``
    是契约 §3.3 的两份独立实现（代码注释承认了这份重复），必须逐格一致。"""
    from pulse.shared.models import PublishResult

    for payload, expected in RESULT_MATRIX:
        result = PublishResult(
            platform_post_id="pf_a6_1" if payload.get("ok") else None, **payload
        )
        from_a2 = publish_state.job_status_for(result)
        from_a3 = scheduler_state.job_status_for_result(result)
        assert from_a2 is expected, f"A2 判定错误：{payload} → {from_a2}（期望 {expected}）"
        assert from_a3 is expected, f"A3 判定错误：{payload} → {from_a3}（期望 {expected}）"


def test_accepted_statuses_never_become_published():
    """契约 §3.3 最硬的一条：受理 ≠ 发布成功。"""
    from pulse.shared.models import PublishResult

    assert publish_state.ACCEPTED_STATUSES == scheduler_state.ACCEPTED_STATUSES
    for status in sorted(publish_state.ACCEPTED_STATUSES):
        result = PublishResult(ok=True, status=status, platform_post_id="pf_a6_2")
        assert publish_state.job_status_for(result) is PublishJobStatus.PENDING_FINALIZE
        assert scheduler_state.job_status_for_result(result) is PublishJobStatus.PENDING_FINALIZE


def test_publish_result_requires_platform_id_when_accepted():
    """契约 §4：受理后必须能轮询，因此 ``publishing`` 必须带 platform_post_id。"""
    from pulse.shared.models import ContractError, PublishResult

    with pytest.raises(ContractError):
        PublishResult(ok=True, status="publishing")
    assert PublishResult(ok=True, status="publishing", platform_post_id="pf_a6_3").ok


def test_media_processing_takes_the_polling_path_not_failure(clock, adapter):
    """§3.4：``media_processing`` 必须转轮询（``pending_finalize``），不能当失败处理。"""
    from pulse.shared.models import PublishResult

    polling = PublishResult(
        ok=False,
        status="pending_finalize",
        error_class=ErrorClass.MEDIA_PROCESSING.value,
        platform_post_id="pf_a6_media",
    )
    assert publish_state.job_status_for(polling) is PublishJobStatus.PENDING_FINALIZE
    assert scheduler_state.job_status_for_result(polling) is PublishJobStatus.PENDING_FINALIZE

    gateway = PublishGateway(
        adapters=[FakeAdapter("linkedin", FakeBehaviour.MEDIA_PROCESSING)], now=clock
    )
    outcome = run_async(gateway.dispatch(make_post(), CredentialStub()))
    assert outcome.status is PublishJobStatus.PENDING_FINALIZE
    assert outcome.needs_polling is True
    assert outcome.alert is None


# --------------------------------------------------------------------------
# §4：半自动导出（P0 能力）
# --------------------------------------------------------------------------


def test_semi_auto_bundle_exposes_contract_fields(exporter):
    """§4 docstring 写死四个字段：``{text, media_paths, deep_link, checklist}``。"""
    match = re.search(r"返回 \{([a-z_,\s]+)\}", _doc())
    assert match is not None, "§4 的半自动返回结构解析失败"
    documented = {item.strip() for item in match.group(1).split(",") if item.strip()}
    assert documented == {"text", "media_paths", "deep_link", "checklist"}

    fields = {f.name for f in dataclasses.fields(SemiAutoBundle)}
    assert documented <= fields
    assert fields - documented == {"platform"}, "SemiAutoBundle 多出的字段必须有据可依"

    bundle = exporter.export(make_post(media=(make_media_item(),), content_type="image"))
    assert bundle.text.startswith("Casting plus pre-machining")
    assert isinstance(bundle.media_paths, tuple) and bundle.media_paths
    assert bundle.deep_link and bundle.deep_link.startswith("https://")
    assert isinstance(bundle.checklist, tuple) and len(bundle.checklist) >= 2
    # 中文对照仅供审校，绝不能进外发文案包
    assert "中文对照" not in bundle.text


def test_semi_auto_exporter_covers_every_platform_in_contract(exporter):
    """未过审平台全靠半自动出货：白名单里每个平台都要有官方入口。"""
    for platform in Platform:
        bundle = exporter.export(make_post(platform, title="t", text="x"))
        assert bundle.platform == platform.value
        assert bundle.checklist


# --------------------------------------------------------------------------
# §5 / §6 / §7：任务名、DDL 字段、REST 路径
# --------------------------------------------------------------------------


def test_celery_task_names_match_contract_table():
    """§5 的任务名逐个核对，且必须遵守 AGENTS.md §7 的 ``pulse.<domain>.<action>``。"""
    rows = _table_rows(_subsection("5. 队列消息契约"))
    documented = {
        cells[0].strip().strip("`")
        for cells in rows
        if cells and cells[0].startswith("`pulse.")
    }
    assert documented, "§5 任务名表解析失败"

    #: 契约已冻结、但所属域尚未开工的任务名（A4 合规域：pulse/services/compliance/ 不存在）
    declared_not_yet_implemented = {"pulse.compliance.check"}
    implemented = set(ALL_TASK_NAMES) | {TASK_GENERATE_VARIANT}
    assert documented - implemented == declared_not_yet_implemented, (
        "§5 里的任务名与实现不一致："
        f"缺 {documented - implemented - declared_not_yet_implemented}"
    )
    assert implemented <= documented, f"实现了契约外的任务名：{implemented - documented}"

    for name in documented:
        assert re.fullmatch(r"pulse\.[a-z]+\.[a-z_]+", name), f"任务名不符合命名规范：{name}"


def test_contract_tables_are_covered_by_code_records_or_declared_as_gaps():
    """§6 DDL 字段名是契约：已实现的表必须逐字段对上，未实现的表必须在缺口清单里。"""
    tables = contract_tables()
    assert tables, "§6 DDL 解析失败"

    code_records: dict[str, type] = {
        "accounts": Account,
        "credentials": CredentialRecord,
        "contents": ContentRecord,
        "variants": VariantRecord,
        "media_assets": MediaAssetRecord,
        "schedules": ScheduleRecord,
        "publish_jobs": PublishJobRecord,
    }
    #: 记录里允许存在的"非契约列"（运维/反规范化字段），新增必须同步本清单
    allowed_extras = {
        "accounts": set(),
        "credentials": {"key_version"},
        "contents": set(),
        "variants": set(),
        "media_assets": set(),
        "schedules": {"task_id", "reason", "created_at", "updated_at"},
        "publish_jobs": {
            "account_id",
            "platform",
            "task_id",
            "finalize_deadline",
            "created_at",
            "updated_at",
        },
    }

    for table, record in code_records.items():
        columns = set(tables[table])
        fields = {f.name for f in dataclasses.fields(record)}
        assert columns <= fields, f"{table} 缺字段：{columns - fields}"
        assert fields - columns <= allowed_extras[table], (
            f"{table} 出现了未登记的非契约字段：{fields - columns - allowed_extras[table]}"
        )

    unimplemented = set(tables) - set(code_records)
    declared = CONTRACT_TABLES_WITHOUT_IMPLEMENTATION | set(
        CONTRACT_TABLES_PARTIALLY_IMPLEMENTED
    )
    assert unimplemented == declared, (
        "§6 的表集合与『已实现 / 部分实现 / 缺口』清单不一致——有表被实现或新增，需要更新本测试："
        f"仅代码外 {sorted(unimplemented - declared)}，仅清单 {sorted(declared - unimplemented)}"
    )

    # 部分实现的表：``publish_results`` 的列被拆在 A2 的 PublishRecord（回执快照）
    # 与 A3 的 PublishJobRecord（作业行）两处，因此按两者字段的并集核对。
    from pulse.services.publish import PublishRecord

    publish_result_fields = {f.name for f in dataclasses.fields(PublishRecord)} | {
        f.name for f in dataclasses.fields(PublishJobRecord)
    }
    for table, missing in CONTRACT_TABLES_PARTIALLY_IMPLEMENTED.items():
        columns = set(tables[table])
        assert missing <= columns, f"{table} 的契约里没有这些列：{missing - columns}"
        assert (columns - missing) <= publish_result_fields, (
            f"{table} 已实现的列在 A2/A3 的记录上都找不到："
            f"{columns - missing - publish_result_fields}"
        )


def test_contract_unique_index_is_the_idempotency_key():
    """§6：``ux_jobs_unified`` 唯一索引落在 ``publish_jobs.unified_post_id``。"""
    section = _subsection("6. 数据模型")
    assert "CREATE UNIQUE INDEX ux_jobs_unified ON publish_jobs(unified_post_id);" in section


def test_frozen_rest_paths_are_unchanged():
    """§7 的 11 个端点路径冻结；A5 未开工，这里只守住契约本身。"""
    rows = _table_rows(_subsection("7. REST API"))
    documented = {
        (cells[0].strip().strip("`"), cells[1].strip().strip("`"))
        for cells in rows
        if len(cells) >= 2 and cells[1].startswith("`/api/")
    }
    assert documented == FROZEN_REST_ENDPOINTS


def test_id_prefixes_match_contract():
    """§6 DDL 注释与 §2 的 ``up_`` 前缀 → 逐个 ID 生成器行为验证。"""
    section = _subsection("6. 数据模型")
    documented = set(re.findall(r"--\s*([a-z]+)_xxx", section))
    assert documented == {"acct", "src", "var", "sched", "job"}
    assert '"unified_post_id": "up_' in _code_block(_subsection("统一发布对象"))

    prefixes = {
        "acct_": new_account_id(),
        "b_": new_brief_id(),
        "src_": new_source_id(),
        "var_": new_variant_id(),
        "sched_": new_schedule_id(),
        "job_": new_job_id(),
        "up_": new_unified_post_id(),
    }
    for prefix, value in prefixes.items():
        assert value.startswith(prefix), f"{prefix} 前缀未生效：{value}"
        assert len(value) == len(prefix) + 26, f"{prefix} 的 ULID 长度应为 26"
