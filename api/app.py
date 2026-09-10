"""FastAPI app (design sec.11): facts-first endpoints, every response carries
data/as_of/coverage/denominator(run_id comes with P3 estimates)/warnings.

Run: python3 -m uvicorn api.app:app --port 8120
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from models.observations import (etf_size_decomposition, margin_view,  # noqa: E402
                                 market_overview, sector_ranking)
from storage.models import FactObservation, JobRun  # noqa: E402

DB_URL = f"sqlite:///{ROOT / 'data' / 'capobs.db'}"
engine = create_engine(DB_URL, future=True, connect_args={"check_same_thread": False})

app = FastAPI(title="capital-observer", version="0.1.0-p2")

from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/dashboard", StaticFiles(directory=str(ROOT / "dashboard"), html=True), name="dashboard")

INDICES = ["sh000300", "sh000016", "sh000905", "sh000852", "sh000912",
           "sz399006", "sh000688"]


def _session():
    return Session(engine, future=True)


@app.get("/api/v1/market/overview")
def get_overview(as_of: str | None = Query(None, description="YYYY-MM-DD replay")):
    with _session() as s:
        v = market_overview(s, INDICES)
    if as_of:
        v["warnings"].append(f"as_of={as_of} 重放未在 v1 实现，返回最新")
    v["published_cutoff"] = v["as_of"]
    return v


@app.get("/api/v1/sectors/ranking")
def get_sectors(sort: str = Query("ret_20d", pattern="^ret_(5|20|60)d$"),
                as_of: str | None = None):
    with _session() as s:
        v = sector_ranking(s, as_of=as_of)
    if sort != "ret_20d":
        v["data"] = sorted(v["data"], key=lambda d: d.get(sort) or -9, reverse=True)
    v["published_cutoff"] = v["as_of"]
    return v


@app.get("/api/v1/margin/summary")
def get_margin():
    with _session() as s:
        v = margin_view(s)
    v["published_cutoff"] = v["as_of"]
    return v


@app.get("/api/v1/etf/{code}/structure")
def get_etf_structure(code: str):
    if not (len(code) == 6 and code.isdigit()):
        raise HTTPException(400, "code must be 6-digit fund code")
    with _session() as s:
        v = etf_size_decomposition(s, code)
    v["published_cutoff"] = v["as_of"]
    return v


@app.get("/api/v1/margin/history")
def get_margin_history(days: int = Query(120, ge=10, le=2000)):
    import pandas as pd
    with _session() as s:
        rows = s.execute(
            select(FactObservation.effective_at, FactObservation.metric,
                   FactObservation.value)
            .where(FactObservation.subject_key == "market:SSE",
                   FactObservation.metric.in_(("margin_fin_balance", "margin_fin_buy")),
                   FactObservation.quality_status == "valid")
            .order_by(FactObservation.effective_at)).all()
    if not rows:
        raise HTTPException(503, "no margin data")
    df = pd.DataFrame(rows, columns=["date", "metric", "value"])
    df = df.groupby(["date", "metric"], as_index=False).agg(value=("value", "last"))
    piv = df.pivot(index="date", columns="metric", values="value").reset_index()
    piv = piv.tail(days)
    return {
        "data": {"dates": piv["date"].tolist(),
                 "fin_balance": piv.get("margin_fin_balance", pd.Series(dtype=float)).tolist(),
                 "fin_buy": piv.get("margin_fin_buy", pd.Series(dtype=float)).tolist()},
        "as_of": str(piv["date"].iloc[-1]) if len(piv) else None,
        "coverage": f"{len(piv)}个交易日(上交所口径)",
        "denominator": "交易所披露原值(元)",
        "warnings": ["仅上交所；深交所逐日接口待并入"],
        "published_cutoff": str(piv["date"].iloc[-1]) if len(piv) else None,
    }


@app.get("/api/v1/quality/status")
def get_quality():
    with _session() as s:
        per_metric = s.execute(
            select(FactObservation.metric, FactObservation.subject_key,
                   func.count(), func.min(FactObservation.effective_at),
                   func.max(FactObservation.effective_at))
            .group_by(FactObservation.metric).order_by(func.count().desc())
        ).all()
        jobs = s.execute(
            select(JobRun.job_key, JobRun.status, JobRun.finished_at, JobRun.data_range)
            .order_by(JobRun.id.desc()).limit(20)).all()
    return {
        "data": {
            "facts_by_metric": [
                {"metric": m, "sample_subject": subj, "rows": n, "range": f"{lo}..{hi}"}
                for m, subj, n, lo, hi in per_metric],
            "recent_jobs": [
                {"job_key": k, "status": st, "finished_at": str(f), "range": r}
                for k, st, f, r in jobs],
        },
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
        "coverage": f"{sum(r[2] for r in per_metric)} facts",
        "denominator": "fact_observation 计数（按修订链去重前）",
        "warnings": [],
    }


@app.get("/health")
def health():
    return {"ok": True}
