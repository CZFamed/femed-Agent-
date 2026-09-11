"""Pulse — 海外社媒内容 Agent（B2B 工业铸件出海）。

产品版本：见 ``__version__``（与 pyproject.toml 的 version 保持一致，由
``pulse/shared/tests/test_release_version.py`` 守护）。
接口契约版本：见 CONTRACT_VERSION（pulse.shared.models）。
契约文件：contracts/INTERFACES.md（冻结，只读）。
"""

__all__ = ["__version__", "__contract_version__"]

# 产品版本（语义化版本）。升版时同步三处：pyproject.toml / 本文件 / CHANGELOG.md。
__version__ = "1.10.1"

from pulse.shared.models import CONTRACT_VERSION as __contract_version__
