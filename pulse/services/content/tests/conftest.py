"""内容生产域测试的公共夹具。"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from pulse.services.media.catalog import MediaAsset
from pulse.services.media.config import RecallConfig
from pulse.services.media.ledger import RecallLedger
from pulse.services.media.policy import RecallPolicy


def make_asset(
    file_name: str,
    *,
    process: str = "加工件",
    sub_process: str = "壳体",
    summary: str = "",
    keywords: tuple[str, ...] = (),
    is_legacy: bool = True,
) -> MediaAsset:
    """造一条素材（不使用任何真实素材库文件）。"""
    return MediaAsset(
        asset_id=f"a_{file_name}",
        file_name=file_name,
        process=process,
        sub_process=sub_process,
        source_path=f"../菲美得产品图片/{process}/{sub_process}/{file_name}",
        description_path=f"RAG知识库/图片描述/{process}/{sub_process}/{file_name}.md",
        summary=summary,
        keywords=keywords,
        added_at=None,
        is_legacy=is_legacy,
    )


@pytest.fixture()
def asset_pool() -> list[MediaAsset]:
    """四个位次都能精准命中的最小素材池。"""
    return [
        make_asset(
            "IMG_0001.jpg",
            summary="精加工阀体成品与数控加工中心同框",
            keywords=("加工面", "加工中心", "成品"),
        ),
        make_asset(
            "IMG_0002.jpg",
            summary="大型数控镗铣床加工厚壁箱体",
            keywords=("镗床", "数控", "箱体", "机加工"),
        ),
        make_asset(
            "IMG_0003.jpg",
            process="生产流程",
            sub_process="扫描",
            summary="大型箱体铸件三维扫描尺寸检测",
            keywords=("扫描", "三维扫描", "尺寸检测"),
        ),
        make_asset(
            "IMG_0004.jpg",
            process="铸件",
            sub_process="箱体_支座",
            summary="托盘上缠膜待发的成品铸件",
            keywords=("托盘", "包装", "待发"),
        ),
    ]


@pytest.fixture()
def ledger(tmp_path) -> RecallLedger:
    return RecallLedger(tmp_path / "ledger.sqlite3")


@pytest.fixture()
def policy(ledger: RecallLedger) -> RecallPolicy:
    return RecallPolicy(
        ledger,
        RecallConfig(cooldown_days=15, top_k=1),
        rng=random.Random(20260917),
    )


@pytest.fixture()
def fixed_now() -> datetime:
    return datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
