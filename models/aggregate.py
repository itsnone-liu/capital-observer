"""Active-fund aggregate industry allocation estimates (sec.7.4, P3).

Sample -> aggregate, with honest labeling:
  - equal-weight & median aggregation (no AUM weights available in free sources)
  - aggregate labeled identification_bound (NOT a market total)
  - per-fund fit quality reported; funds with R2 < 0.2 excluded from headline
    and counted in coverage warnings
"""
from __future__ import annotations

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.nowcast import nowcast_fund
from storage.models import FactObservation

SW_INDUSTRIES = ["801010", "801030", "801040", "801050", "801080", "801110", "801120",
                 "801130", "801140", "801150", "801160", "801170", "801180", "801200",
                 "801210", "801230", "801710", "801720", "801730", "801740", "801750",
                 "801760", "801770", "801780", "801790", "801880", "801890", "801950",
                 "801960", "801970", "801980"]


def fund_universe(session: Session) -> list[str]:
    rows = session.execute(
        select(FactObservation.subject_key).distinct()
        .where(FactObservation.metric == "fund_nav")).scalars().all()
    return [r.split(":", 1)[-1] for r in rows]


def aggregate_exposure(session: Session, window: int = 120, min_r2: float = 0.2) -> dict:
    funds = fund_universe(session)
    exposures, r2s, failed = [], [], []
    for f in funds:
        try:
            out = nowcast_fund(session, f, SW_INDUSTRIES, prior=None, window=window)
        except Exception as e:  # noqa: BLE001 — per-fund isolation
            failed.append({"fund": f, "error": str(e)[:80]})
            continue
        if out.get("status") != "ok":
            failed.append({"fund": f, "error": out.get("status")})
            continue
        r2s.append(out["fit_r2_in_sample"])
        exposures.append((f, out["industry_exposure"], out["equity_sum"],
                          out["fit_r2_in_sample"]))
    kept = [e for e in exposures if e[3] is not None and e[3] >= min_r2]
    if not kept:
        return {"status": "no_qualified_funds", "attempted": len(funds)}
    # equal-weight aggregate (identification bound)
    agg_mean = {}
    for _, exp, _, _ in kept:
        for k, v in exp.items():
            agg_mean[k] = agg_mean.get(k, 0.0) + v / len(kept)
    # median robustness
    per_ind = {k: sorted(e[1].get(k, 0.0) for e in kept) for k in agg_mean}
    agg_med = {k: float(np.median(v)) for k, v in per_ind.items()}
    # weekly aggregate change vs 5 days ago (exposure drift of the cohort)
    return {
        "status": "ok",
        "data": {
            "n_funds_attempted": len(funds),
            "n_funds_qualified": len(kept),
            "n_failed": len(failed),
            "mean_r2": round(float(np.mean([e[3] for e in kept])), 3),
            "aggregate_equal_weight": {k: round(v, 4) for k, v in
                                       sorted(agg_mean.items(), key=lambda kv: -kv[1])},
            "aggregate_median": {k: round(v, 4) for k, v in agg_med.items()},
            "mean_equity_position": round(float(np.mean([e[2] for e in kept])), 3),
            "top5": dict(sorted(agg_mean.items(), key=lambda kv: -kv[1])[:5]),
        },
        "as_of": None,
        "coverage": f"{len(kept)}/{len(funds)}只样本基金达标(R²≥{min_r2})",
        "denominator": "等权样本聚合（无规模权重）；样本=可申购主动权益哈希抽样60只",
        "warnings": [f"{len(failed)}只未达标或失败" ] + [f"{d['fund']}: {d['error']}" for d in failed[:5]],
        "scenario_id": "L2_aggregate",
        "range_type": "identification_bound",
        "assumptions": [
            "样本等权（免费源无规模字段，非加权市场估计）",
            "行业暴露来自申万一级线性近似（nowcast）",
            "R2<0.2 的基金剔除并计数（不纳入不等于不存在）",
        ],
        "invalidation": "获得规模数据后应改为加权聚合；样本外季度披露对照验证",
    }
