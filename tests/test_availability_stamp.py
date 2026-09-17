"""保守可用性回补：口径分档、幂等、范围外不动。"""
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.stamp_background_availability import (  # noqa: E402
    TIER_A, TIER_B, stamp)
from storage.ingest import write_records  # noqa: E402
from storage.models import FactObservation, init_db  # noqa: E402


def _seed(tmp_path):
    eng = init_db(f"sqlite:///{tmp_path/'s.db'}")
    metrics = [("index_close", "2025-01-02"), ("etf_total_shares", "2025-01-02"),
               ("margin_fin_balance", "2025-01-02"), ("sf_main_net", "2025-01-02")]
    with Session(eng) as s:
        write_records(s, [dict(
            kind="fact", source_id="t", artifact_id=f"t:{m}",
            subject_key="x", asset_key=None, metric=m, value=1.0, unit="yuan",
            effective_at=d, published_at=None,
            available_at="2026-01-01T08:00:00" if m == "sf_main_net" else None,
            quality_status="valid") for m, d in metrics])
        s.commit()
    return eng


def test_stamp_tiers_and_exclusions(tmp_path):
    eng = _seed(tmp_path)
    with Session(eng) as s:
        dry = stamp(s, apply=False)
        assert dry["stamped"]["A"]["would_stamp"] == 1
        assert dry["stamped"]["B"]["would_stamp"] == 1
        out = stamp(s, apply=True)
        assert out["stamped"]["A"]["rows"] == 1 and out["stamped"]["B"]["rows"] == 1
        again = stamp(s, apply=True)  # 幂等
        assert again["stamped"]["A"]["rows"] == 0 and again["stamped"]["B"]["rows"] == 0
        rows = {r.metric: r.available_at for r in s.query(FactObservation).all()}
    assert rows["index_close"] == "2025-01-02T15:30:00"
    assert rows["etf_total_shares"] == "2025-01-02T22:00:00"
    assert rows["margin_fin_balance"] is None      # 范围外不猜
    assert rows["sf_main_net"] == "2026-01-01T08:00:00"  # 已带戳不动


def test_tier_metrics_disjoint():
    assert not set(TIER_A) & set(TIER_B)
