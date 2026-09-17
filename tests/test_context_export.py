"""context_export 契约：strict-PIT 排除、ETF 同日成对、成员区间全量导出。"""
import sys
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.context_export import (  # noqa: E402
    etf_new_exposure, export_facts, export_membership_history)
from storage.ingest import write_records  # noqa: E402
from storage.models import init_db  # noqa: E402


def _facts_db(tmp_path):
    eng = init_db(f"sqlite:///{tmp_path/'f.db'}")
    rows = [
        # (available_at, effective_at, metric, value)
        ("2025-01-02T08:00:00", "2025-01-01", "index_close", 3000.0),
        (None, "2025-01-02", "index_close", 3001.0),          # 未知可用性→排除
        ("2025-01-10T08:00:00", "2025-01-03", "index_close", 3002.0),  # 晚于 as_of→排除
        ("2025-01-03T08:00:00", "2025-01-02", "etf_total_shares", 100.0),
        ("2025-01-03T08:00:00", "2025-01-02", "etf_nav", 1.5),
        ("2025-01-04T08:00:00", "2025-01-03", "etf_total_shares", 110.0),  # nav 缺→不成对
    ]
    with Session(eng) as s:
        write_records(s, [dict(
            kind="fact", source_id="t", artifact_id=f"t:{i}",
            subject_key="idx:sh000300" if metric == "index_close" else "etf:510300",
            asset_key=None, metric=metric, value=val, unit="yuan",
            effective_at=eff, published_at=None, available_at=av,
            quality_status="valid") for i, (av, eff, metric, val) in enumerate(rows)])
        s.commit()
    return eng


def test_export_facts_strict_pit(tmp_path):
    eng = _facts_db(tmp_path)
    with Session(eng) as s:
        out = export_facts(s, ("index_close", "etf_total_shares", "etf_nav"),
                           as_of="2025-01-05T00:00:00")
    assert len(out) == 4  # 排除 unknown-available 与 future-available；缺nav的share仍导出
    assert set(out["metric"]) == {"index_close", "etf_total_shares", "etf_nav"}
    assert "2025-01-03" not in set(out[out.metric == "index_close"]["effective_at"])


def test_etf_new_exposure_pairs_same_day_only(tmp_path):
    eng = _facts_db(tmp_path)
    with Session(eng) as s:
        facts = export_facts(s, ("etf_total_shares", "etf_nav"), as_of="2025-01-05")
    exp = etf_new_exposure(facts)
    assert list(exp["effective_at"]) == ["2025-01-02"]
    assert exp["new_exposure_yuan"].iloc[0] == pytest.approx(150.0)


def test_membership_history_exports_closed_intervals(tmp_path):
    eng = init_db(f"sqlite:///{tmp_path/'m.db'}")
    base = {"kind": "membership", "classification_version": "sw_l1_2021", "weight": None}
    with Session(eng) as s:
        write_records(s, [
            {"kind": "asset_info", "asset_key": "sw_l1:801080", "asset_type": "industry", "name": "电子"},
            {"kind": "asset_info", "asset_key": "sw_l1:801010", "asset_type": "industry", "name": "农林牧渔"},
            {**base, "asset_key": "stock:600001", "group_key": "sw_l1:801080", "effective_from": "2021-12-13"},
            {**base, "asset_key": "stock:600001", "group_key": "sw_l1:801010", "effective_from": "2025-05-13"},
        ])
        s.commit()
    with Session(eng) as s:
        hist = export_membership_history(s)
    assert len(hist) == 2
    old = hist[hist.industry_code == "801080"].iloc[0]
    assert old["effective_to"] == "2025-05-13"  # 换组闭区间原样导出
    assert hist["effective_to"].isna().sum() == 1  # 当前区间开放
