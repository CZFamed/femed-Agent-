"""接口层（所有者：A5）：契约 §7 的 11 个端点 + 审批与半自动导出。

**框架无关**：`ApiApp.handle(method, path, body=…)` 直接返回 `ApiResponse`，
测试不需要起 HTTP 服务。要挂到 `http.server` / Cloudflare Worker 上只需写一层薄适配。

两条硬约束（派工单 §4）：

1. **不依赖任何具体 Adapter**：本模块只调用各域的入口（内容域、调度域、账号域、合规域）
   与抽象出来的导出器接口，不 import LinkedIn/YouTube 适配器。
2. **KPI 用询盘与触达口径**，不用点赞数（B2B 决策周期 3–18 个月）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar, Mapping, Sequence

from pulse.api.approvals import BATCH_APPROVE, ApprovalService
from pulse.api.errors import ApiResponse, bad_request, conflict, not_found, unsupported
from pulse.shared.enums import Platform, VariantStatus
from pulse.shared.models import UnifiedPost


@dataclass
class PostRegistry:
    """变体 → `UnifiedPost` 的登记处。

    生成时 A1 产出的 `UnifiedPost` 是排期/发布/半自动导出都要用的对象，
    这里按 `variant_id` 与 `unified_post_id` 双键登记，避免各处重新拼装。
    """

    _by_variant: dict[str, UnifiedPost] = field(default_factory=dict, repr=False)
    _by_unified: dict[str, UnifiedPost] = field(default_factory=dict, repr=False)

    def register(self, post: UnifiedPost) -> None:
        self._by_variant[post.variant_id] = post
        self._by_unified[post.unified_post_id] = post

    def by_variant(self, variant_id: str) -> UnifiedPost | None:
        return self._by_variant.get(str(variant_id))

    def by_unified(self, unified_post_id: str) -> UnifiedPost | None:
        return self._by_unified.get(str(unified_post_id))

    def __len__(self) -> int:  # pragma: no cover - 便于断言
        return len(self._by_variant)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ApiApp:
    """11 个端点的路由与处理器。"""

    content: Any
    approvals: ApprovalService
    dispatcher: Any | None = None
    identity: Any | None = None
    compliance: Any | None = None
    exporter: Any | None = None
    posts: PostRegistry = field(default_factory=PostRegistry)
    #: 平台 → 账号绑定（`AccountBinding`）；提交 brief 时用它派生变体
    bindings: Mapping[str, Any] = field(default_factory=dict)
    clock: Callable[[], datetime] = _now

    # ---------- 路由表（路径与契约 §7 逐字一致） ----------

    ROUTES: tuple[tuple[str, str, str], ...] = (
        ("POST", "/api/v1/briefs", "post_brief"),
        # 路径参数名与契约 §7 逐字一致（契约统一写 {id}）；
        # 语义别名在 _alias_params() 里补，处理器仍然读 variant_id / schedule_id 这类可读名字
        ("GET", "/api/v1/contents/{id}/variants", "get_content_variants"),
        ("PATCH", "/api/v1/variants/{id}/status", "patch_variant_status"),
        ("POST", "/api/v1/variants/{id}/schedule", "post_variant_schedule"),
        ("PATCH", "/api/v1/schedules/{id}", "patch_schedule"),
        ("POST", "/api/v1/schedules/{id}/publish", "post_schedule_publish"),
        ("GET", "/api/v1/schedules/{id}/semi-auto", "get_semi_auto"),
        ("GET", "/api/v1/accounts", "get_accounts"),
        ("POST", "/api/v1/accounts/{id}/oauth", "post_account_oauth"),
        ("DELETE", "/api/v1/accounts/{id}/credential", "delete_credential"),
        ("POST", "/api/v1/compliance/screen", "post_compliance_screen"),
    )

    @classmethod
    def route_table(cls) -> tuple[dict[str, str], ...]:
        """给控制台/前端与契约一致性检查用的路由清单（不需要实例）。"""
        return tuple(
            {"method": method, "path": path, "handler": handler}
            for method, path, handler in cls.ROUTES
        )

    # ---------- 入口 ----------

    def handle(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        actor: str = "system",
    ) -> ApiResponse:
        """路由 + 统一错误处理。**不抛异常给调用方**（除编程错误外都转成错误体）。"""
        from pulse.api.errors import map_exception

        verb = str(method or "").upper()
        target = "/" + str(path or "").strip("/")
        payload = dict(body or {})
        params = dict(query or {})
        try:
            for route_method, pattern, handler_name in self.ROUTES:
                if route_method != verb:
                    continue
                matched = self._match(pattern, target)
                if matched is None:
                    continue
                matched = self._alias_params(handler_name, matched)
                handler = getattr(self, handler_name)
                return handler(matched, payload, params, actor)
            return not_found(f"没有这个端点：{verb} {target}").response()
        except Exception as exc:  # noqa: BLE001 - 统一转错误体是设计目标
            return map_exception(exc)

    @staticmethod
    def _match(pattern: str, target: str) -> dict[str, str] | None:
        """把 `/api/v1/contents/{content_id}/variants` 与真实路径对上。"""
        regex = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$"
        found = re.match(regex, target)
        return found.groupdict() if found else None

    #: 契约 §7 统一用 `{id}` 作为路径参数名；这里给处理器补一个可读的语义别名
    _ID_ALIASES: ClassVar[Mapping[str, str]] = {
        "get_content_variants": "content_id",
        "patch_variant_status": "variant_id",
        "post_variant_schedule": "variant_id",
        "patch_schedule": "schedule_id",
        "post_schedule_publish": "schedule_id",
        "get_semi_auto": "schedule_id",
        "post_account_oauth": "account_id",
        "delete_credential": "account_id",
    }

    @classmethod
    def _alias_params(cls, handler_name: str, matched: Mapping[str, str]) -> dict[str, str]:
        params = dict(matched)
        if "id" in params:
            params[cls._ID_ALIASES.get(handler_name, "id")] = params["id"]
        return params

    # ---------- 公共小工具 ----------

    def _service(self, name: str) -> Any:
        value = getattr(self, name)
        if value is None:
            raise unsupported(f"该功能需要注入 {name}（当前未接线）")
        return value

    def _post_for_variant(self, variant_id: str) -> UnifiedPost:
        post = self.posts.by_variant(variant_id)
        if post is None:
            raise not_found(
                f"变体 {variant_id} 没有登记 UnifiedPost（先经 POST /briefs 生成）",
                variant_id=variant_id,
            )
        return post

    def variant_payload(self, variant: Any) -> dict[str, Any]:
        payload = variant.as_payload() if hasattr(variant, "as_payload") else dict(variant)
        post = self.posts.by_variant(str(payload.get("id") or payload.get("variant_id") or ""))
        if post is not None:
            payload["unified_post_id"] = post.unified_post_id
            payload["has_media"] = bool(post.media)
        return payload

    # ---------- 1) POST /api/v1/briefs ----------

    def post_brief(
        self, params: Mapping[str, str], body: dict[str, Any], query: dict[str, Any], actor: str
    ) -> ApiResponse:
        """提交 brief 并派生各平台变体（草稿）。"""
        del params, query
        if not isinstance(body, Mapping) or not body:
            raise bad_request("请求体必须是 brief 对象（见 A1 的 brief 字段）")
        content = self.content.submit_brief(body)
        brief = self.content.brief(content.brief_id)
        wanted: Sequence[str] = [item.value for item in brief.platforms]
        if "platforms" in body and body["platforms"]:
            wanted = [str(item).lower() for item in body["platforms"]]

        variants: list[dict[str, Any]] = []
        for platform in wanted:
            binding = self.bindings.get(platform)
            if binding is None:
                raise conflict(
                    f"平台 {platform} 没有账号绑定，无法派生变体（先在 bindings 里配置账号）",
                    platform=platform,
                )
            derived = self.content.generate_variant(content.brief_id, platform, account=binding)
            self.posts.register(derived.post)
            variants.append(self.variant_payload(self.content.store.get_variant(derived.variant_id)))
        return ApiResponse(
            201,
            {
                "content": {
                    "id": content.id,
                    "brief_id": content.brief_id,
                    "title": content.title,
                    "brand_guide_id": content.brand_guide_id,
                },
                "variants": variants,
            },
        )

    # ---------- 2) GET /api/v1/contents/{content_id}/variants ----------

    def get_content_variants(
        self, params: Mapping[str, str], body: dict[str, Any], query: dict[str, Any], actor: str
    ) -> ApiResponse:
        del body, query, actor
        content_id = params["content_id"]
        try:
            self.content.store.get_content(content_id)
        except Exception as exc:  # noqa: BLE001
            raise not_found(f"找不到内容 {content_id}", content_id=content_id) from exc
        variants = [
            self.variant_payload(item)
            for item in self.content.store.variants_for_source(content_id)
        ]
        return ApiResponse(200, {"content_id": content_id, "variants": variants})

    # ---------- 3) PATCH /api/v1/variants/{variant_id}/status ----------

    def patch_variant_status(
        self, params: Mapping[str, str], body: dict[str, Any], query: dict[str, Any], actor: str
    ) -> ApiResponse:
        """提交待审 / 通过 / 驳回 / 改稿 / 整批通过。"""
        del query
        variant_id = params["variant_id"]
        action = str(body.get("action") or "").strip().lower()
        if not action:
            raise bad_request("缺少 action（submit / approve / reject / needs_revision / batch_approve）")
        who = str(body.get("actor") or actor or "").strip()
        diff = body.get("diff")
        brand_guide_changed = bool(body.get("brand_guide_changed", False))
        is_admin = bool(body.get("is_admin", False))

        if action == "submit":
            return ApiResponse(200, self.approvals.submit(variant_id, actor=who, now=self.clock()))
        if action == BATCH_APPROVE:
            ids = body.get("variant_ids") or [variant_id]
            outcome = self.approvals.batch_approve(
                ids,
                actor=who,
                diff=diff,
                brand_guide_changed=brand_guide_changed,
                is_admin=is_admin,
                now=self.clock(),
            )
            return ApiResponse(200, outcome.as_payload())
        record = self.approvals.decide(
            variant_id,
            action=action,
            actor=who,
            diff=diff,
            brand_guide_changed=brand_guide_changed,
            is_admin=is_admin,
            now=self.clock(),
        )
        return ApiResponse(
            200,
            {
                "variant_id": variant_id,
                "status": self.content.store.get_variant(variant_id).status.value,
                "approval": record.as_payload(),
            },
        )

    # ---------- 4) POST /api/v1/variants/{variant_id}/schedule ----------

    def post_variant_schedule(
        self, params: Mapping[str, str], body: dict[str, Any], query: dict[str, Any], actor: str
    ) -> ApiResponse:
        """入排期并投递（**只有 approved 的变体可以进发布池**）。"""
        del query
        dispatcher = self._service("dispatcher")
        variant_id = params["variant_id"]
        variant = self.content.store.get_variant(variant_id)
        if variant.status is not VariantStatus.APPROVED:
            raise conflict(
                f"变体 {variant_id} 当前状态为 {variant.status.value}，"
                "只有 approved 的内容可以进发布池（FR-4 硬要求）",
                variant_id=variant_id,
                status=variant.status.value,
            )

        account_id = str(body.get("account_id") or "").strip()
        if not account_id:
            platform_binding = self.bindings.get(variant.platform.value)
            account_id = str(getattr(platform_binding, "account_id", "") or "")
        if not account_id:
            raise bad_request(
                f"缺少 account_id，且 bindings 里没有 {variant.platform.value} 的默认账号",
                platform=variant.platform.value,
            )

        scheduled_at = _parse_datetime(body.get("scheduled_at"))
        at_best_time = bool(body.get("at_best_time", scheduled_at is None))
        schedule = dispatcher.create_schedule(
            variant_id=variant_id,
            account_id=account_id,
            scheduled_at=scheduled_at,
            at_best_time=at_best_time,
            actor=str(body.get("actor") or actor or "system"),
            now=self.clock(),
        )
        post = self._post_for_variant(variant_id)
        plan = dispatcher.enqueue_schedule(
            schedule.id,
            now=self.clock(),
            unified_post_id=post.unified_post_id,
        )
        return ApiResponse(
            201,
            {
                "schedule": {
                    "id": schedule.id,
                    "variant_id": schedule.variant_id,
                    "account_id": schedule.account_id,
                    "timezone": schedule.timezone,
                    "scheduled_at": schedule.scheduled_at.isoformat(),
                    "status": schedule.status_value.value,
                },
                "job": {
                    "id": plan.job_id,
                    "status": plan.status,
                    "duplicate": plan.duplicate,
                    "reason": plan.reason,
                },
                "unified_post_id": post.unified_post_id,
            },
        )

    # ---------- 5) PATCH /api/v1/schedules/{schedule_id} ----------

    def patch_schedule(
        self, params: Mapping[str, str], body: dict[str, Any], query: dict[str, Any], actor: str
    ) -> ApiResponse:
        """暂停（取消投递）/ 取消 / 重排。"""
        del query
        dispatcher = self._service("dispatcher")
        schedule_id = params["schedule_id"]
        action = str(body.get("action") or "").strip().lower()
        if action not in ("cancel", "pause", "reschedule"):
            raise bad_request("action 必须是 cancel / pause / reschedule 之一")
        who = str(body.get("actor") or actor or "system")
        if action in ("cancel", "pause"):
            plan = dispatcher.cancel_schedule(
                schedule_id, actor=who, reason=str(body.get("reason") or ""), now=self.clock()
            )
        else:
            plan = dispatcher.reschedule_schedule(
                schedule_id,
                scheduled_at=_parse_datetime(body.get("scheduled_at")),
                at_best_time=bool(body.get("at_best_time", False)),
                actor=who,
                now=self.clock(),
            )
        return ApiResponse(
            200,
            {
                "schedule_id": schedule_id,
                "action": plan.action,
                "job_id": plan.job_id,
                "status": plan.status,
                "reason": plan.reason,
            },
        )

    # ---------- 6) POST /api/v1/schedules/{schedule_id}/publish ----------

    def post_schedule_publish(
        self, params: Mapping[str, str], body: dict[str, Any], query: dict[str, Any], actor: str
    ) -> ApiResponse:
        """立即发布（管理员可 `force` 跳过配额与限流，账号状态检查永不跳过）。"""
        del query
        dispatcher = self._service("dispatcher")
        schedule_id = params["schedule_id"]
        schedule = dispatcher.schedules.get(schedule_id)
        if schedule is None:
            raise not_found(f"找不到排期 {schedule_id}", schedule_id=schedule_id)
        post = self._post_for_variant(schedule.variant_id)
        plan = dispatcher.publish_now(
            schedule_id,
            actor=str(body.get("actor") or actor or "system"),
            force=bool(body.get("force", False)),
            unified_post_id=post.unified_post_id,
            now=self.clock(),
        )
        return ApiResponse(
            200,
            {
                "schedule_id": schedule_id,
                "job_id": plan.job_id,
                "status": plan.status,
                "task_id": plan.task_id,
                "reason": plan.reason,
            },
        )

    # ---------- 7) GET /api/v1/schedules/{schedule_id}/semi-auto ----------

    def get_semi_auto(
        self, params: Mapping[str, str], body: dict[str, Any], query: dict[str, Any], actor: str
    ) -> ApiResponse:
        """半自动导出（P0 能力）：正文 + 素材 + 官方入口 + 检查清单。"""
        del body, query, actor
        dispatcher = self._service("dispatcher")
        exporter = self._service("exporter")
        schedule_id = params["schedule_id"]
        schedule = dispatcher.schedules.get(schedule_id)
        if schedule is None:
            raise not_found(f"找不到排期 {schedule_id}", schedule_id=schedule_id)
        post = self._post_for_variant(schedule.variant_id)
        bundle = exporter.export(post)
        return ApiResponse(
            200,
            {
                "schedule_id": schedule_id,
                "platform": post.platform.value if isinstance(post.platform, Platform) else str(post.platform),
                "text": bundle.text,
                "media_paths": list(bundle.media_paths),
                "deep_link": bundle.deep_link,
                "checklist": list(bundle.checklist),
                "note": "半自动包是把内容交给人去平台发布；导出的素材会计入 15 天冷却",
            },
        )

    # ---------- 8) GET /api/v1/accounts ----------

    def get_accounts(
        self, params: Mapping[str, str], body: dict[str, Any], query: dict[str, Any], actor: str
    ) -> ApiResponse:
        """账号列表：状态、时区、配额配置与健康度（不返回任何凭据内容）。"""
        del params, body, actor
        identity = self._service("identity")
        platform = query.get("platform")
        status = query.get("status")
        accounts = identity.list_accounts(platform=platform, status=status)
        payload = []
        for account in accounts:
            payload.append(
                {
                    "id": account.id,
                    "platform": (
                        account.platform.value
                        if hasattr(account.platform, "value")
                        else str(account.platform)
                    ),
                    "display_name": account.display_name,
                    "region": getattr(account, "region", None),
                    "timezone": account.timezone,
                    "status": (
                        account.status_value.value
                        if hasattr(account, "status_value")
                        else str(getattr(account, "status", ""))
                    ),
                    "quota_config": dict(getattr(account, "quota_config", {}) or {}),
                    "has_credential": _has_credential(identity, account.id),
                }
            )
        return ApiResponse(200, {"accounts": payload, "count": len(payload)})

    # ---------- 9) POST /api/v1/accounts/{account_id}/oauth ----------

    def post_account_oauth(
        self, params: Mapping[str, str], body: dict[str, Any], query: dict[str, Any], actor: str
    ) -> ApiResponse:
        """发起 OAuth 接入。

        真实 OAuth 需要平台应用凭据（client_id / client_secret / 回调地址），
        这些属于**尚未提供的真实数据**，所以本端点只创建一次带 `state` 的接入会话，
        缺什么就列什么——不编造授权链接。
        """
        del query
        identity = self._service("identity")
        account_id = params["account_id"]
        account = identity.get_account(account_id)
        callback = str(body.get("redirect_uri") or "").strip()
        client_id = str(body.get("client_id") or "").strip()
        missing = [name for name, value in (("client_id", client_id), ("redirect_uri", callback)) if not value]
        session = {
            "account_id": account.id,
            "platform": account.platform.value if hasattr(account.platform, "value") else str(account.platform),
            "state": f"oauth_{account.id}_{int(self.clock().timestamp())}",
            "status": "pending_configuration" if missing else "pending_authorization",
            "authorize_url": None,
            "missing": missing,
            "requested_by": str(body.get("actor") or actor or "system"),
            "note": (
                "TODO(need-real-data)：平台应用凭据（client_id/secret）与回调地址未提供，"
                "因此不生成授权链接；补齐后由部署层调用平台授权端点"
            ),
        }
        return ApiResponse(202, {"oauth": session})

    # ---------- 10) DELETE /api/v1/accounts/{account_id}/credential ----------

    def delete_credential(
        self, params: Mapping[str, str], body: dict[str, Any], query: dict[str, Any], actor: str
    ) -> ApiResponse:
        """吊销凭据并熔断（管理权限操作；账号状态变化会挂起该账号全部待发任务）。"""
        del query
        identity = self._service("identity")
        account_id = params["account_id"]
        who = str(body.get("actor") or actor or "system")
        record = identity.revoke_credential(
            account_id, actor=who, reason=str(body.get("reason") or "")
        )
        suspended = 0
        if self.dispatcher is not None:
            try:
                result = self.dispatcher.suspend_account(account_id, reason="凭据已吊销")
                suspended = int(getattr(result, "suspended", 0) or 0)
            except Exception:  # noqa: BLE001 - 熔断失败不影响吊销结论，但要留痕
                suspended = -1
        return ApiResponse(
            200,
            {
                "account_id": account_id,
                "credential_status": _status_of(record),
                "revoked_by": who,
                "suspended_jobs": suspended,
            },
        )

    # ---------- 11) POST /api/v1/compliance/screen ----------

    def post_compliance_screen(
        self, params: Mapping[str, str], body: dict[str, Any], query: dict[str, Any], actor: str
    ) -> ApiResponse:
        """制裁与出口管制筛查（红线功能）。"""
        del params, query
        compliance = self._service("compliance")
        subject = str(body.get("subject") or "").strip()
        if not subject:
            raise bad_request("缺少 subject（要筛查的客户或主体名）")
        record = compliance.screen_subject(
            subject,
            checked_by=str(body.get("checked_by") or actor or "system"),
            now=self.clock(),
        )
        return ApiResponse(200, {"screening": record.as_payload()})

    # ---------- 看板（KPI 口径：询盘与触达，不用点赞数） ----------

    def dashboard(self) -> dict[str, Any]:
        """发布看板数据。

        ⚠️ 契约 §7 的 11 个端点里**没有**看板端点，所以这里先做成应用层方法
        （控制台可直接调用），要暴露成 HTTP 必须走契约变更流程——不自行加临时端点。
        """
        variants = list(self.content.store.list_variants())
        by_status: dict[str, int] = {}
        for item in variants:
            key = item.status.value if hasattr(item.status, "value") else str(item.status)
            by_status[key] = by_status.get(key, 0) + 1
        schedules: list[Any] = []
        if self.dispatcher is not None:
            store = self.dispatcher.schedules
            # 调度域的存储提供的是 `list_schedules()`；这里做一次兼容取值，
            # 避免接口层绑死某一个方法名
            lister = getattr(store, "list_schedules", None) or getattr(store, "all_schedules", None)
            schedules = list(lister()) if lister else []
        jobs: list[Any] = []
        if self.dispatcher is not None:
            jobs = list(self.dispatcher.jobs.all_jobs())
        published = [job for job in jobs if _status_of(job) == "published"]
        failed = [job for job in jobs if _status_of(job) in ("failed", "rejected")]
        return {
            "kpi_basis": "询盘与触达口径（B2B 决策周期 3–18 个月；不使用点赞数作为 KPI）",
            "content": {"variants_total": len(variants), "by_status": by_status},
            "publishing": {
                "schedules_total": len(schedules),
                "jobs_total": len(jobs),
                "published": len(published),
                "failed_or_rejected": len(failed),
                "success_rate": round(len(published) / len(jobs), 4) if jobs else None,
            },
            "funnel": {
                "reached_companies": None,
                "inquiries": None,
                "shortlisted": None,
                "note": "触达/询盘/进入短名单三个口径需要 CRM 与表单来源字段，当前标注为待接数据源",
            },
        }


# --------------------------------------------------------------------------
# 模块级小工具
# --------------------------------------------------------------------------


def _parse_datetime(raw: Any) -> datetime | None:
    """解析 ISO8601；**没有时区偏移的字符串直接拒绝**（多时区排期会漂移）。"""
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raise bad_request("时间必须带时区偏移（如 2026-09-23T10:00:00+05:30）")
        return raw
    text = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise bad_request(f"时间格式无法解析：{text!r}") from exc
    if parsed.tzinfo is None:
        raise bad_request(
            f"时间缺少时区偏移：{text!r}（必须形如 2026-09-23T10:00:00+05:30，否则排期会漂移）"
        )
    return parsed


def _status_of(record: Any) -> str:
    for attr in ("status_value", "status"):
        value = getattr(record, attr, None)
        if value is None:
            continue
        return value.value if hasattr(value, "value") else str(value)
    return ""


def _has_credential(identity: Any, account_id: str) -> bool:
    """账号是否有有效凭据（**只返回布尔值，不返回任何凭据内容**）。"""
    try:
        record = identity.credential_record(account_id)
    except Exception:  # noqa: BLE001 - 没有凭据就是 False
        return False
    return _status_of(record) == "valid"
