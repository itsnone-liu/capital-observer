"""Smoke test: SinaIndexBarsAdapter end-to-end through the sec.4 contract.

Run: python3 -m tests.test_ingestion_smoke  (network required; read-only upstream)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from ingestion.adapters.sina_index import SinaIndexBarsAdapter
from ingestion.base import JobRunner
from storage.models import Base, FactObservation, JobRun, RawArtifact


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="capobs_smoke_"))
    db = tmp / "smoke.db"
    url = f"sqlite:///{db}"
    runner = JobRunner(url, tmp / "raw")

    ad = SinaIndexBarsAdapter(symbols=["sh000300", "sz399006"], start="2025-01-01")
    s1 = runner.run(ad, job_key="smoke:sina_index:2026-09-10")
    print("run1:", {k: s1[k] for k in ("ok", "failed", "status")}, "writes:", s1.get("writes"))
    assert s1["status"] == "succeeded", s1

    eng = create_engine(url)
    with Session(eng) as s:
        n_art = s.execute(select(func.count()).select_from(RawArtifact)).scalar()
        n_fact = s.execute(select(func.count()).select_from(FactObservation)).scalar()
        jr = s.execute(select(JobRun)).scalar_one()
        assert jr.status == "succeeded"
        # public replay query (published_at ordering) sanity
        q = (select(FactObservation.effective_at, FactObservation.value)
             .where(FactObservation.subject_key == "index:sh000300",
                    FactObservation.metric == "index_close")
             .order_by(FactObservation.effective_at.desc()).limit(1))
        last = s.execute(q).first()
        print(f"artifacts={n_art} facts={n_fact} last_close(sh000300)={last}")
        assert n_art >= 2 and n_fact > 500 and last is not None

    # idempotent rerun: no duplicate facts, cursor skips
    s2 = runner.run(ad, job_key="smoke:sina_index:2026-09-10")
    print("run2 (same key):", s2)
    assert s2.get("skipped") == "running" or s2["status"] in ("succeeded", "partial")

    with Session(eng) as s:
        n_fact2 = s.execute(select(func.count()).select_from(FactObservation)).scalar()
    assert n_fact2 == n_fact, f"duplicated facts: {n_fact} -> {n_fact2}"
    print("SMOKE OK: artifacts+facts persisted, idempotent rerun verified")


if __name__ == "__main__":
    main()
