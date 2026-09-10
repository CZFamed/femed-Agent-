"""版本一致性守护测试（root 所有）。

防止"代码说 1.1.0、包元数据说 1.0.0、变更记录里没有这一版"这类**版本漂移**。
三处版本必须同时成立：``pyproject.toml``、``pulse.__version__``、``CHANGELOG.md``。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pulse
from pulse.shared.models import CONTRACT_VERSION

REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = REPO_ROOT / "pyproject.toml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def _pyproject_version() -> str:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_package_version_matches_pyproject() -> None:
    """`pulse.__version__` 与包元数据版本一致。"""
    assert pulse.__version__ == _pyproject_version()


def test_version_is_semver() -> None:
    """产品版本必须是 `MAJOR.MINOR.PATCH` 三段数字。"""
    parts = pulse.__version__.split(".")
    assert len(parts) == 3, f"版本号不是三段式：{pulse.__version__}"
    assert all(part.isdigit() for part in parts), f"版本号含非数字段：{pulse.__version__}"


def test_changelog_documents_current_version() -> None:
    """每个已发布版本都必须在 CHANGELOG 中有对应标题。"""
    assert CHANGELOG.is_file(), "缺少 CHANGELOG.md"
    text = CHANGELOG.read_text(encoding="utf-8")
    assert f"## [{pulse.__version__}]" in text, f"CHANGELOG.md 未记录版本 {pulse.__version__}"


def test_contract_version_is_frozen_baseline() -> None:
    """契约版本与 1.0 冻结基线一致；升版须由 root 改契约文末变更记录。"""
    assert CONTRACT_VERSION == "1.0"


def test_version_module_exports() -> None:
    """`pulse.__all__` 必须导出产品版本与契约版本，且两者取值一致。"""
    assert "__version__" in pulse.__all__
    assert "__contract_version__" in pulse.__all__
    assert isinstance(pulse.__contract_version__, str)
    assert pulse.__contract_version__ == CONTRACT_VERSION
