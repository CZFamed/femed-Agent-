"""合规与治理域的错误类型（所有者：A4）。"""

from __future__ import annotations

from typing import Any


class ComplianceError(Exception):
    """合规域异常基类。"""

    code = "compliance_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.message = message
        self.details = dict(details or {})
        super().__init__(message)

    def as_error_body(self) -> dict[str, Any]:
        """契约 §7 的统一错误体（供 A5 直接用）。"""
        return {"error": {"code": self.code, "message": self.message, "details": dict(self.details)}}


class FindingNotFoundError(ComplianceError):
    code = "finding_not_found"


class WaiverNotAllowedError(ComplianceError):
    """不该被豁免的东西被要求豁免（block 默认不可绕过）。"""

    code = "waiver_not_allowed"


class RuleConfigError(ComplianceError):
    """规则/词库数据文件损坏或缺失。"""

    code = "rule_config_invalid"
