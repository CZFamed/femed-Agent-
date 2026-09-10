"""LinkedIn 公司页 Adapter（P0-A）。

发布是**同步**的：平台返回 2xx 即已生成贴文 URN，因此 `poll_finalize()`
直接回 `published`（契约 §4 对同步平台的约定）。

幂等：LinkedIn REST 不支持按幂等键检索贴文，所以两层兜底是
① 发布时携带 `X-Restli-Idempotency-Key: <unified_post_id>`；
② `find_existing()` 依赖已落库的映射（A3 侧 `publish_results`）。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pulse.services.publish.base import PlatformAdapter, options_of
from pulse.services.publish.errors import classify_http_status, result_status_for
from pulse.services.publish.transport import HttpRequest, HttpResponse, TransportCallable, no_transport
from pulse.shared.models import PublishResult, UnifiedPost

LINKEDIN_API_POSTS = "https://api.linkedin.com/rest/posts"
LINKEDIN_API_VERSION = "202409"

#: 公司页可见性取值（契约 §2.1，三值）
LINKEDIN_VISIBILITIES: frozenset[str] = frozenset({"PUBLIC", "CONNECTIONS", "LOGGED_IN"})

#: 正文长度上限（平台公开限制）
MAX_COMMENTARY_LEN = 3000


class LinkedInAdapter(PlatformAdapter):
    """LinkedIn 公司页发布网关。所有网络调用都走注入的 `transport`。"""

    platform = "linkedin"

    def __init__(
        self,
        *,
        transport: TransportCallable | None = None,
        published: dict[str, str] | None = None,
    ) -> None:
        self._transport: Callable[[HttpRequest], Awaitable[HttpResponse]] = (
            transport or no_transport
        )
        #: unified_post_id → 平台 URN（幂等兜底；生产由 A3 的 publish_results 提供）
        self._published: dict[str, str] = dict(published or {})

    # -- 校验（不发网络请求） ---------------------------------------------

    async def validate(self, post: UnifiedPost) -> list[str]:
        """校验 LinkedIn 必填 options 与正文长度。"""

        errors: list[str] = []
        opts = options_of(post)

        urn = opts.get("author_urn")
        if not urn:
            errors.append("linkedin 缺少必填 options.author_urn（公司页 URN）")
        elif not str(urn).startswith("urn:li:"):
            errors.append(f"options.author_urn 必须以 urn:li: 开头：{urn!r}")

        visibility = opts.get("linkedin_visibility")
        if not visibility:
            errors.append("linkedin 缺少必填 options.linkedin_visibility")
        elif visibility not in LINKEDIN_VISIBILITIES:
            errors.append(
                f"options.linkedin_visibility 非法：{visibility!r}"
                f"（允许：{', '.join(sorted(LINKEDIN_VISIBILITIES))}）"
            )

        if len(post.caption.text) > MAX_COMMENTARY_LEN:
            errors.append(
                f"正文 {len(post.caption.text)} 字，超过 LinkedIn 上限 {MAX_COMMENTARY_LEN}"
            )

        return errors

    # -- 幂等兜底 ----------------------------------------------------------

    async def find_existing(self, post: UnifiedPost) -> str | None:
        """返回已落库的平台 URN（本地映射，不消耗 API 配额）。"""

        return self._published.get(post.unified_post_id)

    # -- 发布 --------------------------------------------------------------

    def _commentary(self, post: UnifiedPost) -> str:
        """正文 + hashtags（hashtags 在契约里已含 `#`、不含空格）。"""

        tags = " ".join(post.hashtags)
        return f"{post.caption.text}\n\n{tags}".strip() if tags else post.caption.text

    async def publish(self, post: UnifiedPost, credential: Any) -> PublishResult:
        """调用 `/rest/posts` 发布公司页贴文。"""

        opts = options_of(post)
        body: dict[str, Any] = {
            "author": opts.get("author_urn"),
            "commentary": self._commentary(post),
            "visibility": opts.get("linkedin_visibility"),
            "distribution": {"feedDistribution": "MAIN_FEED"},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }

        response = await self._transport(
            HttpRequest(
                method="POST",
                url=LINKEDIN_API_POSTS,
                json=body,
                headers={
                    "Authorization": f"Bearer {credential.access_token()}",
                    "X-Restli-Idempotency-Key": post.unified_post_id,
                    "LinkedIn-Version": LINKEDIN_API_VERSION,
                    "Content-Type": "application/json",
                },
            )
        )

        if 200 <= response.status_code < 300:
            platform_post_id = response.header("x-restli-id") or str(
                response.body.get("id", "")
            )
            if platform_post_id:
                self._published[post.unified_post_id] = platform_post_id
            post_url = (
                f"https://www.linkedin.com/feed/update/{platform_post_id}"
                if platform_post_id
                else None
            )
            # LinkedIn 同步返回 → 已发布
            return PublishResult(
                ok=True,
                status="published",
                platform_post_id=platform_post_id or None,
                post_url=post_url,
                raw={"status_code": response.status_code},
            )

        reason = response.body.get("serviceErrorCode") or response.body.get("code")
        message = str(response.body.get("message", f"LinkedIn 返回 {response.status_code}"))
        error_class = classify_http_status(
            response.status_code, reason=str(reason) if reason else None, message=message
        )
        return PublishResult(
            ok=False,
            status=result_status_for(error_class),
            error_class=error_class.value,
            error_message=message,
            raw={"status_code": response.status_code},
        )

    # -- 回执 --------------------------------------------------------------

    async def poll_finalize(self, platform_post_id: str, credential: Any) -> PublishResult:
        """LinkedIn 是同步平台，无异步回执：直接收敛为已发布。"""

        return PublishResult(
            ok=True,
            status="published",
            platform_post_id=platform_post_id,
            post_url=f"https://www.linkedin.com/feed/update/{platform_post_id}",
        )

    async def fetch_metrics(self, platform_post_id: str, credential: Any) -> dict:
        """FR-8 数据回捞（P1）：M1/M2 返回空字典。"""

        return {}
