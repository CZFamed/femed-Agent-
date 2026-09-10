"""平台 Adapter 实现。

W1 只实现 P0-A（LinkedIn / YouTube）+ Fake；VK 冻结不实现，TikTok 不投入
（未过审平台一律走 `SemiAutoBundleExporter` 半自动出货）。
"""

from pulse.services.publish.adapters.fake import FakeAdapter, FakeBehaviour
from pulse.services.publish.adapters.linkedin import LinkedInAdapter
from pulse.services.publish.adapters.youtube import YouTubeAdapter

__all__ = ["FakeAdapter", "FakeBehaviour", "LinkedInAdapter", "YouTubeAdapter"]
