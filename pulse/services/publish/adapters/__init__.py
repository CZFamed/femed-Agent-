"""平台 Adapter 实现。

W1 只实现 P0-A（LinkedIn / YouTube）+ Fake。

VK 已于 2026-09-11 转正（契约 v1.1，P1），W1 尚未实现其 Adapter；
TikTok 维持"暂不投入"。未实现 Adapter 的平台一律走 `SemiAutoBundleExporter` 半自动出货。
"""

from pulse.services.publish.adapters.fake import FakeAdapter, FakeBehaviour
from pulse.services.publish.adapters.linkedin import LinkedInAdapter
from pulse.services.publish.adapters.youtube import YouTubeAdapter

__all__ = ["FakeAdapter", "FakeBehaviour", "LinkedInAdapter", "YouTubeAdapter"]
