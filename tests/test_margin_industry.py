"""行业两融聚合：日期感知映射、unknown计数、规则派生available_at。"""
import pandas as pd
from sqlalchemy.orm import Session

from ingestion.adapters.margin_detail import industry_records, next_session_0915
from storage.ingest import write_records
from storage.models import init_db


def _seed_mapping(tmp_path):
    eng = init_db(f"sqlite:///{tmp_path/'m.db'}")
    with Session(eng) as s:
        write_records(s, [
            {"kind": "asset_info", "asset_key": "sw_l1:801080", "asset_type": "industry", "name": "电子"},
            {"kind": "membership", "asset_key": "stock:600001", "group_key": "sw_l1:801080",
             "effective_from": "2021-12-13", "classification_version": "sw_l1_2021", "weight": None},
        ])
        s.commit()
    return eng


def test_aggregation_and_unknown_count(tmp_path):
    detail = pd.DataFrame({"code": ["600001", "600999"],  # 600999不在映射
                           "margin_balance": [100.0, 7.0],
                           "margin_buy": [10.0, 1.0]})
    records, diag = industry_records(detail, {"600001": "sw_l1:801080"}, "2026-09-15")
    by = {(r["subject_key"], r["metric"]): r for r in records}
    assert by[("sw_l1:801080", "margin_balance")]["value"] == 100.0
    assert diag["unknown_count"] == 1
    assert all(r["available_at"] == "2026-09-16 09:15:00" for r in records)


def test_transfer_day_uses_new_industry(tmp_path):
    # 换组股在切换日应属新行业：membership_map 的闭区间语义。
    from ingestion.adapters.margin_detail import membership_map
    eng = _seed_mapping(tmp_path)
    with Session(eng) as s:
        write_records(s, [
            {"kind": "asset_info", "asset_key": "sw_l1:801010", "asset_type": "industry", "name": "农林牧渔"},
            {"kind": "membership", "asset_key": "stock:600001", "group_key": "sw_l1:801010",
             "effective_from": "2026-07-02", "classification_version": "sw_l1_2021", "weight": None},
        ])
        s.commit()
        assert membership_map(s, "2026-07-01")["600001"] == "sw_l1:801080"
        assert membership_map(s, "2026-07-02")["600001"] == "sw_l1:801010"


def test_next_session_skips_weekend():
    assert next_session_0915("2026-09-18") == "2026-09-21 09:15:00"  # 周五→周一
