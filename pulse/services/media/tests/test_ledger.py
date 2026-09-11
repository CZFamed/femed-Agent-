"""使用台账测试：幂等、冷却窗口、历史查询。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pulse.services.media.config import BRAND_NAME
from pulse.services.media.ledger import RecallLedger

NOW = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)


def test_mark_used_is_idempotent_per_content(tmp_path) -> None:
    ledger = RecallLedger(tmp_path / "ledger.sqlite3")
    assert ledger.mark_used("img_a", brand=BRAND_NAME, content_id="var_1", used_at=NOW) is True
    assert ledger.mark_used("img_a", brand=BRAND_NAME, content_id="var_1", used_at=NOW) is False
    assert ledger.usage_count("img_a", brand=BRAND_NAME) == 1


def test_last_used_tracks_latest_event(tmp_path) -> None:
    ledger = RecallLedger(tmp_path / "ledger.sqlite3")
    ledger.mark_used("img_a", brand=BRAND_NAME, used_at=NOW - timedelta(days=5))
    ledger.mark_used("img_a", brand=BRAND_NAME, content_id="var_2", used_at=NOW)
    assert ledger.last_used_at("img_a", brand=BRAND_NAME) == NOW


def test_used_within_window_and_brand_isolation(tmp_path) -> None:
    ledger = RecallLedger(tmp_path / "ledger.sqlite3")
    ledger.mark_used("img_a", brand=BRAND_NAME, used_at=NOW - timedelta(days=3))
    assert ledger.used_within("img_a", brand=BRAND_NAME, window=timedelta(days=15), now=NOW)
    assert not ledger.used_within("img_a", brand="其他品牌", window=timedelta(days=15), now=NOW)


def test_history_returns_latest_first(tmp_path) -> None:
    ledger = RecallLedger(tmp_path / "ledger.sqlite3")
    ledger.mark_used("img_a", brand=BRAND_NAME, used_at=NOW - timedelta(days=2))
    ledger.mark_used("img_b", brand=BRAND_NAME, used_at=NOW)
    history = ledger.history(brand=BRAND_NAME)
    assert [row["asset_id"] for row in history] == ["img_b", "img_a"]
