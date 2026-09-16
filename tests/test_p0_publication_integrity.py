"""P0：发布时间不伪造、可用时间落位、membership区间闭合。"""
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ingestion.base import CollectionAdapter, JobRunner
from storage.ingest import write_records
from storage.models import Asset, AssetMembership, Base, FactObservation


class FakeAdapter(CollectionAdapter):
    source_id = "test.p0"
    min_interval = 0

    def discover(self):
        return ["2026-09-15"]

    def fetch(self, item):
        return {"payload": "x", "url": "test://x", "content_type": "text/plain"}

    def parse(self, item, raw):
        return [{"kind": "fact", "subject_key": "market:X", "asset_key": None,
                 "metric": "x", "value": 1, "unit": "count",
                 "effective_at": item, "published_at": item,
                 "quality_status": "valid"}]


def test_unverified_published_at_is_cleared_and_available_at_set(tmp_path: Path):
    url = f"sqlite:///{tmp_path/'p0.db'}"
    runner = JobRunner(url, tmp_path / "raw")
    runner.run(FakeAdapter(), job_key="p0")
    with Session(runner.engine) as s:
        row = s.execute(select(FactObservation)).scalar_one()
        assert row.published_at is None
        assert row.available_at and "T" in row.available_at


def test_membership_change_closes_old_interval(tmp_path: Path):
    eng = create_engine(f"sqlite:///{tmp_path/'m.db'}")
    Base.metadata.create_all(eng)
    base = {"kind": "membership", "classification_version": "em_industry_v1",
            "effective_from": "2026-09-15", "weight": None}
    with Session(eng) as s:
        write_records(s, [{**base, "asset_key": "stock:600001", "group_key": "em_board:A"}])
        write_records(s, [{**base, "effective_from": "2026-09-16",
                           "asset_key": "stock:600001", "group_key": "em_board:B"}])
        rows = s.execute(select(AssetMembership).order_by(AssetMembership.id)).scalars().all()
        assert len(rows) == 2
        assert rows[0].effective_to == "2026-09-16"
        assert rows[1].effective_to is None
