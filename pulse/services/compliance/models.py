"""合规域的数据结构（所有者：A4）。

`Finding` 的字段与契约 §6 `compliance_findings` 表一一对应；
`VariantView` 是本域的输入视图——**只吃 A1 产物的公开字段**，不看生成过程，
也不 import A1 的内部实现（派工单 §1 与 §4：生成者不能判定自己合规）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from pulse.shared.enums import ComplianceSeverity, LicenseStatus


@dataclass(frozen=True, slots=True)
class Finding:
    """一条合规发现项（契约 §6 `compliance_findings`）。"""

    rule: str
    severity: ComplianceSeverity
    message: str
    position: Mapping[str, Any] | None = None
    #: 由服务层填入的自增 ID（对应 `compliance_findings.id`）
    id: int | None = None
    waived_by: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule": self.rule,
            "severity": self.severity.value,
            "message": self.message,
            "position": dict(self.position) if self.position else None,
            "waived_by": self.waived_by,
        }


@dataclass(frozen=True, slots=True)
class MediaView:
    """素材在合规视角下需要的最小信息。"""

    license_status: str = LicenseStatus.OWNED.value
    kind: str = "image"
    url: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class VariantView:
    """一次合规判定的输入视图。"""

    variant_id: str
    platform: str
    text: str
    text_zh: str | None = None
    title: str | None = None
    hashtags: tuple[str, ...] = ()
    media: tuple[MediaView, ...] = ()
    #: 品牌规范（`brand_guides.payload`）；目前只用到"是否更换过 Brand Guide"这类标记
    brand_guide_id: str | None = None
    #: 是否是首次接入该账号（首次接入必须逐条复核）
    first_time_account: bool = False
    options: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_variant_record(cls, record: Any, *, platform: str | None = None) -> "VariantView":
        """从 A1 的 `VariantRecord`（或等价对象）构造视图。

        只读 `fields` 里的公开键，不 import A1 的模块——契约 §9 要求单向依赖，
        而且合规判定不该跟内容域的实现细节绑在一起。
        """
        fields = dict(getattr(record, "fields", {}) or {})
        caption = fields.get("caption") or {}
        media = tuple(
            MediaView(
                license_status=str(item.get("license_status") or LicenseStatus.OWNED.value),
                kind=str(item.get("kind") or "image"),
                url=str(item.get("url") or ""),
            )
            for item in (fields.get("media") or ())
            if isinstance(item, Mapping)
        )
        return cls(
            variant_id=str(getattr(record, "id", "") or ""),
            platform=str(platform or getattr(record, "platform", "") or ""),
            text=str(caption.get("text") or fields.get("text") or ""),
            text_zh=caption.get("text_zh"),
            title=fields.get("title"),
            hashtags=tuple(str(tag) for tag in (fields.get("hashtags") or ())),
            media=media,
            brand_guide_id=fields.get("brand_guide_id"),
        )


def position(field_name: str, text: str, needle: str, *, index: int | None = None) -> dict[str, Any]:
    """构造 `position`：字段名 + 字符偏移 + 命中片段。

    契约要求"定位到字段名 + 字符偏移"，否则审批人在控制台上看不到是哪一句有问题。
    """
    offset = text.lower().find(needle.lower()) if index is None else index
    return {
        "field": field_name,
        "offset": offset,
        "length": len(needle),
        "excerpt": text[max(offset, 0) : max(offset, 0) + len(needle) + 20]
        if offset >= 0
        else "",
    }


def covered_fields() -> Sequence[str]:
    """本域会做定位的字段（供 A5 渲染审批界面与 A6 做一致性检查）。"""
    return ("text", "text_zh", "title", "hashtags", "media")
