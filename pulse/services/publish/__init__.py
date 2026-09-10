"""A2 发布网关域（Pulse）。

对外只有三样东西：

* `PlatformAdapter` / `SemiAutoExporter`（契约 §4 抽象）
* `PublishGateway`（把 `UnifiedPost` 可靠送到平台，并保证幂等）
* 各平台 Adapter（LinkedIn / YouTube）与 `FakeAdapter`（供其他域与测试用）

契约：`pulse/contracts/INTERFACES.md` §2 / §3.3 / §3.4 / §4 / §5。
本域**不**负责排期与限流（那是 A3），**不**负责合规判定（那是 A4）。
"""

from pulse.services.publish.adapters.fake import FakeAdapter
from pulse.services.publish.adapters.linkedin import LinkedInAdapter
from pulse.services.publish.adapters.youtube import YouTubeAdapter
from pulse.services.publish.base import Credential, PlatformAdapter, SemiAutoExporter
from pulse.services.publish.errors import RetryPlan, RetryPolicy, classify_http_status
from pulse.services.publish.gateway import DispatchOutcome, PublishGateway
from pulse.services.publish.semi_auto import SemiAutoBundleExporter
from pulse.services.publish.state import job_status_for
from pulse.services.publish.store import InMemoryPublishStore, PublishRecord, PublishStore

__all__ = [
    "Credential",
    "DispatchOutcome",
    "FakeAdapter",
    "InMemoryPublishStore",
    "LinkedInAdapter",
    "PlatformAdapter",
    "PublishGateway",
    "PublishRecord",
    "PublishStore",
    "RetryPlan",
    "RetryPolicy",
    "SemiAutoBundleExporter",
    "SemiAutoExporter",
    "YouTubeAdapter",
    "classify_http_status",
    "job_status_for",
]
