"""YouTube Adapter（P0-A）：resumable upload + 异步处理回执。

**关键约束**：上传成功只代表平台"已受理"。YouTube 还要转码/审核，
所以 `publish()` **只能**返回 `pending_finalize`，绝不能直接置 `published`
（契约 §3.3）。终态由 `poll_finalize()` 收敛。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pulse.services.publish.base import PlatformAdapter, options_of
from pulse.services.publish.errors import classify_http_status, result_status_for
from pulse.services.publish.transport import (
    HttpRequest,
    HttpResponse,
    TransportCallable,
    no_transport,
)
from pulse.shared.enums import ErrorClass, MediaKind
from pulse.shared.models import MediaItem, PublishResult, UnifiedPost

YOUTUBE_UPLOAD_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

#: 平台公开限制
MAX_TITLE_LEN = 100
MAX_DESCRIPTION_LEN = 5000
DEFAULT_CATEGORY_ID = "28"  # Science & Technology（契约 §2.1 默认值）

YOUTUBE_PRIVACY_VALUES: frozenset[str] = frozenset({"public", "unlisted", "private"})

#: 回执状态 → 处理方式
_UPLOAD_STATUS_TO_ERROR_CLASS: dict[str, ErrorClass] = {
    "rejected": ErrorClass.POLICY_REJECTED,
    "deleted": ErrorClass.POLICY_REJECTED,
    "failed": ErrorClass.MEDIA_PROCESSING,
}


class MediaNotReadable(RuntimeError):
    """素材无法读取（S3 客户端未注入 / 对象不存在）。"""


class YouTubeAdapter(PlatformAdapter):
    """YouTube 视频发布网关。素材字节由注入的 `read_media` 提供。"""

    platform = "youtube"

    def __init__(
        self,
        *,
        transport: TransportCallable | None = None,
        read_media: Callable[[MediaItem], Awaitable[bytes]] | None = None,
        published: dict[str, str] | None = None,
    ) -> None:
        self._transport: Callable[[HttpRequest], Awaitable[HttpResponse]] = (
            transport or no_transport
        )
        self._read_media: Callable[[MediaItem], Awaitable[bytes]] = (
            read_media or self._default_read_media
        )
        self._published: dict[str, str] = dict(published or {})

    @staticmethod
    async def _default_read_media(item: MediaItem) -> bytes:
        raise MediaNotReadable(
            f"未注入素材读取器，无法上传 {item.url}"
            "（生产由部署层接 S3 客户端；测试请注入假读取器）"
        )

    # -- 校验（不发网络请求） ---------------------------------------------

    async def validate(self, post: UnifiedPost) -> list[str]:
        """校验 YouTube 必填 options、标题与视频素材（契约 §2.1 / §2）。"""

        errors: list[str] = []
        opts = options_of(post)

        privacy = opts.get("privacy_status")
        if not privacy:
            errors.append("youtube 缺少必填 options.privacy_status")
        elif privacy not in YOUTUBE_PRIVACY_VALUES:
            errors.append(
                f"options.privacy_status 非法：{privacy!r}"
                f"（允许：{', '.join(sorted(YOUTUBE_PRIVACY_VALUES))}）"
            )

        if not opts.get("category_id"):
            errors.append(f"youtube 缺少必填 options.category_id（默认 {DEFAULT_CATEGORY_ID}）")

        if "made_for_kids" not in opts:
            errors.append("youtube 缺少必填 options.made_for_kids（默认 false）")
        elif not isinstance(opts["made_for_kids"], bool):
            errors.append("options.made_for_kids 必须是布尔值")

        if not post.title:
            errors.append("platform=youtube 必须提供 title")
        elif len(post.title) > MAX_TITLE_LEN:
            errors.append(f"标题 {len(post.title)} 字，超过 YouTube 上限 {MAX_TITLE_LEN}")

        if len(post.caption.text) > MAX_DESCRIPTION_LEN:
            errors.append(
                f"描述 {len(post.caption.text)} 字，超过 YouTube 上限 {MAX_DESCRIPTION_LEN}"
            )

        has_video = any(
            (m.kind if isinstance(m.kind, MediaKind) else MediaKind(m.kind)) is MediaKind.VIDEO
            for m in post.media
        )
        if not has_video:
            errors.append("platform=youtube 必须提供视频素材（不接受纯文本/图片贴）")

        return errors

    # -- 幂等兜底 ----------------------------------------------------------

    async def find_existing(self, post: UnifiedPost) -> str | None:
        """本地映射兜底。

        **不要**把 search API 放进常规路径：一次 search.list 约 100 配额单位，
        而单次上传约 1600 单位、日配额约 10000（派工单 §5）。
        """

        return self._published.get(post.unified_post_id)

    # -- 发布 --------------------------------------------------------------

    @staticmethod
    def _video_item(post: UnifiedPost) -> MediaItem:
        """取第一个视频素材；没有则给明确错误（而不是裸 StopIteration）。"""

        for item in post.media:
            kind = item.kind if isinstance(item.kind, MediaKind) else MediaKind(item.kind)
            if kind is MediaKind.VIDEO:
                return item
        raise ValueError("platform=youtube 必须提供视频素材，当前 post.media 里没有视频")

    async def publish(self, post: UnifiedPost, credential: Any) -> PublishResult:
        """resumable upload：先建会话，再传字节。**永远返回 pending_finalize**。"""

        opts = options_of(post)
        video_item = self._video_item(post)

        snippet: dict[str, Any] = {
            "title": post.title,
            "description": post.caption.text,
            "categoryId": str(opts.get("category_id", DEFAULT_CATEGORY_ID)),
        }
        if post.hashtags:
            snippet["tags"] = [tag.lstrip("#") for tag in post.hashtags]

        init_response = await self._transport(
            HttpRequest(
                method="POST",
                url=YOUTUBE_UPLOAD_URL,
                json={
                    "snippet": snippet,
                    "status": {
                        "privacyStatus": opts.get("privacy_status"),
                        "madeForKids": opts.get("made_for_kids", False),
                    },
                },
                headers={
                    "Authorization": f"Bearer {credential.access_token()}",
                    "Content-Type": "application/json",
                    "X-Upload-Content-Type": video_item.mime,
                },
            )
        )

        if not 200 <= init_response.status_code < 300:
            return self._error_result(init_response)

        session_uri = init_response.header("location")
        if not session_uri:
            return PublishResult(
                ok=False,
                status="failed",
                error_class=ErrorClass.VALIDATION_ERROR.value,
                error_message="YouTube 未返回 resumable 会话地址（Location 头缺失）",
                raw={"status_code": init_response.status_code},
            )

        try:
            payload = await self._read_media(video_item)
        except MediaNotReadable as exc:
            return PublishResult(
                ok=False,
                status="failed",
                error_class=ErrorClass.VALIDATION_ERROR.value,
                error_message=str(exc),
            )

        upload_response = await self._transport(
            HttpRequest(
                method="PUT",
                url=session_uri,
                content=payload,
                headers={
                    "Authorization": f"Bearer {credential.access_token()}",
                    "Content-Type": video_item.mime,
                },
            )
        )

        if not 200 <= upload_response.status_code < 300:
            return self._error_result(upload_response)

        platform_post_id = str(upload_response.body.get("id", "") or "")
        if platform_post_id:
            self._published[post.unified_post_id] = platform_post_id

        # 受理 ≠ 发布：上传成功也必须进 pending_finalize，等转码/审核收敛。
        return PublishResult(
            ok=True,
            status="pending_finalize",
            platform_post_id=platform_post_id or None,
            post_url=(
                f"https://www.youtube.com/watch?v={platform_post_id}"
                if platform_post_id
                else None
            ),
            raw={"status_code": upload_response.status_code},
        )

    # -- 回执 --------------------------------------------------------------

    async def poll_finalize(self, platform_post_id: str, credential: Any) -> PublishResult:
        """查 `videos.status.uploadStatus` 收敛终态。"""

        response = await self._transport(
            HttpRequest(
                method="GET",
                url=f"{YOUTUBE_VIDEOS_URL}?part=status&id={platform_post_id}",
                headers={"Authorization": f"Bearer {credential.access_token()}"},
            )
        )

        if not 200 <= response.status_code < 300:
            return self._error_result(response)

        items = list(response.body.get("items") or [])
        if not items:
            return PublishResult(
                ok=False,
                status="failed",
                error_class=ErrorClass.VALIDATION_ERROR.value,
                error_message=f"YouTube 查不到视频 {platform_post_id}（可能已删除或凭据越权）",
                raw={"status_code": response.status_code},
            )

        upload_status = str((items[0].get("status") or {}).get("uploadStatus", ""))
        post_url = f"https://www.youtube.com/watch?v={platform_post_id}"

        if upload_status == "processed":
            return PublishResult(
                ok=True,
                status="published",
                platform_post_id=platform_post_id,
                post_url=post_url,
            )
        if upload_status in ("uploaded", "processing", ""):
            return PublishResult(
                ok=True,
                status="pending_finalize",
                platform_post_id=platform_post_id,
                post_url=post_url,
            )

        error_class = _UPLOAD_STATUS_TO_ERROR_CLASS.get(
            upload_status, ErrorClass.MEDIA_PROCESSING
        )
        return PublishResult(
            ok=False,
            status=result_status_for(error_class),
            platform_post_id=platform_post_id,
            error_class=error_class.value,
            error_message=f"YouTube uploadStatus={upload_status}",
            raw={"status_code": response.status_code},
        )

    async def fetch_metrics(self, platform_post_id: str, credential: Any) -> dict:
        """FR-8 数据回捞（P1）：M1/M2 返回空字典。"""

        return {}

    # -- 内部 --------------------------------------------------------------

    @staticmethod
    def _google_reason(body: dict[str, Any]) -> str | None:
        """从 Google API 错误体里取业务 reason（403 的语义全靠它区分）。"""

        error = body.get("error") or {}
        nested = list(error.get("errors") or [])
        if nested:
            reason = nested[0].get("reason")
            if reason:
                return str(reason)
        if error.get("status"):
            return str(error["status"])
        return None

    def _error_result(self, response: HttpResponse) -> PublishResult:
        body = dict(response.body)
        reason = self._google_reason(body)
        error = body.get("error") or {}
        message = str(body.get("message") or error.get("message") or "")
        if not message:
            message = f"YouTube 返回 {response.status_code}"
        error_class = classify_http_status(
            response.status_code, reason=reason, message=message
        )
        return PublishResult(
            ok=False,
            status=result_status_for(error_class),
            error_class=error_class.value,
            error_message=message,
            raw={"status_code": response.status_code},
        )
