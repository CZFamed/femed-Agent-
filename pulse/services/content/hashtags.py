"""话题标签库（所有者：A1，任务包 W1-A1-4）。

两条硬约束，直接来自契约与派工单：

1. 标签**必须已含 ``#`` 且不含空格**（契约 §2 的 ``UnifiedPost.hashtags`` 校验会拦）。
2. 只能从**白名单**里取词——标签跑偏是 B2B 内容最常见的低级错误
   （例如给铸件贴 ``#handmade``），一旦跑偏，平台的推荐人群与目标客户完全不重合。

词表按 B2B 工业铸件出海场景整理，依据《菲美得_四平台推荐风格与方式报告_v1》§3 的
分平台标签建议（LinkedIn 3–5 个品类词、VK 2–5 个可含西里尔字母、Facebook 1–2 个）。
不在这里编造客户名、认证名或产品参数。
"""

from __future__ import annotations

from typing import Iterable

from pulse.services.content.errors import UnknownHashtagError

#: 品牌词（每帖最多一个，放最后）
BRAND_TAGS: tuple[str, ...] = ("#famed", "#cangzhoufamed")

#: 品类词白名单：类别 key → 可用标签
CATEGORY_TAGS: dict[str, tuple[str, ...]] = {
    "casting": ("#casting", "#foundry", "#ironcasting", "#lostfoam", "#greyiron", "#ductileiron"),
    "machining": ("#machining", "#cncmachining", "#machinedparts", "#boringmilling"),
    "machine_tools": ("#machinetools", "#machinetool", "#machinebed", "#cnc"),
    "construction_machinery": ("#constructionmachinery", "#counterweight", "#gearboxhousing"),
    "pump_valve": ("#valvebody", "#pumpbody", "#valvecasting", "#pipefitting"),
    "quality": ("#qualitycontrol", "#ndt", "#cmminspection", "#inspection"),
    "logistics": ("#exportpacking", "#seafreight", "#shipping"),
    "operations": ("#manufacturing", "#supplychain", "#sourcing", "#oemsupplier"),
}

#: 地区词（默认不贴市场标签；需要"看得到我们在服务谁"时再打开）
REGION_TAGS: dict[str, tuple[str, ...]] = {
    "india": ("#india",),
    "usa": ("#usa",),
    "taiwan": ("#taiwan",),
    "global": ("#globalsourcing",),
}

#: 兜底标签：当调用方没给类别时，保证仍能凑够最小数量
FALLBACK_TAGS: tuple[str, ...] = ("#casting", "#machining", "#oemparts", "#manufacturing")

#: 全部允许的标签（白名单校验用）
ALLOWED_TAGS: frozenset[str] = frozenset(
    tag
    for group in (BRAND_TAGS, *CATEGORY_TAGS.values(), *REGION_TAGS.values(), FALLBACK_TAGS)
    for tag in group
)


def normalize_tag(raw: str) -> str:
    """统一成 ``#小写无空格`` 形式。"""
    text = str(raw or "").strip().lstrip("#").lower()
    return f"#{text}" if text else ""


def ensure_allowed(tags: Iterable[str]) -> None:
    """白名单校验；越界即抛 ``UnknownHashtagError``（列出全部越界项）。"""
    bad = sorted({tag for tag in tags if tag not in ALLOWED_TAGS})
    if bad:
        raise UnknownHashtagError(
            "以下话题标签不在白名单内：" + "、".join(bad),
            details={"not_allowed": bad, "allowed": sorted(ALLOWED_TAGS)},
        )


def validate_hashtags(tags: Iterable[str], *, min_count: int = 0, max_count: int | None = None) -> list[str]:
    """返回错误消息列表（空 = 通过）。用于生成前的最后一道闸。"""
    items = list(tags)
    errors: list[str] = []
    for tag in items:
        if not tag.startswith("#"):
            errors.append(f"标签必须以 # 开头：{tag!r}")
        if " " in tag:
            errors.append(f"标签不能含空格：{tag!r}")
    normalized = [normalize_tag(tag) for tag in items]
    if len(normalized) != len(set(normalized)):
        errors.append("标签存在重复")
    if len(items) < min_count:
        errors.append(f"标签数量不足：{len(items)} 个（至少 {min_count} 个）")
    if max_count is not None and len(items) > max_count:
        errors.append(f"标签数量超限：{len(items)} 个（最多 {max_count} 个）")
    unknown = sorted({tag for tag in items if tag not in ALLOWED_TAGS})
    if unknown:
        errors.append("标签不在白名单内：" + "、".join(unknown))
    return errors


def compose_hashtags(
    *,
    categories: Iterable[str] = (),
    regions: Iterable[str] = (),
    extra: Iterable[str] = (),
    max_count: int = 5,
    brand_tag: str | None = BRAND_TAGS[0],
) -> tuple[str, ...]:
    """按"品类词 → 地区词 → 品牌词"的顺序组标签，去重后截到 ``max_count``。

    Args:
        categories: ``CATEGORY_TAGS`` 的 key（未知 key 会被忽略并在返回值里体现为数量不足）。
        regions: ``REGION_TAGS`` 的 key。
        extra: 额外标签；**仍受白名单约束**（越界抛错，不做静默丢弃）。
        max_count: 上限，由平台规格给出（LinkedIn 5、Facebook 2、VK 5 …）。
        brand_tag: 品牌词，传 ``None`` 表示不贴品牌词。

    Returns:
        已含 ``#``、不含空格的标签元组（顺序稳定，便于快照测试）。
    """
    if max_count < 0:
        raise ValueError(f"max_count 不能为负：{max_count}")

    ordered: list[str] = []
    for key in categories:
        ordered.extend(CATEGORY_TAGS.get(str(key).strip().lower(), ()))
    for key in regions:
        ordered.extend(REGION_TAGS.get(str(key).strip().lower(), ()))
    # 越界的额外标签**直接报错**，不静默丢弃——被 max_count 截掉的那几个
    # 根本到不了校验那一步，静默丢弃等于把问题藏起来
    extra_tags = tuple(normalize_tag(tag) for tag in extra)
    ensure_allowed(tag for tag in extra_tags if tag)
    ordered.extend(extra_tags)
    ordered.extend(FALLBACK_TAGS)
    if brand_tag:
        ordered.append(normalize_tag(brand_tag))

    result: list[str] = []
    for tag in ordered:
        if not tag or tag in result:
            continue
        result.append(tag)
        if len(result) >= max_count:
            break

    ensure_allowed(result)
    return tuple(result)
