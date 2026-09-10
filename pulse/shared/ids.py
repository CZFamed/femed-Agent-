"""ID 生成（契约 §6 命名规范）。

前缀约定：acct_ / b_ / src_ / var_ / sched_ / job_ / up_
ULID 风格：时间有序，便于按创建顺序排序与分页。
"""

from __future__ import annotations

import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    """26 字符、按时间有序的 ULID（不依赖第三方库）。"""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    ts_part = "".join(_CROCKFORD[(ts >> shift) & 0x1F] for shift in range(45, -1, -5))
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    rand_part = "".join(_CROCKFORD[(rand >> shift) & 0x1F] for shift in range(75, -1, -5))
    return ts_part + rand_part


def _prefixed(prefix: str) -> str:
    return f"{prefix}{_ulid()}"


def new_account_id() -> str:
    return _prefixed("acct_")


def new_brief_id() -> str:
    return _prefixed("b_")


def new_source_id() -> str:
    return _prefixed("src_")


def new_variant_id() -> str:
    return _prefixed("var_")


def new_schedule_id() -> str:
    return _prefixed("sched_")


def new_job_id() -> str:
    return _prefixed("job_")


def new_unified_post_id() -> str:
    return _prefixed("up_")
