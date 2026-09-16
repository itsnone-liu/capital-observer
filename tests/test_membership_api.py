"""申万L1映射端点：生效区间、换组闭区间、未知不猜。"""
from sqlalchemy.orm import Session

from api.app import app, engine
from fastapi.testclient import TestClient
from storage.ingest import write_records
from storage.models import Base, AssetMembership, init_db


def _seed(tmp_path, records):
    eng = init_db(f"sqlite:///{tmp_path/'m.db'}")
    with Session(eng) as s:
        stats = write_records(s, records)
        s.commit()
    return eng, stats


def test_transfer_closes_old_and_lookup_is_date_aware(tmp_path):
    base = {"kind": "membership", "classification_version": "sw_l1_2021", "weight": None}
    _, stats = _seed(tmp_path, [
        {"kind": "asset_info", "asset_key": "sw_l1:801010", "asset_type": "industry", "name": "农林牧渔"},
        {"kind": "asset_info", "asset_key": "sw_l1:801080", "asset_type": "industry", "name": "电子"},
        {**base, "asset_key": "stock:600001", "group_key": "sw_l1:801080",
         "effective_from": "2021-12-13"},
        {**base, "asset_key": "stock:600001", "group_key": "sw_l1:801010",
         "effective_from": "2025-05-13"},
    ])
    assert stats["membership_closed"] == 1
    with Session(init_db(f"sqlite:///{tmp_path/'m.db'}")) as s:
        rows = s.query(AssetMembership).order_by(AssetMembership.id).all()
        assert rows[0].effective_to == "2025-05-13"
        assert rows[1].effective_to is None


def test_api_returns_unknown_for_pre_membership_date(tmp_path):
    import api.app as appmod
    _seed(tmp_path, [
        {"kind": "asset_info", "asset_key": "sw_l1:801080", "asset_type": "industry", "name": "电子"},
        {"kind": "membership", "asset_key": "stock:600001", "group_key": "sw_l1:801080",
         "effective_from": "2025-05-13", "classification_version": "sw_l1_2021", "weight": None},
    ])
    from sqlalchemy import create_engine
    appmod.engine = create_engine(f"sqlite:///{tmp_path/'m.db'}", future=True)
    client = TestClient(app)
    before = client.get("/api/v1/membership", params={"as_of": "2025-05-12"}).json()
    on = client.get("/api/v1/membership", params={"as_of": "2025-05-13"}).json()
    assert "600001" not in before["mapping"]
    assert on["mapping"]["600001"]["industry_code"] == "801080"
