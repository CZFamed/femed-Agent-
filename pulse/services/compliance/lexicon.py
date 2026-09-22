"""规则数据装载（所有者：A4，任务包 W2-A4-3）。

词库、平台口径、禁用词、制裁清单**全部外置在 `data/` 下的 JSON 里**，代码只做匹配与判定。
清单和法律口径会变，改数据不该改代码（派工单 §5.2）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from pulse.services.compliance.errors import RuleConfigError

DATA_DIR = Path(__file__).resolve().parent / "data"

SENSITIVE_WORDS_FILE = "sensitive_words.json"
PLATFORM_POLICIES_FILE = "platform_policies.json"
SANCTIONS_LISTS_FILE = "sanctions_lists.json"


def load_json(name: str) -> dict[str, Any]:
    """读取数据文件；缺失或损坏时抛 `RuleConfigError`（不要静默用空数据）。"""
    path = DATA_DIR / name
    if not path.is_file():
        raise RuleConfigError(f"规则数据文件缺失：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # noqa: PERF203 - 需要给出文件名与位置
        raise RuleConfigError(f"规则数据文件不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise RuleConfigError(f"规则数据文件的顶层必须是对象：{path}")
    return payload


@dataclass(frozen=True, slots=True)
class SensitiveTerm:
    term: str
    reason: str
    lang: str = ""


@dataclass(frozen=True, slots=True)
class SensitiveLexicon:
    """敏感词表：block 级与 warn 级分开。"""

    block: tuple[SensitiveTerm, ...] = ()
    warn: tuple[SensitiveTerm, ...] = ()
    updated: str = "TODO(need-real-data)"

    def terms(self, severity: str) -> tuple[SensitiveTerm, ...]:
        return self.block if str(severity).lower() == "block" else self.warn


@lru_cache(maxsize=1)
def sensitive_lexicon() -> SensitiveLexicon:
    raw = load_json(SENSITIVE_WORDS_FILE)

    def parse(key: str) -> tuple[SensitiveTerm, ...]:
        items = raw.get(key) or []
        if not isinstance(items, list):
            raise RuleConfigError(f"{SENSITIVE_WORDS_FILE} 的 {key} 必须是数组")
        parsed: list[SensitiveTerm] = []
        for item in items:
            if not isinstance(item, Mapping) or not item.get("term"):
                raise RuleConfigError(f"{SENSITIVE_WORDS_FILE} 的 {key} 条目缺少 term：{item!r}")
            parsed.append(
                SensitiveTerm(
                    term=str(item["term"]),
                    reason=str(item.get("reason") or ""),
                    lang=str(item.get("lang") or ""),
                )
            )
        return tuple(parsed)

    return SensitiveLexicon(
        block=parse("block"), warn=parse("warn"), updated=str(raw.get("_updated") or "")
    )


@dataclass(frozen=True, slots=True)
class PlatformPolicy:
    platform: str
    min_chars: int | None = None
    max_chars: int | None = None
    hashtag_min: int = 0
    hashtag_max: int = 0
    emoji_max: int = 0


@dataclass(frozen=True, slots=True)
class ContentPolicies:
    platforms: Mapping[str, PlatformPolicy]
    banned_block: tuple[str, ...] = ()
    banned_warn: tuple[str, ...] = ()
    flavor_openers: tuple[str, ...] = ()
    max_repeated_exclamation: int = 2
    updated: str = ""

    def platform(self, key: str) -> PlatformPolicy | None:
        return self.platforms.get(str(key or "").strip().lower())


@lru_cache(maxsize=1)
def content_policies() -> ContentPolicies:
    raw = load_json(PLATFORM_POLICIES_FILE)
    platforms_raw = raw.get("platforms") or {}
    if not isinstance(platforms_raw, Mapping):
        raise RuleConfigError(f"{PLATFORM_POLICIES_FILE} 的 platforms 必须是对象")
    platforms: dict[str, PlatformPolicy] = {}
    for key, value in platforms_raw.items():
        if not isinstance(value, Mapping):
            raise RuleConfigError(f"{PLATFORM_POLICIES_FILE} 的 platforms.{key} 必须是对象")
        platforms[str(key).lower()] = PlatformPolicy(
            platform=str(key).lower(),
            min_chars=value.get("min_chars"),
            max_chars=value.get("max_chars"),
            hashtag_min=int(value.get("hashtag_min") or 0),
            hashtag_max=int(value.get("hashtag_max") or 0),
            emoji_max=int(value.get("emoji_max") or 0),
        )
    banned = raw.get("banned_phrases") or {}
    flavor = raw.get("machine_flavor") or {}
    return ContentPolicies(
        platforms=platforms,
        banned_block=tuple(str(item) for item in (banned.get("block") or ())),
        banned_warn=tuple(str(item) for item in (banned.get("warn") or ())),
        flavor_openers=tuple(str(item) for item in (flavor.get("openers") or ())),
        max_repeated_exclamation=int(flavor.get("max_repeated_exclamation") or 2),
        updated=str(raw.get("_updated") or ""),
    )


def clear_caches() -> None:
    """测试用：数据文件改动后清缓存。"""
    sensitive_lexicon.cache_clear()
    content_policies.cache_clear()


def iter_terms(lexicon: SensitiveLexicon) -> Iterable[SensitiveTerm]:
    yield from lexicon.block
    yield from lexicon.warn
