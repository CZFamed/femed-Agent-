"""内容与变体的本地存储（所有者：A1）。

字段与契约 §6 的 ``contents`` / ``variants`` / ``media_assets`` 三张表一一对应，
但**存储介质是内存**：A1 的验收标准是"不启动其他域也能跑通"（契约 §9），
落库由 A5/基础设施在集成阶段接上，域内不引入数据库依赖。

ID 一律走 ``pulse.shared.ids``，前缀由契约 §7 规定（``b_`` / ``src_`` / ``var_``），
不要在域内自己拼。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from pulse.services.content.errors import ContentNotFoundError
from pulse.shared.enums import Platform, VariantStatus
from pulse.shared.ids import new_source_id, new_variant_id
from pulse.shared.models import MediaItem


@dataclass(frozen=True, slots=True)
class ContentRecord:
    """契约 §6 ``contents`` 表。"""

    id: str
    brief_id: str
    title: str
    body_seed: str = ""
    brand_guide_id: str | None = None


@dataclass(frozen=True, slots=True)
class VariantRecord:
    """契约 §6 ``variants`` 表。"""

    id: str
    source_id: str
    platform: Platform
    status: VariantStatus = VariantStatus.DRAFT
    fields: dict[str, Any] = field(default_factory=dict)
    ai_score: float | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "platform": self.platform.value,
            "status": self.status.value,
            "fields": dict(self.fields),
            "ai_score": self.ai_score,
        }


@dataclass(frozen=True, slots=True)
class MediaAssetRecord:
    """契约 §6 ``media_assets`` 表（域内视图）。"""

    id: str
    variant_id: str
    kind: str
    oss_key: str
    source: str
    license_status: str
    meta: dict[str, Any] = field(default_factory=dict)


class ContentStore:
    """内存版内容库。线程内使用；跨进程由上层换成真实库。"""

    def __init__(self) -> None:
        self._contents: dict[str, ContentRecord] = {}
        self._variants: dict[str, VariantRecord] = {}
        self._media: list[MediaAssetRecord] = []

    # ---------- contents ----------

    def create_content(
        self,
        *,
        brief_id: str,
        title: str,
        body_seed: str = "",
        brand_guide_id: str | None = None,
        source_id: str | None = None,
    ) -> ContentRecord:
        record = ContentRecord(
            id=source_id or new_source_id(),
            brief_id=brief_id,
            title=title,
            body_seed=body_seed,
            brand_guide_id=brand_guide_id,
        )
        self._contents[record.id] = record
        return record

    def get_content(self, source_id: str) -> ContentRecord:
        found = self._contents.get(source_id)
        if found is None:
            raise ContentNotFoundError(
                f"找不到内容 {source_id!r}", details={"source_id": source_id}
            )
        return found

    def list_contents(self) -> tuple[ContentRecord, ...]:
        return tuple(self._contents.values())

    def content_by_brief(self, brief_id: str) -> ContentRecord | None:
        for record in self._contents.values():
            if record.brief_id == brief_id:
                return record
        return None

    # ---------- variants ----------

    def create_variant(
        self,
        *,
        source_id: str,
        platform: Platform,
        fields: dict[str, Any] | None = None,
        status: VariantStatus = VariantStatus.DRAFT,
        ai_score: float | None = None,
        variant_id: str | None = None,
    ) -> VariantRecord:
        record = VariantRecord(
            id=variant_id or new_variant_id(),
            source_id=source_id,
            platform=platform,
            status=status,
            fields=dict(fields or {}),
            ai_score=ai_score,
        )
        self._variants[record.id] = record
        return record

    def get_variant(self, variant_id: str) -> VariantRecord:
        found = self._variants.get(variant_id)
        if found is None:
            raise ContentNotFoundError(
                f"找不到变体 {variant_id!r}", details={"variant_id": variant_id}
            )
        return found

    def update_variant_fields(self, variant_id: str, fields: dict[str, Any]) -> VariantRecord:
        """整体替换变体的 ``fields``（生成完成后由内容域回写）。"""
        current = self.get_variant(variant_id)
        updated = VariantRecord(
            id=current.id,
            source_id=current.source_id,
            platform=current.platform,
            status=current.status,
            fields=dict(fields),
            ai_score=current.ai_score,
        )
        self._variants[variant_id] = updated
        return updated

    def variants_for_source(self, source_id: str) -> tuple[VariantRecord, ...]:
        return tuple(item for item in self._variants.values() if item.source_id == source_id)

    def list_variants(self) -> tuple[VariantRecord, ...]:
        return tuple(self._variants.values())

    # ---------- media_assets ----------

    def attach_media(
        self,
        variant_id: str,
        items: Iterable[MediaItem],
        *,
        oss_key_for: Any = None,
    ) -> tuple[MediaAssetRecord, ...]:
        """把素材登记到变体上（``license_status`` 原样带过来，供版权审计）。"""
        self.get_variant(variant_id)  # 变体不存在时直接报错，避免挂空
        created: list[MediaAssetRecord] = []
        for index, item in enumerate(items, start=1):
            kind = item.kind.value if hasattr(item.kind, "value") else str(item.kind)
            license_status = (
                item.license_status.value
                if hasattr(item.license_status, "value")
                else str(item.license_status)
            )
            oss_key = oss_key_for(item) if callable(oss_key_for) else item.url
            record = MediaAssetRecord(
                id=f"{variant_id}_m{index}",
                variant_id=variant_id,
                kind=kind,
                oss_key=str(oss_key),
                source="owned",
                license_status=license_status,
                meta={
                    "mime": item.mime,
                    "width": item.width,
                    "height": item.height,
                    "duration_s": item.duration_s,
                },
            )
            self._media.append(record)
            created.append(record)
        return tuple(created)

    def media_for_variant(self, variant_id: str) -> tuple[MediaAssetRecord, ...]:
        return tuple(item for item in self._media if item.variant_id == variant_id)
