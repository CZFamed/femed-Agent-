"""半自动导出（P0 能力，契约 §4）。

不是降级方案：MVP 阶段 Facebook 群组、Instagram、Reddit 的部分版块，
以及所有未过审平台，全靠"复制文案 + 唤起官方入口"出货。
"""

from __future__ import annotations

from typing import Callable

from pulse.services.publish.base import SemiAutoExporter, options_of
from pulse.shared.models import SemiAutoBundle, UnifiedPost

#: 各平台**官方**发布入口（不做浏览器自动化，只把入口给到人）
DEEP_LINKS: dict[str, str] = {
    "linkedin": "https://www.linkedin.com/feed/?shareActive=true",
    "youtube": "https://studio.youtube.com/channel/UC/videos/upload",
    "facebook": "https://www.facebook.com/",
    "instagram": "https://www.instagram.com/",
    "reddit": "https://www.reddit.com/submit",
}


class SemiAutoBundleExporter(SemiAutoExporter):
    """把 `UnifiedPost` 转成人工可执行的一揽子拷贝包。

    Args:
        media_url_resolver: 把 `s3://…` 换成人工可下载的地址。
            缺省原样返回（控制台侧再解析）。
    """

    def __init__(
        self, *, media_url_resolver: Callable[[str], str] | None = None
    ) -> None:
        self._resolve = media_url_resolver or (lambda url: url)

    def export(self, post: UnifiedPost) -> SemiAutoBundle:
        """产出 `{text, media_paths, deep_link, checklist}`。"""

        platform = str(post.platform)
        opts = options_of(post)

        text_parts = [post.caption.text]
        if post.hashtags:
            text_parts.append(" ".join(post.hashtags))
        text = "\n\n".join(part for part in text_parts if part)
        # 注意：`caption.text_zh` 仅供审校，**不**进外发文案包。

        media_paths = tuple(self._resolve(item.url) for item in post.media)

        checklist = [
            f"复制文案（{len(post.caption.text)} 字）到 {platform} 官方发布入口",
            "确认素材授权：仅允许 license_status 为 owned / licensed 的素材",
        ]

        if post.media:
            licenses = sorted(
                {
                    item.license_status
                    if isinstance(item.license_status, str)
                    else str(item.license_status)
                    for item in post.media
                }
            )
            checklist.append(f"素材授权状态复核：{', '.join(licenses)}")
        else:
            checklist.append("本条为纯文本贴，无需上传素材")

        if platform == "youtube":
            checklist.append(
                "确认视频已处理完成（平台侧 uploadStatus=processed）后再对外可见"
            )
            checklist.append("填写标题与描述，分类默认 28（Science & Technology）")
        elif platform == "linkedin":
            urn = opts.get("author_urn", "（未指定公司页 URN）")
            checklist.append(f"确认以公司页 {urn} 发布，而非个人号")
        elif platform == "reddit":
            subreddit = opts.get("subreddit", "（未指定 subreddit）")
            checklist.append(f"人工确认版规后发布到 r/{subreddit}，需要时补 flair")
        else:
            checklist.append("该平台无自动适配器，人工发布后回填平台链接")

        checklist.append("发布后回到控制台回填平台贴文链接（人工闭环留痕）")

        return SemiAutoBundle(
            text=text,
            media_paths=media_paths,
            deep_link=DEEP_LINKS.get(platform),
            checklist=tuple(checklist),
            platform=platform,
        )
